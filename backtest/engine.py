"""Day-by-day backtest engine that reuses existing strategy modules."""

import logging
import math
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

from core.config import Config
from core.constants import Regime
from strategy.indicators import (
    sma, highest_high, lowest_low, sma_declining, sma_rising,
)
from strategy.filters import SymbolFilter
from strategy.computed_scanners import (
    passes_selected_scanners,
    scanner_rules_from_config,
    selected_scanner_mask,
)
from backtest.config_overrides import BacktestConfig
from backtest.data_adapter import BacktestDataCache
from backtest.order_simulator import (
    check_breakout_entry, check_stop_loss, check_breakeven_only, check_eod_exit,
)
from backtest.portfolio import Portfolio, BacktestPosition
from backtest.statistics import compute_statistics

logger = logging.getLogger(__name__)


class BacktestRegimeDetector:
    """Regime detector for backtesting.

    Mirrors the live RegimeDetector logic but computes hit rate from
    the portfolio's closed trades instead of querying the DB.
    """

    def __init__(self, data_cache: BacktestDataCache, config: Config):
        self.data = data_cache
        self.config = config
        self._current_regime = Regime.TREND
        self._cooldown_remaining = 0
        self._closed_trades_ref: List[dict] = []

    def set_closed_trades_ref(self, trades_list: List[dict]):
        self._closed_trades_ref = trades_list

    @property
    def current_regime(self) -> Regime:
        return self._current_regime

    def evaluate(self) -> Tuple[Regime, dict]:
        cfg = self.config.regime
        benchmark = cfg.benchmark

        df = self.data.get_daily_bars(benchmark)
        if df is None or len(df) < cfg.sma_long + cfg.decline_consecutive_days + 5:
            return self._current_regime, {"error": "insufficient_data"}

        close = df["close"]
        sma_short = sma(close, cfg.sma_short)
        sma_long_series = sma(close, cfg.sma_long)

        latest_close = close.iloc[-1]
        latest_sma10 = sma_short.iloc[-1]
        latest_sma20 = sma_long_series.iloc[-1]

        short_declining = sma_declining(sma_short)
        long_declining = sma_declining(sma_long_series)
        both_declining = short_declining & long_declining

        short_rising = sma_rising(sma_short)
        long_rising = sma_rising(sma_long_series)
        both_rising = short_rising & long_rising

        decline_count = 0
        for i in range(len(both_declining) - 1, -1, -1):
            if both_declining.iloc[i]:
                decline_count += 1
            else:
                break

        rise_count = 0
        for i in range(len(both_rising) - 1, -1, -1):
            if both_rising.iloc[i]:
                rise_count += 1
            else:
                break

        details = {
            "benchmark": benchmark,
            "close": latest_close,
            "sma10": latest_sma10,
            "sma20": latest_sma20,
            "decline_count": decline_count,
            "rise_count": rise_count,
            "hit_rate": None,
            "cooldown_remaining": self._cooldown_remaining,
        }

        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            details["cooldown_remaining"] = self._cooldown_remaining
            return self._current_regime, details

        new_regime = self._current_regime

        # RISK_OFF check
        is_risk_off_trigger = decline_count >= cfg.decline_consecutive_days
        if cfg.require_close_above_sma20:
            is_risk_off_trigger = is_risk_off_trigger and (latest_close < latest_sma20)

        if is_risk_off_trigger and self._current_regime != Regime.RISK_OFF:
            new_regime = Regime.RISK_OFF

        # RISK_OFF exit
        if self._current_regime == Regime.RISK_OFF:
            is_recovery = rise_count >= cfg.rise_consecutive_days
            if cfg.require_close_above_sma20:
                is_recovery = is_recovery and (latest_close > latest_sma20)
            if is_recovery:
                new_regime = self._determine_trend_or_chop()
            else:
                new_regime = Regime.RISK_OFF

        # TREND vs CHOP
        if new_regime != Regime.RISK_OFF and self._current_regime != Regime.RISK_OFF:
            new_regime = self._determine_trend_or_chop()

        details["hit_rate"] = self._compute_hit_rate()

        if new_regime != self._current_regime:
            self._current_regime = new_regime
            self._cooldown_remaining = cfg.cooldown_days

        return self._current_regime, details

    def _determine_trend_or_chop(self) -> Regime:
        hit_rate = self._compute_hit_rate()
        if hit_rate is not None and hit_rate >= self.config.regime.trend_hit_rate_threshold:
            return Regime.TREND
        return Regime.CHOP

    def _compute_hit_rate(self) -> Optional[float]:
        window = self.config.regime.hit_rate_window
        trades = self._closed_trades_ref[-window:] if self._closed_trades_ref else []
        if len(trades) < 5:
            return None
        winners = sum(1 for t in trades if t.get("pnl_r", 0) >= 1.0)
        return winners / len(trades)


class BacktestEngine:
    """Runs a day-by-day simulation of the momentum breakout strategy."""

    def __init__(
        self,
        config: Config,
        bt_config: BacktestConfig,
        data_cache: BacktestDataCache,
    ):
        self.config = config
        self.bt_config = bt_config
        self.data_cache = data_cache

        self.portfolio = Portfolio(bt_config.starting_capital)
        self.filters = SymbolFilter(data_cache, config)
        self.regime_detector = BacktestRegimeDetector(data_cache, config)
        self.regime_detector.set_closed_trades_ref(self.portfolio._closed_trades)

        self.current_regime = Regime.TREND
        self.regime_details: dict = {}
        self._cancelled = False
        self._daily_loss_r = 0.0

        # Cooldown tracking: symbol -> date when re-entry allowed
        self._reentry_cooldown: Dict[str, datetime] = {}
        self._computed_scanner_prepared = False
        self._computed_scanner_day_cache: Dict[pd.Timestamp, List[str]] = {}
        self._scanner_rules = scanner_rules_from_config(config.universe)

        # Callbacks for GUI
        self.on_progress: Optional[Callable[[int, int, str], None]] = None

    def cancel(self):
        self._cancelled = True

    def run(self) -> dict:
        """Execute the full backtest. Returns summary dict."""
        start = pd.Timestamp(self.bt_config.start_date)
        end = pd.Timestamp(self.bt_config.end_date)

        trading_dates = self.data_cache.get_trading_dates(
            start, end, reference_symbol=self.bt_config.benchmark
        )
        if not trading_dates:
            return {"error": "No trading dates found"}

        total_days = len(trading_dates)
        symbols = self.bt_config.symbols
        benchmark = self.bt_config.benchmark

        logger.info(
            f"Backtest: {self.bt_config.start_date} to {self.bt_config.end_date}, "
            f"{total_days} trading days, {len(symbols)} symbols"
        )

        if self.bt_config.use_computed_scanners:
            self._build_computed_scanner_universe(trading_dates)

        for day_idx, current_date in enumerate(trading_dates):
            if self._cancelled:
                logger.info("Backtest cancelled by user")
                break

            # 1. Advance simulation clock
            self.data_cache.set_current_date(current_date)

            # 2. Evaluate regime
            self.current_regime, self.regime_details = self.regime_detector.evaluate()

            # 2b. Resolve today's entry universe
            day_symbols = self._get_symbols_for_day(current_date)

            # 3. Get today's bars for all relevant symbols
            all_symbols = list(set(list(self.portfolio.positions.keys()) + day_symbols + [benchmark]))
            today_bars = {}
            close_prices = {}
            for sym in all_symbols:
                df = self.data_cache.get_daily_bars(sym, min_bars=1)
                if df is not None and not df.empty:
                    last_row = df.iloc[-1]
                    if pd.Timestamp(last_row["datetime"]).normalize() == pd.Timestamp(current_date).normalize():
                        today_bars[sym] = last_row
                        close_prices[sym] = last_row["close"]

            # 4. Reset daily loss tracking
            self._daily_loss_r = 0.0

            # 5. Handle RISK_OFF liquidation
            if self.current_regime == Regime.RISK_OFF and self.portfolio.open_position_count > 0:
                self._liquidate_all(current_date, today_bars, "RISK_OFF")

            # 6. Manage existing positions (stops, breakeven, EOD)
            self._manage_positions(current_date, today_bars)

            # 7. Check for new entries (skip if RISK_OFF or daily loss locked)
            daily_locked = self._daily_loss_r >= self.config.safety.daily_loss_lock_r
            if self.current_regime != Regime.RISK_OFF and not daily_locked:
                self._scan_for_entries(current_date, day_symbols, today_bars)

            # 8. Record daily equity
            self.portfolio.record_equity(current_date, close_prices, self.current_regime.value)

            if self.on_progress:
                self.on_progress(day_idx + 1, total_days, current_date.strftime("%Y-%m-%d"))

        return compute_statistics(self.portfolio, self.bt_config)

    def _build_computed_scanner_universe(self, trading_dates: List[datetime]):
        if self._computed_scanner_prepared:
            return

        selected_scanners = self.bt_config.scanner_names or self.config.universe.scanner_names
        if not selected_scanners:
            logger.warning("Computed scanner mode enabled but no scanner names were configured")
            self._computed_scanner_prepared = True
            return

        trading_day_set = {pd.Timestamp(dt).normalize() for dt in trading_dates}
        day_to_symbols: Dict[pd.Timestamp, List[str]] = {}
        total_symbols = len(self.bt_config.symbols)

        for idx, symbol in enumerate(self.bt_config.symbols, start=1):
            if idx % 500 == 0:
                logger.info("Computed scanner precompute: %d/%d symbols", idx, total_symbols)

            df = self.data_cache.get_full_bars(symbol)
            if df is None or df.empty or "datetime" not in df.columns:
                continue

            mask = selected_scanner_mask(df, selected_scanners, self._scanner_rules)
            if mask.empty or not bool(mask.any()):
                continue

            hit_days = pd.to_datetime(df.loc[mask, "datetime"]).dt.normalize().unique()
            for hit_day in hit_days:
                day = pd.Timestamp(hit_day)
                if day in trading_day_set:
                    day_to_symbols.setdefault(day, []).append(symbol)

        for day, symbols_for_day in day_to_symbols.items():
            self._computed_scanner_day_cache[day] = sorted(set(symbols_for_day))

        logger.info(
            "Computed scanner universe prepared for %d trading days",
            len(self._computed_scanner_day_cache),
        )
        self._computed_scanner_prepared = True

    def _get_symbols_for_day(self, current_date: datetime) -> List[str]:
        if self.bt_config.use_scanner_history and self.bt_config.scanner_history:
            return self.bt_config.scanner_history.get_symbols_for_date(
                current_date,
                carry_forward_days=self.bt_config.scanner_carry_forward_days,
            )

        if self.bt_config.use_computed_scanners:
            day = pd.Timestamp(current_date).normalize()
            if self._computed_scanner_prepared:
                return self._computed_scanner_day_cache.get(day, [])

            if day in self._computed_scanner_day_cache:
                return self._computed_scanner_day_cache[day]

            selected = []
            scanner_names = self.bt_config.scanner_names or self.config.universe.scanner_names
            for symbol in self.bt_config.symbols:
                df = self.data_cache.get_daily_bars(symbol, min_bars=1)
                if df is None or df.empty:
                    continue
                last_dt = pd.Timestamp(df.iloc[-1]["datetime"]).normalize()
                if last_dt != day:
                    continue
                if passes_selected_scanners(df, scanner_names, self._scanner_rules):
                    selected.append(symbol)

            selected = sorted(set(selected))
            self._computed_scanner_day_cache[day] = selected
            return selected

        return self.bt_config.symbols

    def _manage_positions(self, current_date: datetime, today_bars: dict):
        for symbol in list(self.portfolio.positions.keys()):
            if symbol not in today_bars:
                continue

            bar = today_bars[symbol]
            pos = self.portfolio.positions[symbol]

            # Check stop-loss (with breakeven check built in)
            stop_result = check_stop_loss(bar, pos, self.bt_config)
            if stop_result.filled:
                reason = "BREAKEVEN_STOP" if pos.breakeven_set else "STOP_LOSS"
                result = self.portfolio.close_position(symbol, stop_result.fill_price, current_date, reason)
                if result and result["pnl_r"] < 0:
                    self._daily_loss_r += abs(result["pnl_r"])
                # Set re-entry cooldown
                self._reentry_cooldown[symbol] = pd.Timestamp(current_date) + pd.Timedelta(
                    days=self.config.app.reentry_cooldown_days
                )
                continue

            # Breakeven check (if stop wasn't hit)
            if not pos.breakeven_set:
                check_breakeven_only(bar, pos)

            # EOD exit: check close vs SMA10/SMA20
            df = self.data_cache.get_daily_bars(symbol, min_bars=20)
            if df is not None and len(df) >= 20:
                close_series = df["close"]
                sma10 = sma(close_series, 10)
                sma20 = sma(close_series, 20)
                if pd.notna(sma10.iloc[-1]) and pd.notna(sma20.iloc[-1]):
                    eod_result = check_eod_exit(
                        bar["close"],
                        sma10.iloc[-1],
                        sma20.iloc[-1],
                        self.config.eod_exit.sma_closeness_threshold,
                    )
                    if eod_result.filled:
                        result = self.portfolio.close_position(
                            symbol, eod_result.fill_price, current_date, "EOD_EXIT"
                        )
                        if result and result["pnl_r"] < 0:
                            self._daily_loss_r += abs(result["pnl_r"])
                        self._reentry_cooldown[symbol] = pd.Timestamp(current_date) + pd.Timedelta(
                            days=self.config.app.reentry_cooldown_days
                        )

    def _scan_for_entries(self, current_date: datetime, symbols: List[str], today_bars: dict):
        benchmark_df = self.data_cache.get_daily_bars(self.bt_config.benchmark)

        cfg = self.config.sizing
        max_pos = cfg.max_positions_trend if self.current_regime == Regime.TREND else cfg.max_positions_chop
        if self.portfolio.open_position_count >= max_pos:
            return

        for symbol in symbols:
            if symbol in self.portfolio.positions:
                continue

            # Re-entry cooldown
            if symbol in self._reentry_cooldown:
                if pd.Timestamp(current_date) < self._reentry_cooldown[symbol]:
                    continue

            if symbol not in today_bars:
                continue

            bar = today_bars[symbol]

            # Synthesize a quote for the filter
            quote = self.data_cache.get_quote(symbol)
            if not quote:
                continue

            # Apply filters (reuses existing SymbolFilter)
            passed, reason = self.filters.apply_all_filters(symbol, quote, benchmark_df)
            if not passed:
                continue

            # Compute breakout level from yesterday's data (no look-ahead)
            df = self.data_cache.get_daily_bars(symbol)
            if df is None or len(df) < self.config.breakout.lookback + 1:
                continue

            yesterday_df = df.iloc[:-1]
            bl = highest_high(yesterday_df["high"], self.config.breakout.lookback).iloc[-1]
            if pd.isna(bl):
                continue

            avg_vol = yesterday_df["volume"].tail(20).mean()

            # Check breakout on today's bar
            fill_result = check_breakout_entry(
                bar, bl, self.config.breakout.entry_buffer_pct, avg_vol, self.bt_config,
            )
            if not fill_result.filled:
                continue

            # Compute stop price
            base_low = lowest_low(yesterday_df["low"], self.config.breakout.lookback).iloc[-1]
            if pd.isna(base_low):
                continue
            stop_buffer = self.config.stop.stop_buffer_pct / 100
            stop_price = base_low * (1 - stop_buffer)

            entry_price = fill_result.fill_price
            r_value = entry_price - stop_price
            if r_value <= 0:
                continue

            # Size the position
            equity = self.portfolio.total_equity(
                {s: today_bars[s]["close"] for s in today_bars}
            )
            shares = self._calculate_shares(entry_price, stop_price, equity)
            if shares <= 0:
                continue

            # Check max positions again
            if self.portfolio.open_position_count >= max_pos:
                break

            pos = BacktestPosition(
                symbol=symbol,
                shares=shares,
                entry_price=entry_price,
                entry_date=current_date,
                initial_stop=stop_price,
                current_stop=stop_price,
                r_value=r_value,
                regime_at_entry=self.current_regime.value,
                signal_type="BREAKOUT",
            )
            self.portfolio.open_position(pos)

    def _calculate_shares(self, entry: float, stop: float, equity: float) -> int:
        """Position sizing: same math as PositionSizer.calculate()."""
        cfg = self.config.sizing
        risk_pct = cfg.risk_per_trade_pct

        size_mult = 1.0
        if self.current_regime == Regime.CHOP:
            size_mult = self.config.regime.chop_size_multiplier

        per_share_risk = entry - stop
        if per_share_risk <= 0:
            return 0

        risk_dollars = equity * (risk_pct / 100) * size_mult
        shares = math.floor(risk_dollars / per_share_risk)

        if shares <= 0:
            return 0

        # Max position % of equity
        position_value = shares * entry
        max_position_value = equity * (cfg.max_position_pct_equity / 100)
        if position_value > max_position_value:
            shares = math.floor(max_position_value / entry)

        # Max total open risk
        current_risk_r = self.portfolio.total_open_risk_r()
        if current_risk_r + 1.0 > cfg.max_total_open_risk_r:
            return 0

        return max(0, shares)

    def _liquidate_all(self, current_date: datetime, today_bars: dict, reason: str):
        for symbol in list(self.portfolio.positions.keys()):
            if symbol in today_bars:
                exit_price = today_bars[symbol]["close"]
            else:
                exit_price = self.portfolio.positions[symbol].entry_price
            self.portfolio.close_position(symbol, exit_price, current_date, reason)
