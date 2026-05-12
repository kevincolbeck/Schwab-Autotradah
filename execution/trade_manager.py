"""Trade management: breakeven stops, EOD exits, regime exits."""

import logging
from datetime import datetime, time as dtime
from typing import List, Optional

import pandas as pd
from sqlalchemy.orm import Session

from broker.schwab_client import SchwabBrokerClient
from core.constants import Regime, TradeStatus, EventType
from core.config import Config
from data.market_data import MarketDataCache
from data.models import Trade, BotOrder, Event, DailyPnL
from execution.order_manager import OrderManager
from strategy.indicators import sma

logger = logging.getLogger(__name__)


class TradeManager:
    """Manages open trades: BE stops, EOD exits, regime liquidation."""

    def __init__(
        self,
        broker: SchwabBrokerClient,
        order_mgr: OrderManager,
        data_cache: MarketDataCache,
        config: Config,
        db_session_factory,
    ):
        self.broker = broker
        self.order_mgr = order_mgr
        self.data = data_cache
        self.config = config
        self.db_session_factory = db_session_factory

    def manage_all(
        self, current_time: datetime, regime: Regime, shadow_mode: bool = False,
    ):
        """Run all trade management checks on open positions."""
        session = self.db_session_factory()
        try:
            open_trades = session.query(Trade).filter(
                Trade.status.in_([TradeStatus.OPEN.name, TradeStatus.BREAKEVEN.name]),
                Trade.bot_managed == True,
            ).all()

            for trade in open_trades:
                quote = self.data.get_quote(trade.symbol)
                if not quote:
                    continue

                last_price = quote.get('last', 0)

                # 1) Breakeven at +1R
                self._check_breakeven(trade, last_price, session, shadow_mode)

                # 2) EOD MA-based exit
                self._check_eod_exit(trade, last_price, current_time, session, shadow_mode)

            session.commit()
        except Exception as e:
            logger.error(f"Trade management error: {e}")
            session.rollback()
        finally:
            session.close()

    def _check_breakeven(
        self, trade: Trade, last_price: float, session: Session, shadow_mode: bool,
    ):
        """Move stop to breakeven when price reaches +1R."""
        if trade.breakeven_set:
            return
        if not trade.entry_price or not trade.r_value or trade.r_value <= 0:
            return

        be_trigger = trade.entry_price + trade.r_value
        if last_price >= be_trigger:
            be_price = trade.entry_price + (trade.entry_price * self.config.stop.breakeven_buffer_pct / 100)

            # Never lower the stop
            if trade.current_stop_price and be_price <= trade.current_stop_price:
                return

            success = self.order_mgr.update_stop_price(trade, be_price, session, shadow_mode)
            if success:
                trade.breakeven_set = True
                trade.status = TradeStatus.BREAKEVEN.name
                logger.info(f"Breakeven set: {trade.symbol} stop -> {be_price:.2f} (was {trade.current_stop_price:.2f})")
                event = Event(
                    event_type=EventType.STOP_TO_BREAKEVEN.name,
                    symbol=trade.symbol,
                    trade_id=trade.id,
                    message=f"Stop moved to BE: {be_price:.2f} at price {last_price:.2f} (+1R={be_trigger:.2f})",
                )
                session.add(event)

    def _check_eod_exit(
        self, trade: Trade, last_price: float, current_time: datetime,
        session: Session, shadow_mode: bool,
    ):
        """EOD MA-based full exit in the last N minutes of trading."""
        cfg = self.config.eod_exit

        # Check if we're in the EOD window
        market_close = current_time.replace(hour=16, minute=0, second=0, microsecond=0)
        eod_start = current_time.replace(
            hour=15,
            minute=60 - cfg.eod_window_minutes,
            second=0, microsecond=0,
        )
        # Handle edge case for window
        minutes_to_close = (market_close - current_time).total_seconds() / 60
        if minutes_to_close > cfg.eod_window_minutes or minutes_to_close < 0:
            return

        # Compute SMA10 and SMA20 for the stock
        df = self.data.get_daily_bars(trade.symbol)
        if df is None or len(df) < 20:
            return

        close = df['close']
        sma10 = sma(close, 10)
        sma20 = sma(close, 20)

        if pd.isna(sma10.iloc[-1]) or pd.isna(sma20.iloc[-1]):
            return

        sma10_val = sma10.iloc[-1]
        sma20_val = sma20.iloc[-1]

        # Determine exit line based on closeness
        closeness = abs(sma10_val - sma20_val) / sma10_val * 100 if sma10_val > 0 else 999
        if closeness <= cfg.sma_closeness_threshold:
            exit_line = sma20_val
            exit_line_name = "SMA20"
        else:
            exit_line = sma10_val
            exit_line_name = "SMA10"

        # Check if price is below exit line
        if last_price < exit_line:
            logger.info(f"EOD exit triggered: {trade.symbol} price={last_price:.2f} < {exit_line_name}={exit_line:.2f}")

            if shadow_mode:
                logger.info(f"[SHADOW] Would EOD exit {trade.symbol} qty={trade.shares}")
            else:
                # Use MOC if available, else market sell
                if cfg.use_moc_if_available:
                    order_id = self.broker.place_moc_sell(trade.symbol, trade.shares)
                else:
                    order_id = self.broker.place_market_sell(trade.symbol, trade.shares)

                if order_id:
                    bot_order = BotOrder(
                        trade_id=trade.id,
                        symbol=trade.symbol,
                        broker_order_id=order_id,
                        side='SELL',
                        order_type='MARKET_ON_CLOSE' if cfg.use_moc_if_available else 'MARKET',
                        status='WORKING',
                        requested_qty=trade.shares,
                        placed_at=datetime.utcnow(),
                        purpose='EOD_EXIT',
                    )
                    session.add(bot_order)

                    # Cancel existing stop
                    self._cancel_stop_order(trade, session)

            trade.status = TradeStatus.CLOSED_EOD.name
            trade.exit_time = datetime.utcnow()
            trade.exit_price = last_price
            trade.exit_reason = f"EOD exit: price < {exit_line_name}"
            if trade.entry_price and trade.r_value and trade.r_value > 0:
                trade.pnl_dollars = (last_price - trade.entry_price) * trade.shares
                trade.pnl_r_multiple = (last_price - trade.entry_price) / trade.r_value

            event = Event(
                event_type=EventType.EOD_EXIT.name,
                symbol=trade.symbol,
                trade_id=trade.id,
                message=f"EOD exit: price={last_price:.2f} < {exit_line_name}={exit_line:.2f} (closeness={closeness:.2f}%)",
            )
            session.add(event)

    def liquidate_all(self, reason: str, session: Session, shadow_mode: bool = False):
        """Sell all positions (for RISK_OFF or kill switch)."""
        open_trades = session.query(Trade).filter(
            Trade.status.in_([TradeStatus.OPEN.name, TradeStatus.BREAKEVEN.name]),
        ).all()

        for trade in open_trades:
            quote = self.data.get_quote(trade.symbol)
            last_price = quote.get('last', 0) if quote else 0

            if shadow_mode:
                logger.info(f"[SHADOW] Would liquidate {trade.symbol} qty={trade.shares}")
            else:
                order_id = self.broker.place_market_sell(trade.symbol, trade.shares)
                if order_id:
                    bot_order = BotOrder(
                        trade_id=trade.id,
                        symbol=trade.symbol,
                        broker_order_id=order_id,
                        side='SELL',
                        order_type='MARKET',
                        status='WORKING',
                        requested_qty=trade.shares,
                        placed_at=datetime.utcnow(),
                        purpose='REGIME_EXIT',
                    )
                    session.add(bot_order)
                self._cancel_stop_order(trade, session)

            trade.status = TradeStatus.CLOSED_REGIME.name
            trade.exit_time = datetime.utcnow()
            trade.exit_price = last_price
            trade.exit_reason = reason
            if trade.entry_price and trade.r_value and trade.r_value > 0:
                trade.pnl_dollars = (last_price - trade.entry_price) * trade.shares
                trade.pnl_r_multiple = (last_price - trade.entry_price) / trade.r_value

            event = Event(
                event_type=EventType.RISK_OFF_LIQUIDATION.name,
                symbol=trade.symbol,
                trade_id=trade.id,
                message=f"Liquidated: {reason}",
            )
            session.add(event)

        logger.warning(f"Liquidated {len(open_trades)} positions: {reason}")

    def _cancel_stop_order(self, trade: Trade, session: Session):
        """Cancel existing stop loss order for a trade."""
        stop_orders = session.query(BotOrder).filter(
            BotOrder.trade_id == trade.id,
            BotOrder.purpose == 'STOP_LOSS',
            BotOrder.status.in_(['WORKING', 'PENDING']),
        ).all()
        for so in stop_orders:
            if so.broker_order_id and so.broker_order_id not in ("SHADOW_STOP", "SHADOW_ORDER"):
                self.broker.cancel_order(so.broker_order_id)
            so.status = 'CANCELLED'
            so.cancelled_at = datetime.utcnow()

    def check_daily_loss_lock(self) -> bool:
        """Check if daily loss exceeds limit. Returns True if locked out."""
        session = self.db_session_factory()
        try:
            today = datetime.utcnow().strftime('%Y-%m-%d')
            daily = session.query(DailyPnL).filter_by(date=today).first()
            if daily and daily.total_r <= -self.config.safety.daily_loss_lock_r:
                if not daily.locked_out:
                    daily.locked_out = True
                    session.commit()
                    logger.warning(f"Daily loss lock: {daily.total_r:.1f}R exceeds -{self.config.safety.daily_loss_lock_r}R")
                return True
            return False
        except Exception:
            return False
        finally:
            session.close()

    def update_daily_pnl(self, trade: Trade):
        """Update daily PnL tracking when a trade closes."""
        session = self.db_session_factory()
        try:
            today = datetime.utcnow().strftime('%Y-%m-%d')
            daily = session.query(DailyPnL).filter_by(date=today).first()
            if not daily:
                daily = DailyPnL(date=today)
                session.add(daily)

            if trade.pnl_dollars:
                daily.realized_pnl = (daily.realized_pnl or 0) + trade.pnl_dollars
            if trade.pnl_r_multiple:
                daily.total_r = (daily.total_r or 0) + trade.pnl_r_multiple
            daily.trades_closed = (daily.trades_closed or 0) + 1
            session.commit()
        except Exception as e:
            logger.error(f"Error updating daily PnL: {e}")
            session.rollback()
        finally:
            session.close()
