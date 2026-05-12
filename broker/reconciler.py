"""Position and order reconciliation — broker is source of truth."""

import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from broker.schwab_client import SchwabBrokerClient
from core.constants import TradeStatus, EventType
from data.models import Trade, BotOrder, Event, ManualOverride

logger = logging.getLogger(__name__)


class Reconciler:
    """Reconciles local state with broker positions/orders every cycle."""

    def __init__(self, broker: SchwabBrokerClient, db_session_factory, config):
        self.broker = broker
        self.db_session_factory = db_session_factory
        self.config = config
        self._last_broker_positions: Dict[str, float] = {}
        self._last_broker_orders: Dict[str, dict] = {}

    def reconcile(self) -> List[dict]:
        """Run full reconciliation. Returns list of detected events."""
        events = []
        session: Session = self.db_session_factory()
        try:
            broker_positions = self.broker.get_positions()
            broker_orders = self.broker.get_orders()

            broker_pos_map = {p['symbol']: p for p in broker_positions if p['qty'] != 0}

            # Get all locally tracked open trades
            open_trades = session.query(Trade).filter(
                Trade.status.in_([
                    TradeStatus.OPEN.name,
                    TradeStatus.BREAKEVEN.name,
                    TradeStatus.PENDING_ENTRY.name,
                ])
            ).all()

            open_trade_map = {t.symbol: t for t in open_trades}

            # --- Detect manual closes ---
            for symbol, trade in open_trade_map.items():
                if trade.status == TradeStatus.PENDING_ENTRY.name:
                    continue
                if symbol not in broker_pos_map:
                    # Position fully closed externally
                    events.append(self._handle_manual_close(session, trade))
                else:
                    broker_qty = int(broker_pos_map[symbol]['qty'])
                    if broker_qty < trade.shares:
                        # Partial close
                        events.append(self._handle_partial_close(session, trade, broker_qty))

            # --- Detect stop modifications ---
            broker_order_map = self._build_order_map(broker_orders)
            bot_orders = session.query(BotOrder).filter(
                BotOrder.status.in_(['WORKING', 'PENDING']),
                BotOrder.purpose == 'STOP_LOSS',
            ).all()

            for bot_order in bot_orders:
                if bot_order.broker_order_id in broker_order_map:
                    broker_ord = broker_order_map[bot_order.broker_order_id]
                    broker_stop = self._get_stop_price_from_order(broker_ord)
                    if broker_stop and bot_order.stop_price:
                        if abs(broker_stop - bot_order.stop_price) > 0.005:
                            events.append(self._handle_stop_modified(
                                session, bot_order, broker_stop
                            ))
                elif bot_order.broker_order_id:
                    # Order no longer exists at broker — may have been cancelled
                    trade = session.query(Trade).filter_by(id=bot_order.trade_id).first()
                    if trade and trade.symbol in broker_pos_map:
                        events.append(self._handle_stop_modified(
                            session, bot_order, None
                        ))

            # --- Detect filled entry orders ---
            pending_entries = session.query(BotOrder).filter(
                BotOrder.status.in_(['WORKING', 'PENDING']),
                BotOrder.purpose == 'ENTRY',
            ).all()

            for entry_order in pending_entries:
                if entry_order.broker_order_id in broker_order_map:
                    broker_ord = broker_order_map[entry_order.broker_order_id]
                    status = broker_ord.get('status', '')
                    if status == 'FILLED':
                        events.append({
                            'type': 'ENTRY_FILLED',
                            'order_id': entry_order.broker_order_id,
                            'trade_id': entry_order.trade_id,
                            'symbol': entry_order.symbol,
                            'filled_price': self._get_filled_price(broker_ord),
                            'filled_qty': self._get_filled_qty(broker_ord),
                        })
                    elif status in ('CANCELED', 'CANCELLED', 'REJECTED', 'EXPIRED'):
                        entry_order.status = 'CANCELLED'
                        trade = session.query(Trade).filter_by(id=entry_order.trade_id).first()
                        if trade and trade.status == TradeStatus.PENDING_ENTRY.name:
                            trade.status = TradeStatus.CANCELLED.name
                        events.append({
                            'type': 'ENTRY_CANCELLED',
                            'order_id': entry_order.broker_order_id,
                            'trade_id': entry_order.trade_id,
                            'symbol': entry_order.symbol,
                        })

            session.commit()
        except Exception as e:
            logger.error(f"Reconciliation error: {e}")
            session.rollback()
        finally:
            session.close()

        return events

    def _handle_manual_close(self, session: Session, trade: Trade) -> dict:
        """Handle a position that was fully closed outside the bot."""
        logger.warning(f"Manual close detected: {trade.symbol}")
        trade.status = TradeStatus.CLOSED_MANUAL.name
        trade.exit_time = datetime.utcnow()
        trade.exit_reason = "MANUAL_CLOSE"
        trade.manually_overridden = True
        trade.bot_managed = False

        # Cancel any bot-owned working orders for this symbol
        bot_orders = session.query(BotOrder).filter(
            BotOrder.trade_id == trade.id,
            BotOrder.status.in_(['WORKING', 'PENDING']),
        ).all()
        for bo in bot_orders:
            if bo.broker_order_id:
                self.broker.cancel_order(bo.broker_order_id)
            bo.status = 'CANCELLED'
            bo.cancelled_at = datetime.utcnow()

        # Record manual override with re-entry cooldown
        cooldown_days = self.config.app.reentry_cooldown_days
        override = ManualOverride(
            symbol=trade.symbol,
            override_type='CLOSED',
            reentry_allowed_after=datetime.utcnow() + timedelta(days=cooldown_days),
        )
        session.add(override)

        event = Event(
            event_type=EventType.MANUAL_CLOSE_DETECTED.name,
            symbol=trade.symbol,
            trade_id=trade.id,
            message=f"Manual close detected for {trade.symbol}",
        )
        session.add(event)

        return {
            'type': 'MANUAL_CLOSE',
            'symbol': trade.symbol,
            'trade_id': trade.id,
        }

    def _handle_partial_close(self, session: Session, trade: Trade, new_qty: int) -> dict:
        """Handle a partial close detected from broker."""
        old_qty = trade.shares
        logger.warning(f"Partial close detected: {trade.symbol} {old_qty} -> {new_qty}")
        trade.shares = new_qty

        event = Event(
            event_type=EventType.MANUAL_PARTIAL_DETECTED.name,
            symbol=trade.symbol,
            trade_id=trade.id,
            message=f"Partial close: {trade.symbol} {old_qty} -> {new_qty}",
        )
        session.add(event)

        # Update stop order quantity if exists
        stop_order = session.query(BotOrder).filter(
            BotOrder.trade_id == trade.id,
            BotOrder.purpose == 'STOP_LOSS',
            BotOrder.status.in_(['WORKING', 'PENDING']),
        ).first()
        if stop_order and stop_order.broker_order_id:
            new_order_id = self.broker.replace_stop_order(
                stop_order.broker_order_id,
                trade.symbol,
                new_qty,
                stop_order.stop_price,
            )
            if new_order_id:
                stop_order.broker_order_id = new_order_id
                stop_order.requested_qty = new_qty

        return {
            'type': 'PARTIAL_CLOSE',
            'symbol': trade.symbol,
            'trade_id': trade.id,
            'old_qty': old_qty,
            'new_qty': new_qty,
        }

    def _handle_stop_modified(self, session: Session, bot_order: BotOrder, new_stop: Optional[float]) -> dict:
        """Handle a stop order that was modified/cancelled externally."""
        trade = session.query(Trade).filter_by(id=bot_order.trade_id).first()
        symbol = bot_order.symbol

        if trade:
            trade.manually_overridden = True
            trade.bot_managed = False

        override = ManualOverride(
            symbol=symbol,
            override_type='STOP_MODIFIED',
        )
        session.add(override)

        event = Event(
            event_type=EventType.MANUAL_STOP_MODIFIED.name,
            symbol=symbol,
            trade_id=bot_order.trade_id,
            message=f"Stop modified externally: {symbol} old={bot_order.stop_price} new={new_stop}",
        )
        session.add(event)

        logger.warning(f"Stop modified externally for {symbol}, disabling bot management")

        return {
            'type': 'STOP_MODIFIED',
            'symbol': symbol,
            'trade_id': bot_order.trade_id,
            'old_stop': bot_order.stop_price,
            'new_stop': new_stop,
        }

    def _build_order_map(self, broker_orders: List[dict]) -> Dict[str, dict]:
        """Build a map of order_id -> order data from broker orders."""
        result = {}
        for order in broker_orders:
            oid = str(order.get('orderId', ''))
            if oid:
                result[oid] = order
        return result

    def _get_stop_price_from_order(self, order: dict) -> Optional[float]:
        """Extract stop price from a broker order dict."""
        return order.get('stopPrice')

    def _get_filled_price(self, order: dict) -> Optional[float]:
        """Extract average filled price from a broker order."""
        legs = order.get('orderActivityCollection', [])
        if legs:
            for leg in legs:
                execs = leg.get('executionLegs', [])
                if execs:
                    total_cost = sum(e.get('price', 0) * e.get('quantity', 0) for e in execs)
                    total_qty = sum(e.get('quantity', 0) for e in execs)
                    if total_qty > 0:
                        return total_cost / total_qty
        return order.get('price')

    def _get_filled_qty(self, order: dict) -> int:
        """Extract filled quantity from a broker order."""
        return int(order.get('filledQuantity', 0))


    def reenable_bot_management(self, symbol: str):
        """Re-enable bot management for a manually overridden symbol."""
        session = self.db_session_factory()
        try:
            trade = session.query(Trade).filter(
                Trade.symbol == symbol,
                Trade.status.in_([TradeStatus.OPEN.name, TradeStatus.BREAKEVEN.name]),
            ).first()
            if trade:
                trade.manually_overridden = False
                trade.bot_managed = True

            override = session.query(ManualOverride).filter(
                ManualOverride.symbol == symbol,
                ManualOverride.bot_management_reenabled == False,
            ).order_by(ManualOverride.detected_at.desc()).first()
            if override:
                override.bot_management_reenabled = True
                override.reenabled_at = datetime.utcnow()

            event = Event(
                event_type=EventType.BOT_MANAGEMENT_REENABLED.name,
                symbol=symbol,
                message=f"Bot management re-enabled for {symbol}",
            )
            session.add(event)
            session.commit()
            logger.info(f"Bot management re-enabled for {symbol}")
        except Exception as e:
            logger.error(f"Error re-enabling bot management for {symbol}: {e}")
            session.rollback()
        finally:
            session.close()
