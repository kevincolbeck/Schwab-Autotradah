"""Main trading engine â€" orchestrates the full strategy cycle."""

import logging
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict

from sqlalchemy.orm import Session

from broker.schwab_client import SchwabBrokerClient
from broker.reconciler import Reconciler
from core.config import Config
from core.constants import Regime, TradeStatus, EventType
from data.market_data import MarketDataCache
from data.models import Trade, BotOrder, Event, DailyPnL, ManualOverride, init_db
from execution.kill_switch import KillSwitch
from execution.order_manager import OrderManager
from execution.trade_manager import TradeManager
from strategy.filters import SymbolFilter
from strategy.regime import RegimeDetector
from strategy.signals import SignalDetector
from strategy.sizing import PositionSizer
from strategy.universe import UniverseBuilder
from strategy.computed_scanners import passes_selected_scanners, scanner_rules_from_config
from strategy.live_rule_based import LiveRuleBasedExecutor

logger = logging.getLogger(__name__)


class TradingEngine:
    """Main orchestrator: runs the strategy loop and coordinates all components."""

    def __init__(self, broker_client: SchwabBrokerClient, config: Config):
        self.broker = broker_client
        self.config = config
        self.shadow_mode = config.app.shadow_mode
        self._running = False
        self._allow_new_trades = True

        # Database
        self.db_session_factory = init_db(config.app.db_path)

        # Components
        self.data_cache = MarketDataCache(broker_client, config)
        self.universe = UniverseBuilder(broker_client, config)
        self.filters = SymbolFilter(self.data_cache, config)
        self.regime_detector = RegimeDetector(self.data_cache, config, self.db_session_factory)
        self.signal_detector = SignalDetector(self.data_cache, config)
        self.sizer = PositionSizer(config, self.db_session_factory)
        self.order_mgr = OrderManager(broker_client, config, self.db_session_factory)
        self.trade_mgr = TradeManager(
            broker_client, self.order_mgr, self.data_cache, config, self.db_session_factory
        )
        self.reconciler = Reconciler(broker_client, self.db_session_factory, config)
        self.kill_switch = KillSwitch(self.db_session_factory)
        self.rule_live_executor = LiveRuleBasedExecutor(
            config, self.data_cache, self.order_mgr, self.db_session_factory
        )

        # AI Strategist (always-on)
        self.ai_strategist = None
        try:
            ai_key = getattr(config, 'ai_chat', None)
            api_key = getattr(ai_key, 'anthropic_api_key', '') if ai_key else ''
            spec_path = getattr(config.strategy_runtime, 'active_strategy_spec_path', '')
            if api_key and spec_path:
                from ai.strategist import AIStrategist
                self.ai_strategist = AIStrategist(
                    api_key=api_key,
                    db_session_factory=self.db_session_factory,
                    spec_path=spec_path,
                    engine=self,
                )
                logger.info("AI Strategist initialized")
        except Exception as e:
            logger.warning(f"AI Strategist not available: {e}")

        # Runtime state
        self._risk_per_trade_pct: float = config.sizing.risk_per_trade_pct
        self._current_regime = Regime.TREND
        self._regime_details: dict = {}
        self._last_universe: List[str] = []
        self._equity: float = 0.0
        self._live_strategy: str = "momentum_breakout"
        self._scanner_rules = scanner_rules_from_config(config.universe)

        # Callbacks for GUI updates
        self._on_regime_change = None
        self._on_trade_update = None
        self._on_log_event = None
        self._on_pnl_update = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def current_regime(self) -> Regime:
        return self._current_regime

    @property
    def risk_per_trade_pct(self) -> float:
        return self._risk_per_trade_pct

    @risk_per_trade_pct.setter
    def risk_per_trade_pct(self, value: float):
        clamped = max(self.config.sizing.risk_min_pct, min(self.config.sizing.risk_max_pct, value))
        self._risk_per_trade_pct = clamped
        logger.info(f"Risk per trade updated: {clamped:.2f}%")

    def set_callbacks(self, on_regime_change=None, on_trade_update=None,
                      on_log_event=None, on_pnl_update=None):
        """Set GUI callback functions."""
        self._on_regime_change = on_regime_change
        self._on_trade_update = on_trade_update
        self._on_log_event = on_log_event
        self._on_pnl_update = on_pnl_update

    def start(self):
        """Start the engine."""
        self._running = True
        live_strategy = (
            getattr(self.config.strategy_runtime, "active_live_strategy", "momentum_breakout")
            .strip()
            .lower()
        )
        if live_strategy not in {"momentum_breakout", "rule_based"}:
            logger.warning("Unknown live strategy '%s', defaulting to momentum_breakout.", live_strategy)
            live_strategy = "momentum_breakout"
        self._live_strategy = live_strategy
        self._log_event(EventType.ENGINE_START, "Trading engine started",
                       f"shadow_mode={self.shadow_mode} strategy={self._live_strategy}")
        logger.info("Engine started. Shadow mode: %s strategy: %s", self.shadow_mode, self._live_strategy)

    def stop(self):
        """Stop the engine."""
        self._running = False
        self._log_event(EventType.ENGINE_STOP, "Trading engine stopped")
        logger.info("Engine stopped")

    # --- Main cycle methods (called by scheduler) ---

    def run_reconciliation(self):
        """Reconciliation cycle: sync with broker."""
        if not self._running:
            return
        try:
            events = self.reconciler.reconcile()
            for ev in events:
                if ev['type'] == 'ENTRY_FILLED':
                    self._handle_entry_fill(ev)
                elif ev['type'] == 'ENTRY_CANCELLED':
                    if self._on_trade_update:
                        self._on_trade_update()
                elif ev['type'] == 'MANUAL_CLOSE':
                    if self._on_trade_update:
                        self._on_trade_update()
                elif ev['type'] == 'PARTIAL_CLOSE':
                    if self._on_trade_update:
                        self._on_trade_update()

            # Update equity
            try:
                acct = self.broker.get_account_info()
                self._equity = acct.get('equity', 0.0)
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Reconciliation cycle error: {e}")

    def run_signal_scan(self):
        """Signal scan cycle: evaluate universe, apply filters, check entries."""
        if not self._running:
            return

        if self._live_strategy == "rule_based":
            # Rule-based strategy evaluates at EOD only (3:50 PM ET).
            # Regular signal scans are skipped; entries and rule-exits need
            # the synthetic close bar which is only built during run_eod_check.
            return

        try:
            now = datetime.utcnow()  # Should convert to ET in production

            # Check daily loss lock
            if self.trade_mgr.check_daily_loss_lock():
                self._allow_new_trades = False
                return

            # Evaluate regime
            regime, details = self.regime_detector.evaluate()
            self._regime_details = details
            if regime != self._current_regime:
                old = self._current_regime
                self._current_regime = regime
                self.regime_detector.save_state(details)
                self._log_event(EventType.REGIME_CHANGE,
                              f"Regime: {old.value} -> {regime.value}", str(details))
                if self._on_regime_change:
                    self._on_regime_change(regime, details)

                # RISK_OFF: liquidate everything
                if regime == Regime.RISK_OFF:
                    session = self.db_session_factory()
                    try:
                        self.trade_mgr.liquidate_all("RISK_OFF regime", session, self.shadow_mode)
                        self.order_mgr.cancel_all_entry_orders(session, self.shadow_mode)
                        session.commit()
                    finally:
                        session.close()
                    return

            # No new trades if kill switch active or risk-off
            if not self.kill_switch.allow_new_trades or regime == Regime.RISK_OFF:
                # Still manage exits if allowed
                if self.kill_switch.allow_exit_management:
                    self.trade_mgr.manage_all(now, regime, self.shadow_mode)
                return

            if not self._allow_new_trades:
                self.trade_mgr.manage_all(now, regime, self.shadow_mode)
                return

            # Get universe
            symbols = self.universe.get_universe()
            if self.config.universe.source == "computed_scanners":
                symbols = self._filter_with_computed_scanners(symbols)
            self._last_universe = symbols

            if not symbols:
                self.trade_mgr.manage_all(now, regime, self.shadow_mode)
                return

            # Get quotes for all symbols
            quotes = self.data_cache.get_quotes_batch(symbols)

            # Check re-entry cooldowns
            session = self.db_session_factory()
            try:
                cooled_symbols = self._get_cooled_down_symbols(session)
            finally:
                session.close()

            # Filter symbols already in open trades or pending
            session = self.db_session_factory()
            try:
                active_symbols = set()
                active_trades = session.query(Trade).filter(
                    Trade.status.in_([
                        TradeStatus.OPEN.name,
                        TradeStatus.BREAKEVEN.name,
                        TradeStatus.PENDING_ENTRY.name,
                    ])
                ).all()
                for t in active_trades:
                    active_symbols.add(t.symbol)
            finally:
                session.close()

            # Get benchmark data for filters
            benchmark_df = self.data_cache.get_daily_bars(self.config.regime.benchmark)

            # Apply filters and find signals
            candidates = []
            for symbol in symbols:
                if symbol in active_symbols:
                    continue
                if symbol in cooled_symbols:
                    continue
                quote = quotes.get(symbol)
                if not quote:
                    continue

                # Check data staleness
                if self.data_cache.is_quote_stale(symbol):
                    continue

                # Apply all filters
                stricter = (regime == Regime.CHOP and self.config.regime.chop_stricter_filters)
                passed, reason = self.filters.apply_all_filters(symbol, quote, benchmark_df, stricter)

                self._log_event(EventType.SIGNAL_EVALUATED, f"{symbol}: {reason}", symbol=symbol)

                if passed:
                    candidates.append(symbol)

            # Detect entry signals
            if candidates:
                candidate_quotes = {s: quotes[s] for s in candidates if s in quotes}
                signals = self.signal_detector.scan_for_signals(candidates, candidate_quotes, now, regime)

                for signal in signals:
                    self._process_entry_signal(signal, regime)

            # Manage existing trades
            self.trade_mgr.manage_all(now, regime, self.shadow_mode)

            # Check for expired entry orders
            session = self.db_session_factory()
            try:
                self.order_mgr.check_expired_entries(session, self.shadow_mode)
                session.commit()
            finally:
                session.close()

        except Exception as e:
            logger.error(f"Signal scan cycle error: {e}", exc_info=True)

    def run_morning_check(self):
        """Morning open check at 9:30 AM ET.

        For rule-based strategy: evaluates signals on yesterday's confirmed
        close data and fires market-open orders for anything that triggered.
        """
        if not self._running:
            return
        if self._live_strategy == "rule_based":
            self._run_rule_based_morning_cycle()

    def run_eod_check(self):
        """End-of-day check - run near market close (3:50-4:00 PM ET).

        For rule-based strategy this is the primary evaluation window:
        real-time quotes are overlaid onto daily bars to create a synthetic
        close, then entry and exit signals are evaluated.
        """
        if not self._running:
            return
        if self._live_strategy == "rule_based":
            self._run_rule_based_eod_cycle()
            return
        now = datetime.utcnow()
        self.trade_mgr.manage_all(now, self._current_regime, self.shadow_mode)

    # --- Internal methods ---

    def _process_entry_signal(self, signal, regime: Regime):
        """Process a triggered entry signal: size and place order."""
        if self._equity <= 0:
            logger.warning("Cannot size trade: equity unknown")
            return

        shares, size_details = self.sizer.calculate(
            signal.entry_price, signal.stop_price,
            self._equity, regime, self._risk_per_trade_pct,
        )
        if shares <= 0:
            logger.info(f"Sizing rejected {signal.symbol}: {size_details.get('reason', 'unknown')}")
            return

        session = self.db_session_factory()
        try:
            # Create trade record
            r_value = signal.entry_price - signal.stop_price
            trade = Trade(
                symbol=signal.symbol,
                status=TradeStatus.PENDING_ENTRY.name,
                entry_signal_time=datetime.utcnow(),
                entry_price=signal.entry_price,
                shares=shares,
                initial_stop_price=signal.stop_price,
                current_stop_price=signal.stop_price,
                r_value=r_value,
                regime_at_entry=regime.value,
                size_multiplier=size_details.get('size_multiplier', 1.0),
                entry_reason=signal.reason,
            )
            session.add(trade)
            session.flush()  # Get trade.id

            # Place entry order
            limit_price = signal.entry_price * (1 + self.config.breakout.limit_offset_pct / 100)
            order_id = self.order_mgr.place_entry_order(
                trade, signal.entry_price, limit_price, shares, session, self.shadow_mode
            )

            if order_id:
                trade.entry_order_id = order_id
                self._log_event(
                    EventType.ENTRY_TRIGGERED, signal.reason,
                    f"symbol={signal.symbol} shares={shares} entry={signal.entry_price:.2f} "
                    f"stop={signal.stop_price:.2f} R={r_value:.2f} regime={regime.value}",
                    symbol=signal.symbol, trade_id=trade.id,
                )
            else:
                trade.status = TradeStatus.CANCELLED.name

            session.commit()

            if self._on_trade_update:
                self._on_trade_update()

        except Exception as e:
            logger.error(f"Error processing entry for {signal.symbol}: {e}")
            session.rollback()
        finally:
            session.close()

    def _handle_entry_fill(self, event: dict):
        """Handle an entry order that has been filled."""
        session = self.db_session_factory()
        try:
            trade = session.query(Trade).filter_by(id=event['trade_id']).first()
            if not trade:
                return

            trade.status = TradeStatus.OPEN.name
            trade.entry_price = event.get('filled_price', trade.entry_price)
            trade.entry_time = datetime.utcnow()
            trade.shares = event.get('filled_qty', trade.shares)

            # Recalculate R-value with actual fill price
            if trade.initial_stop_price:
                trade.r_value = trade.entry_price - trade.initial_stop_price

            # Place protective stop
            self.order_mgr.place_stop_loss(
                trade, trade.initial_stop_price, trade.shares, session, self.shadow_mode
            )

            self._log_event(
                EventType.ORDER_FILLED,
                f"Entry filled: {trade.symbol} @ {trade.entry_price:.2f} qty={trade.shares}",
                symbol=trade.symbol, trade_id=trade.id,
            )

            session.commit()

            if self._on_trade_update:
                self._on_trade_update()

        except Exception as e:
            logger.error(f"Error handling entry fill: {e}")
            session.rollback()
        finally:
            session.close()

    def _get_cooled_down_symbols(self, session: Session) -> set:
        """Get symbols that are in re-entry cooldown."""
        now = datetime.utcnow()
        overrides = session.query(ManualOverride).filter(
            ManualOverride.reentry_allowed_after > now,
            ManualOverride.bot_management_reenabled == False,
        ).all()
        return {o.symbol for o in overrides}

    def _log_event(self, event_type: EventType, message: str, details: str = "",
                   symbol: str = None, trade_id: int = None):
        """Log an event to DB and notify GUI."""
        session = self.db_session_factory()
        try:
            event = Event(
                event_type=event_type.name,
                symbol=symbol,
                trade_id=trade_id,
                message=message,
                details=details,
            )
            session.add(event)
            session.commit()
        except Exception:
            session.rollback()
        finally:
            session.close()

        if self._on_log_event:
            self._on_log_event(event_type.name, message)

    def _filter_with_computed_scanners(self, symbols: List[str]) -> List[str]:
        """Apply translated ThinkScript momentum scanners to candidate symbols."""
        if not symbols:
            return []

        selected = []
        scanner_names = self.config.universe.scanner_names
        for symbol in symbols:
            df = self.data_cache.get_daily_bars(symbol, min_bars=1)
            if df is None or df.empty:
                continue
            if passes_selected_scanners(df, scanner_names, self._scanner_rules):
                selected.append(symbol)

        selected = sorted(set(selected))
        if selected:
            logger.info(
                "Computed scanners selected %d/%d symbols", len(selected), len(symbols)
            )
        return selected

    def _run_rule_based_cycle(self):
        """Run live rule-based strategy cycle and bypass momentum logic."""
        try:
            if not self.kill_switch.allow_new_trades and not self.kill_switch.allow_exit_management:
                return

            now = datetime.utcnow()
            self.rule_live_executor.run_cycle(
                current_time=now,
                equity=self._equity,
                shadow_mode=self.shadow_mode,
                allow_new_trades=self.kill_switch.allow_new_trades,
                allow_exit_management=(
                    self.kill_switch.allow_new_trades or self.kill_switch.allow_exit_management
                ),
            )
        except Exception as e:
            logger.error(f"Rule-based live cycle error: {e}", exc_info=True)

    def _run_rule_based_morning_cycle(self):
        """Run rule-based strategy at market open (9:30 AM ET).

        Uses yesterday's confirmed close data to fire market orders for any
        signal that formed overnight.
        """
        try:
            if not self.kill_switch.allow_new_trades and not self.kill_switch.allow_exit_management:
                return

            now = datetime.utcnow()
            self.rule_live_executor.run_cycle(
                current_time=now,
                equity=self._equity,
                shadow_mode=self.shadow_mode,
                allow_new_trades=self.kill_switch.allow_new_trades,
                allow_exit_management=(
                    self.kill_switch.allow_new_trades or self.kill_switch.allow_exit_management
                ),
                morning_mode=True,
            )
        except Exception as e:
            logger.error(f"Rule-based morning cycle error: {e}", exc_info=True)

    def _run_rule_based_eod_cycle(self):
        """Run rule-based strategy at EOD with real-time quote overlay.

        Called at 3:50-4:00 PM ET.  Overlays current prices onto daily bars
        so that indicators treat the current price as today's close.  Places
        market orders if the market is still open, or extended-hours limit
        orders if it has already closed.
        """
        try:
            if not self.kill_switch.allow_new_trades and not self.kill_switch.allow_exit_management:
                return

            now = datetime.utcnow()
            self.rule_live_executor.run_cycle(
                current_time=now,
                equity=self._equity,
                shadow_mode=self.shadow_mode,
                allow_new_trades=self.kill_switch.allow_new_trades,
                allow_exit_management=(
                    self.kill_switch.allow_new_trades or self.kill_switch.allow_exit_management
                ),
                eod_mode=True,
            )
        except Exception as e:
            logger.error(f"Rule-based EOD cycle error: {e}", exc_info=True)

    # --- Public state for GUI ---

    def get_open_trades(self) -> List[dict]:
        """Get all open trades for display."""
        session = self.db_session_factory()
        try:
            trades = session.query(Trade).filter(
                Trade.status.in_([
                    TradeStatus.OPEN.name,
                    TradeStatus.BREAKEVEN.name,
                    TradeStatus.PENDING_ENTRY.name,
                ])
            ).all()
            return [{
                'id': t.id,
                'symbol': t.symbol,
                'status': t.status,
                'shares': t.shares,
                'entry_price': t.entry_price,
                'current_stop': t.current_stop_price,
                'r_value': t.r_value,
                'breakeven': t.breakeven_set,
                'bot_managed': t.bot_managed,
                'regime_at_entry': t.regime_at_entry,
            } for t in trades]
        finally:
            session.close()

    def get_pending_orders(self) -> List[dict]:
        """Get all working/pending orders for display."""
        session = self.db_session_factory()
        try:
            orders = session.query(BotOrder).filter(
                BotOrder.status.in_(['WORKING', 'PENDING'])
            ).all()
            return [{
                'id': o.id,
                'symbol': o.symbol,
                'side': o.side,
                'type': o.order_type,
                'qty': o.requested_qty,
                'stop_price': o.stop_price,
                'limit_price': o.limit_price,
                'purpose': o.purpose,
                'broker_id': o.broker_order_id,
            } for o in orders]
        finally:
            session.close()

    def get_recent_events(self, limit: int = 50) -> List[dict]:
        """Get recent log events for display."""
        session = self.db_session_factory()
        try:
            events = session.query(Event).order_by(
                Event.timestamp.desc()
            ).limit(limit).all()
            return [{
                'time': e.timestamp.strftime('%H:%M:%S') if e.timestamp else '',
                'type': e.event_type,
                'symbol': e.symbol or '',
                'message': e.message or '',
            } for e in reversed(events)]
        finally:
            session.close()

    def get_daily_pnl(self) -> dict:
        """Get today's PnL summary."""
        session = self.db_session_factory()
        try:
            today = datetime.utcnow().strftime('%Y-%m-%d')
            daily = session.query(DailyPnL).filter_by(date=today).first()
            if daily:
                return {
                    'realized_pnl': daily.realized_pnl or 0,
                    'total_r': daily.total_r or 0,
                    'trades_closed': daily.trades_closed or 0,
                    'locked_out': daily.locked_out or False,
                }
            return {'realized_pnl': 0, 'total_r': 0, 'trades_closed': 0, 'locked_out': False}
        finally:
            session.close()

    def get_closed_trades(self, days: int = 0) -> List[dict]:
        """Get closed trades, optionally filtered to last N days (0=all)."""
        session = self.db_session_factory()
        try:
            query = session.query(Trade).filter(
                Trade.status.in_([
                    TradeStatus.CLOSED_STOP.name,
                    TradeStatus.CLOSED_RULE.name,
                    TradeStatus.CLOSED_EOD.name,
                    TradeStatus.CLOSED_REGIME.name,
                    TradeStatus.CLOSED_MANUAL.name,
                    TradeStatus.CLOSED_KILL.name,
                ])
            )
            if days > 0:
                cutoff = datetime.utcnow() - timedelta(days=days)
                query = query.filter(Trade.exit_time >= cutoff)
            trades = query.order_by(Trade.exit_time.desc()).all()
            return [{
                'id': t.id,
                'symbol': t.symbol,
                'shares': t.shares,
                'entry_price': t.entry_price,
                'exit_price': t.exit_price,
                'entry_time': t.entry_time,
                'exit_time': t.exit_time,
                'exit_reason': t.exit_reason,
                'pnl_dollars': t.pnl_dollars or 0,
                'pnl_r_multiple': t.pnl_r_multiple or 0,
                'r_value': t.r_value,
                'regime_at_entry': t.regime_at_entry,
                'slippage_entry': t.slippage_entry or 0,
                'slippage_exit': t.slippage_exit or 0,
            } for t in trades]
        finally:
            session.close()

    def get_daily_pnl_history(self, days: int = 0) -> List[dict]:
        """Get daily PnL history, optionally filtered to last N days (0=all)."""
        session = self.db_session_factory()
        try:
            query = session.query(DailyPnL).order_by(DailyPnL.date.asc())
            if days > 0:
                cutoff = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%d')
                query = query.filter(DailyPnL.date >= cutoff)
            rows = query.all()
            return [{
                'date': r.date,
                'realized_pnl': r.realized_pnl or 0,
                'unrealized_pnl': r.unrealized_pnl or 0,
                'total_r': r.total_r or 0,
                'trades_opened': r.trades_opened or 0,
                'trades_closed': r.trades_closed or 0,
                'regime': r.regime,
                'locked_out': r.locked_out or False,
            } for r in rows]
        finally:
            session.close()

    def get_regime_history(self, limit: int = 100) -> List[dict]:
        """Get recent regime state history."""
        from data.models import RegimeState
        session = self.db_session_factory()
        try:
            states = session.query(RegimeState).order_by(
                RegimeState.timestamp.desc()
            ).limit(limit).all()
            return [{
                'timestamp': s.timestamp,
                'regime': s.regime,
                'benchmark_close': s.benchmark_close,
                'sma10': s.sma10_value,
                'sma20': s.sma20_value,
                'hit_rate': s.hit_rate,
            } for s in reversed(states)]
        finally:
            session.close()

    def get_performance_metrics(self, days: int = 0) -> dict:
        """Compute trading performance metrics over a time period."""
        import math
        trades = self.get_closed_trades(days)
        daily_pnl = self.get_daily_pnl_history(days)

        if not trades:
            return {
                'total_trades': 0, 'winners': 0, 'losers': 0,
                'win_rate': 0, 'avg_win': 0, 'avg_loss': 0,
                'profit_factor': 0, 'expectancy': 0, 'expectancy_r': 0,
                'total_pnl': 0, 'avg_pnl': 0, 'max_win': 0, 'max_loss': 0,
                'avg_hold_hours': 0, 'total_r': 0, 'avg_r': 0,
                'sharpe': 0, 'sortino': 0, 'calmar': 0,
                'max_drawdown': 0, 'max_drawdown_pct': 0,
                'best_day': 0, 'worst_day': 0,
                'avg_slippage': 0, 'total_slippage': 0,
                'streak_wins': 0, 'streak_losses': 0,
                'by_symbol': {}, 'by_exit_reason': {},
            }

        wins = [t for t in trades if t['pnl_dollars'] > 0]
        losses = [t for t in trades if t['pnl_dollars'] <= 0]
        total_pnl = sum(t['pnl_dollars'] for t in trades)
        gross_wins = sum(t['pnl_dollars'] for t in wins) if wins else 0
        gross_losses = abs(sum(t['pnl_dollars'] for t in losses)) if losses else 0

        win_rate = len(wins) / len(trades) if trades else 0
        avg_win = gross_wins / len(wins) if wins else 0
        avg_loss = gross_losses / len(losses) if losses else 0
        profit_factor = gross_wins / gross_losses if gross_losses > 0 else float('inf')
        expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

        # R-multiple stats
        r_multiples = [t['pnl_r_multiple'] for t in trades if t['pnl_r_multiple'] is not None]
        total_r = sum(r_multiples) if r_multiples else 0
        avg_r = total_r / len(r_multiples) if r_multiples else 0

        # Hold time
        hold_hours = []
        for t in trades:
            if t['entry_time'] and t['exit_time']:
                delta = t['exit_time'] - t['entry_time']
                hold_hours.append(delta.total_seconds() / 3600)
        avg_hold = sum(hold_hours) / len(hold_hours) if hold_hours else 0

        # Slippage
        slippages = [abs(t['slippage_entry']) + abs(t['slippage_exit']) for t in trades]
        total_slippage = sum(slippages)
        avg_slippage = total_slippage / len(trades) if trades else 0

        # Streaks
        max_win_streak = max_loss_streak = cur_win = cur_loss = 0
        for t in reversed(trades):  # chronological order
            if t['pnl_dollars'] > 0:
                cur_win += 1
                cur_loss = 0
            else:
                cur_loss += 1
                cur_win = 0
            max_win_streak = max(max_win_streak, cur_win)
            max_loss_streak = max(max_loss_streak, cur_loss)

        # Daily returns for Sharpe/Sortino/Drawdown
        daily_returns = [d['realized_pnl'] for d in daily_pnl]
        best_day = max(daily_returns) if daily_returns else 0
        worst_day = min(daily_returns) if daily_returns else 0

        sharpe = sortino = calmar = 0.0
        max_dd = max_dd_pct = 0.0

        if len(daily_returns) >= 2:
            mean_ret = sum(daily_returns) / len(daily_returns)
            variance = sum((r - mean_ret) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
            std_ret = math.sqrt(variance) if variance > 0 else 0
            sharpe = (mean_ret / std_ret) * math.sqrt(252) if std_ret > 0 else 0

            neg_returns = [r for r in daily_returns if r < 0]
            if neg_returns:
                neg_var = sum(r ** 2 for r in neg_returns) / len(neg_returns)
                neg_std = math.sqrt(neg_var) if neg_var > 0 else 0
                sortino = (mean_ret / neg_std) * math.sqrt(252) if neg_std > 0 else 0

            # Max drawdown from cumulative PnL
            cumulative = 0
            peak = 0
            for r in daily_returns:
                cumulative += r
                if cumulative > peak:
                    peak = cumulative
                dd = peak - cumulative
                if dd > max_dd:
                    max_dd = dd

            if peak > 0:
                max_dd_pct = max_dd / peak * 100

            annualized_ret = mean_ret * 252
            calmar = annualized_ret / max_dd if max_dd > 0 else 0

        # By symbol breakdown
        by_symbol = {}
        for t in trades:
            sym = t['symbol']
            if sym not in by_symbol:
                by_symbol[sym] = {'trades': 0, 'pnl': 0, 'wins': 0}
            by_symbol[sym]['trades'] += 1
            by_symbol[sym]['pnl'] += t['pnl_dollars']
            if t['pnl_dollars'] > 0:
                by_symbol[sym]['wins'] += 1

        # By exit reason
        by_reason = {}
        for t in trades:
            reason = t['exit_reason'] or 'UNKNOWN'
            if reason not in by_reason:
                by_reason[reason] = {'trades': 0, 'pnl': 0}
            by_reason[reason]['trades'] += 1
            by_reason[reason]['pnl'] += t['pnl_dollars']

        return {
            'total_trades': len(trades),
            'winners': len(wins),
            'losers': len(losses),
            'win_rate': win_rate,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'profit_factor': profit_factor,
            'expectancy': expectancy,
            'expectancy_r': avg_r,
            'total_pnl': total_pnl,
            'avg_pnl': total_pnl / len(trades) if trades else 0,
            'max_win': max(t['pnl_dollars'] for t in trades) if trades else 0,
            'max_loss': min(t['pnl_dollars'] for t in trades) if trades else 0,
            'avg_hold_hours': avg_hold,
            'total_r': total_r,
            'avg_r': avg_r,
            'sharpe': sharpe,
            'sortino': sortino,
            'calmar': calmar,
            'max_drawdown': max_dd,
            'max_drawdown_pct': max_dd_pct,
            'best_day': best_day,
            'worst_day': worst_day,
            'avg_slippage': avg_slippage,
            'total_slippage': total_slippage,
            'streak_wins': max_win_streak,
            'streak_losses': max_loss_streak,
            'by_symbol': by_symbol,
            'by_exit_reason': by_reason,
        }
