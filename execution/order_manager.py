"""Order placement, tracking, and cancellation."""

import logging
from datetime import datetime, timedelta
from typing import Optional, List

from sqlalchemy.orm import Session

from broker.schwab_client import SchwabBrokerClient
from core.config import Config
from data.models import BotOrder, Trade, Event
from core.constants import EventType, TradeStatus

logger = logging.getLogger(__name__)


class OrderManager:
    """Manages order lifecycle: place, track, cancel, expire."""

    def __init__(self, broker: SchwabBrokerClient, config: Config, db_session_factory):
        self.broker = broker
        self.config = config
        self.db_session_factory = db_session_factory

    def place_entry_order(
        self,
        trade: Trade,
        stop_price: float,
        limit_price: float,
        qty: int,
        session: Session,
        shadow_mode: bool = False,
    ) -> Optional[str]:
        """Place a stop-limit buy entry order. Returns broker order ID."""
        symbol = trade.symbol

        if shadow_mode:
            logger.info(f"[SHADOW] Would place entry: {symbol} qty={qty} stop={stop_price:.2f} limit={limit_price:.2f}")
            self._log_event(session, EventType.ORDER_PLACED, symbol, trade.id,
                          f"[SHADOW] Entry order: qty={qty} stop={stop_price:.2f} limit={limit_price:.2f}")
            return "SHADOW_ORDER"

        broker_order_id = self.broker.place_stop_limit_buy(symbol, qty, stop_price, limit_price)
        if not broker_order_id:
            self._log_event(session, EventType.ORDER_REJECTED, symbol, trade.id,
                          f"Entry order rejected by broker")
            return None

        # Record order in DB
        bot_order = BotOrder(
            trade_id=trade.id,
            symbol=symbol,
            broker_order_id=broker_order_id,
            side='BUY',
            order_type='STOP_LIMIT',
            status='WORKING',
            requested_qty=qty,
            stop_price=stop_price,
            limit_price=limit_price,
            placed_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(minutes=self.config.breakout.max_wait_minutes),
            purpose='ENTRY',
        )
        session.add(bot_order)

        self._log_event(session, EventType.ORDER_PLACED, symbol, trade.id,
                      f"Entry order placed: id={broker_order_id} qty={qty} stop={stop_price:.2f} limit={limit_price:.2f}")

        return broker_order_id

    def place_market_entry_order(
        self,
        trade: Trade,
        qty: int,
        session: Session,
        shadow_mode: bool = False,
    ) -> Optional[str]:
        """Place a market-buy entry order. Returns broker order ID."""
        symbol = trade.symbol

        if shadow_mode:
            logger.info(f"[SHADOW] Would place market entry: {symbol} qty={qty}")
            self._log_event(
                session, EventType.ORDER_PLACED, symbol, trade.id,
                f"[SHADOW] Market entry order: qty={qty}"
            )
            return "SHADOW_ORDER"

        broker_order_id = self.broker.place_market_buy(symbol, qty)
        if not broker_order_id:
            self._log_event(
                session, EventType.ORDER_REJECTED, symbol, trade.id,
                "Market entry order rejected by broker"
            )
            return None

        bot_order = BotOrder(
            trade_id=trade.id,
            symbol=symbol,
            broker_order_id=broker_order_id,
            side='BUY',
            order_type='MARKET',
            status='WORKING',
            requested_qty=qty,
            placed_at=datetime.utcnow(),
            purpose='ENTRY',
        )
        session.add(bot_order)

        self._log_event(
            session, EventType.ORDER_PLACED, symbol, trade.id,
            f"Market entry order placed: id={broker_order_id} qty={qty}"
        )
        return broker_order_id

    def place_limit_entry_order_extended(
        self,
        trade: Trade,
        qty: int,
        limit_price: float,
        session: Session,
        shadow_mode: bool = False,
    ) -> Optional[str]:
        """Place a limit buy entry order valid for extended hours (after-hours / pre-market)."""
        symbol = trade.symbol

        if shadow_mode:
            logger.info(
                f"[SHADOW] Would place extended-hours limit entry: {symbol} qty={qty} limit={limit_price:.2f}"
            )
            self._log_event(
                session, EventType.ORDER_PLACED, symbol, trade.id,
                f"[SHADOW] Extended-hours limit entry: qty={qty} limit={limit_price:.2f}",
            )
            return "SHADOW_ORDER"

        broker_order_id = self.broker.place_limit_buy_extended(symbol, qty, limit_price)
        if not broker_order_id:
            self._log_event(
                session, EventType.ORDER_REJECTED, symbol, trade.id,
                "Extended-hours limit entry rejected by broker",
            )
            return None

        bot_order = BotOrder(
            trade_id=trade.id,
            symbol=symbol,
            broker_order_id=broker_order_id,
            side='BUY',
            order_type='LIMIT',
            status='WORKING',
            requested_qty=qty,
            limit_price=limit_price,
            placed_at=datetime.utcnow(),
            purpose='ENTRY',
        )
        session.add(bot_order)

        self._log_event(
            session, EventType.ORDER_PLACED, symbol, trade.id,
            f"Extended-hours limit entry placed: id={broker_order_id} qty={qty} limit={limit_price:.2f}",
        )
        return broker_order_id

    def place_stop_loss(
        self,
        trade: Trade,
        stop_price: float,
        qty: int,
        session: Session,
        shadow_mode: bool = False,
    ) -> Optional[str]:
        """Place a protective sell stop order."""
        symbol = trade.symbol

        if shadow_mode:
            logger.info(f"[SHADOW] Would place stop loss: {symbol} qty={qty} stop={stop_price:.2f}")
            return "SHADOW_STOP"

        broker_order_id = self.broker.place_sell_stop(symbol, qty, stop_price)
        if not broker_order_id:
            logger.error(f"Failed to place stop loss for {symbol}")
            return None

        bot_order = BotOrder(
            trade_id=trade.id,
            symbol=symbol,
            broker_order_id=broker_order_id,
            side='SELL',
            order_type='STOP',
            status='WORKING',
            requested_qty=qty,
            stop_price=stop_price,
            placed_at=datetime.utcnow(),
            purpose='STOP_LOSS',
        )
        session.add(bot_order)

        self._log_event(session, EventType.ORDER_PLACED, symbol, trade.id,
                      f"Stop loss placed: id={broker_order_id} stop={stop_price:.2f}")

        return broker_order_id

    def cancel_entry_order(self, trade: Trade, session: Session, shadow_mode: bool = False) -> bool:
        """Cancel a pending entry order."""
        entry_orders = session.query(BotOrder).filter(
            BotOrder.trade_id == trade.id,
            BotOrder.purpose == 'ENTRY',
            BotOrder.status.in_(['WORKING', 'PENDING']),
        ).all()

        success = True
        for order in entry_orders:
            if shadow_mode:
                logger.info(f"[SHADOW] Would cancel entry order {order.broker_order_id}")
            elif order.broker_order_id and order.broker_order_id != "SHADOW_ORDER":
                if not self.broker.cancel_order(order.broker_order_id):
                    success = False
                    continue
            order.status = 'CANCELLED'
            order.cancelled_at = datetime.utcnow()

        if entry_orders:
            trade.status = TradeStatus.CANCELLED.name
            self._log_event(session, EventType.ORDER_CANCELLED, trade.symbol, trade.id,
                          f"Entry order cancelled")

        return success

    def cancel_all_entry_orders(self, session: Session, shadow_mode: bool = False) -> int:
        """Cancel all pending entry orders (for kill switch / risk-off)."""
        pending_entries = session.query(BotOrder).filter(
            BotOrder.purpose == 'ENTRY',
            BotOrder.status.in_(['WORKING', 'PENDING']),
        ).all()

        cancelled = 0
        for order in pending_entries:
            if shadow_mode:
                logger.info(f"[SHADOW] Would cancel entry {order.broker_order_id} for {order.symbol}")
            elif order.broker_order_id and order.broker_order_id != "SHADOW_ORDER":
                self.broker.cancel_order(order.broker_order_id)
            order.status = 'CANCELLED'
            order.cancelled_at = datetime.utcnow()
            cancelled += 1

            # Update trade status
            trade = session.query(Trade).filter_by(id=order.trade_id).first()
            if trade and trade.status == TradeStatus.PENDING_ENTRY.name:
                trade.status = TradeStatus.CANCELLED.name

        return cancelled

    def check_expired_entries(self, session: Session, shadow_mode: bool = False) -> int:
        """Cancel entry orders that have exceeded max_wait_minutes."""
        now = datetime.utcnow()
        expired = session.query(BotOrder).filter(
            BotOrder.purpose == 'ENTRY',
            BotOrder.status.in_(['WORKING', 'PENDING']),
            BotOrder.expires_at <= now,
        ).all()

        cancelled = 0
        for order in expired:
            if not shadow_mode and order.broker_order_id and order.broker_order_id != "SHADOW_ORDER":
                self.broker.cancel_order(order.broker_order_id)
            order.status = 'CANCELLED'
            order.cancelled_at = now
            cancelled += 1

            trade = session.query(Trade).filter_by(id=order.trade_id).first()
            if trade and trade.status == TradeStatus.PENDING_ENTRY.name:
                trade.status = TradeStatus.CANCELLED.name
                self._log_event(session, EventType.ORDER_CANCELLED, trade.symbol, trade.id,
                              f"Entry expired after {self.config.breakout.max_wait_minutes}min")

        return cancelled

    def update_stop_price(
        self, trade: Trade, new_stop_price: float, session: Session, shadow_mode: bool = False,
    ) -> bool:
        """Update the stop price for an open trade."""
        stop_order = session.query(BotOrder).filter(
            BotOrder.trade_id == trade.id,
            BotOrder.purpose == 'STOP_LOSS',
            BotOrder.status.in_(['WORKING', 'PENDING']),
        ).first()

        if not stop_order:
            logger.warning(f"No working stop order found for {trade.symbol}")
            return False

        if shadow_mode:
            logger.info(f"[SHADOW] Would update stop: {trade.symbol} {stop_order.stop_price:.2f} -> {new_stop_price:.2f}")
            stop_order.stop_price = new_stop_price
            return True

        if stop_order.broker_order_id and stop_order.broker_order_id != "SHADOW_STOP":
            new_order_id = self.broker.replace_stop_order(
                stop_order.broker_order_id, trade.symbol, trade.shares, new_stop_price
            )
            if new_order_id:
                stop_order.broker_order_id = new_order_id
                stop_order.stop_price = new_stop_price
                trade.current_stop_price = new_stop_price
                return True
            return False
        else:
            stop_order.stop_price = new_stop_price
            trade.current_stop_price = new_stop_price
            return True

    def _log_event(self, session: Session, event_type: EventType, symbol: str,
                   trade_id: int, message: str):
        """Log an event to the database."""
        event = Event(
            event_type=event_type.name,
            symbol=symbol,
            trade_id=trade_id,
            message=message,
        )
        session.add(event)
