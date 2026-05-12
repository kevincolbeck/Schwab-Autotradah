"""Generic rule-based strategy backtester for AI-authored strategy specs."""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

from backtest.config_overrides import BacktestConfig
from backtest.data_adapter import BacktestDataCache
from backtest.portfolio import BacktestPosition, Portfolio
from backtest.statistics import compute_statistics
from core.config import Config

logger = logging.getLogger(__name__)

_DISALLOWED_EXPR_TOKENS = (
    "__",
    "import ",
    "exec(",
    "eval(",
    "open(",
    "os.",
    "sys.",
    "subprocess",
    "lambda ",
)
_PRICE_FIELDS = ("open", "high", "low", "close", "volume")
_AUTO_US_UNIVERSE_TOKENS = {
    "*",
    "ALL",
    "ALL_US",
    "ALL_US_SYMBOLS",
    "AUTO_US",
    "__AUTO_US__",
    "US_ALL",
    "US_MARKET",
    "ENTIRE_US_MARKET",
}
_INTRADAY_BACKTEST_INTERVALS = {"1m", "2m", "5m", "15m", "30m", "60m", "90m"}


@dataclass
class RuleBasedStrategySpec:
    """JSON spec describing a reusable backtest strategy."""

    name: str = "AI Strategy"
    description: str = ""
    symbols: List[str] = field(default_factory=list)
    indicators: List[dict] = field(default_factory=list)
    entry_rule: str = ""  # Legacy alias for long entry rule.
    entry_rule_long: str = ""
    entry_rule_short: str = ""
    exit_rule: str = "False"
    entry_price_field: str = "close"  # open | high | low | close
    use_intraday_vwap_stop: bool = False
    intraday_interval: str = "5m"
    backtest_timeframe: str = "1d"  # 1d or minute interval (1m..90m)
    position_size_mode: str = "notional_pct"  # notional_pct | risk_pct
    position_size_pct: float = 10.0
    risk_per_trade_pct: float = 0.75
    max_positions: int = 1
    stop_loss_pct: float = 0.0
    stop_loss_field: str = ""  # indicator column for dynamic stop distance (e.g. "atr_20")
    exit_rule_after_tp: bool = False  # only evaluate exit_rule after a TP rule has triggered
    take_profit_pct: float = 0.0
    take_profit_rules: List[dict] = field(default_factory=list)
    max_holding_days: int = 0
    # Regime detection & risk management
    regime_hit_rate_window: int = 20
    regime_trend_threshold: float = 0.0  # 0 = disabled (always TREND)
    regime_chop_size_multiplier: float = 1.0
    max_positions_trend: int = 0  # 0 = use max_positions
    max_positions_chop: int = 0   # 0 = use max_positions
    daily_loss_lock_r: float = 0.0  # 0 = disabled
    max_total_open_risk_r: float = 0.0  # 0 = disabled
    ranking_field: str = ""  # indicator column for entry ranking (e.g. "momentum_score"); empty = volume ratio

    @staticmethod
    def from_dict(raw: dict) -> "RuleBasedStrategySpec":
        if not isinstance(raw, dict):
            raise ValueError("Strategy spec must be an object")

        raw_symbols = raw.get("symbols", [])
        if raw_symbols is None:
            raw_symbols = []
        if isinstance(raw_symbols, str):
            raw_symbols = [raw_symbols]
        if not isinstance(raw_symbols, list):
            raise ValueError("symbols must be a list of ticker strings")

        symbols = [str(s).strip().upper() for s in raw_symbols if str(s).strip()]
        symbols = list(dict.fromkeys(symbols))
        if not symbols:
            raise ValueError("Strategy spec requires at least one symbol")
        if not _trade_symbols_from_list(symbols) and not _list_requests_auto_us(symbols):
            raise ValueError(
                "Strategy spec symbols must include at least one ticker or an ALL_US token"
            )

        entry_rule_legacy = str(raw.get("entry_rule") or "").strip()
        entry_rule_long = str(raw.get("entry_rule_long") or entry_rule_legacy).strip()
        entry_rule_short = str(raw.get("entry_rule_short") or "").strip()
        if not entry_rule_long and not entry_rule_short:
            raise ValueError("Strategy spec requires entry_rule or entry_rule_long/entry_rule_short")

        exit_rule = str(raw.get("exit_rule") or "False").strip() or "False"
        entry_price_field = str(raw.get("entry_price_field") or "close").strip().lower()
        use_intraday_vwap_stop = _to_bool(
            raw.get("use_intraday_vwap_stop", False), default=False
        )
        intraday_interval = str(raw.get("intraday_interval") or "5m").strip().lower()
        backtest_timeframe = str(raw.get("backtest_timeframe") or "1d").strip().lower()
        indicators = raw.get("indicators") or []
        if not isinstance(indicators, list):
            raise ValueError("indicators must be a list")

        position_size_mode = str(raw.get("position_size_mode") or "notional_pct").strip().lower()
        position_size_pct = _to_float(raw.get("position_size_pct"), 10.0, "position_size_pct")
        risk_per_trade_pct = _to_float(raw.get("risk_per_trade_pct"), 0.75, "risk_per_trade_pct")
        max_positions = _to_int(raw.get("max_positions"), max(1, len(symbols)), "max_positions")
        stop_loss_pct = _to_float(raw.get("stop_loss_pct"), 0.0, "stop_loss_pct")
        stop_loss_field = str(raw.get("stop_loss_field") or "").strip().lower()
        exit_rule_after_tp = _to_bool(raw.get("exit_rule_after_tp", False), default=False)
        take_profit_pct = _to_float(raw.get("take_profit_pct"), 0.0, "take_profit_pct")
        take_profit_rules = raw.get("take_profit_rules") or []
        if not isinstance(take_profit_rules, list):
            take_profit_rules = []
        max_holding_days = _to_int(raw.get("max_holding_days"), 0, "max_holding_days")
        regime_hit_rate_window = _to_int(raw.get("regime_hit_rate_window"), 20, "regime_hit_rate_window")
        regime_trend_threshold = _to_float(raw.get("regime_trend_threshold"), 0.0, "regime_trend_threshold")
        regime_chop_size_multiplier = _to_float(raw.get("regime_chop_size_multiplier"), 1.0, "regime_chop_size_multiplier")
        max_positions_trend = _to_int(raw.get("max_positions_trend"), 0, "max_positions_trend")
        max_positions_chop = _to_int(raw.get("max_positions_chop"), 0, "max_positions_chop")
        daily_loss_lock_r = _to_float(raw.get("daily_loss_lock_r"), 0.0, "daily_loss_lock_r")
        max_total_open_risk_r = _to_float(raw.get("max_total_open_risk_r"), 0.0, "max_total_open_risk_r")
        ranking_field = str(raw.get("ranking_field") or "").strip().lower()

        if position_size_mode not in {"notional_pct", "risk_pct"}:
            raise ValueError("position_size_mode must be 'notional_pct' or 'risk_pct'")
        if not entry_price_field:
            raise ValueError("entry_price_field must not be empty")
        if intraday_interval not in {"1m", "2m", "5m", "15m", "30m", "60m", "90m"}:
            raise ValueError("intraday_interval must be a supported minute interval")
        if backtest_timeframe not in {"1d", *sorted(_INTRADAY_BACKTEST_INTERVALS)}:
            raise ValueError("backtest_timeframe must be 1d or a supported minute interval")
        if position_size_pct <= 0 or position_size_pct > 300:
            raise ValueError("position_size_pct must be in (0, 300]")
        if risk_per_trade_pct <= 0 or risk_per_trade_pct > 100:
            raise ValueError("risk_per_trade_pct must be in (0, 100]")
        if max_positions <= 0:
            raise ValueError("max_positions must be >= 1")
        if stop_loss_pct < 0 or take_profit_pct < 0:
            raise ValueError("stop_loss_pct/take_profit_pct must be >= 0")
        if max_holding_days < 0:
            raise ValueError("max_holding_days must be >= 0")

        return RuleBasedStrategySpec(
            name=str(raw.get("name") or "AI Strategy").strip() or "AI Strategy",
            description=str(raw.get("description") or "").strip(),
            symbols=symbols,
            indicators=indicators,
            entry_rule=entry_rule_long,
            entry_rule_long=entry_rule_long,
            entry_rule_short=entry_rule_short,
            exit_rule=exit_rule,
            entry_price_field=entry_price_field,
            use_intraday_vwap_stop=use_intraday_vwap_stop,
            intraday_interval=intraday_interval,
            backtest_timeframe=backtest_timeframe,
            position_size_mode=position_size_mode,
            position_size_pct=position_size_pct,
            risk_per_trade_pct=risk_per_trade_pct,
            max_positions=max_positions,
            stop_loss_pct=stop_loss_pct,
            stop_loss_field=stop_loss_field,
            exit_rule_after_tp=exit_rule_after_tp,
            take_profit_pct=take_profit_pct,
            take_profit_rules=take_profit_rules,
            max_holding_days=max_holding_days,
            regime_hit_rate_window=regime_hit_rate_window,
            regime_trend_threshold=regime_trend_threshold,
            regime_chop_size_multiplier=regime_chop_size_multiplier,
            max_positions_trend=max_positions_trend,
            max_positions_chop=max_positions_chop,
            daily_loss_lock_r=daily_loss_lock_r,
            max_total_open_risk_r=max_total_open_risk_r,
            ranking_field=ranking_field,
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "symbols": self.symbols,
            "indicators": self.indicators,
            "entry_rule": self.entry_rule_long or self.entry_rule,
            "entry_rule_long": self.entry_rule_long,
            "entry_rule_short": self.entry_rule_short,
            "exit_rule": self.exit_rule,
            "entry_price_field": self.entry_price_field,
            "use_intraday_vwap_stop": self.use_intraday_vwap_stop,
            "intraday_interval": self.intraday_interval,
            "backtest_timeframe": self.backtest_timeframe,
            "position_size_mode": self.position_size_mode,
            "position_size_pct": self.position_size_pct,
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "max_positions": self.max_positions,
            "stop_loss_pct": self.stop_loss_pct,
            "stop_loss_field": self.stop_loss_field,
            "exit_rule_after_tp": self.exit_rule_after_tp,
            "take_profit_pct": self.take_profit_pct,
            "take_profit_rules": self.take_profit_rules,
            "max_holding_days": self.max_holding_days,
            "regime_hit_rate_window": self.regime_hit_rate_window,
            "regime_trend_threshold": self.regime_trend_threshold,
            "regime_chop_size_multiplier": self.regime_chop_size_multiplier,
            "max_positions_trend": self.max_positions_trend,
            "max_positions_chop": self.max_positions_chop,
            "daily_loss_lock_r": self.daily_loss_lock_r,
            "max_total_open_risk_r": self.max_total_open_risk_r,
            "ranking_field": self.ranking_field,
        }


def symbols_from_strategy_spec(raw_spec: Optional[dict]) -> List[str]:
    if not isinstance(raw_spec, dict):
        return []
    raw_symbols = raw_spec.get("symbols", [])
    if raw_symbols is None:
        return []
    if isinstance(raw_symbols, str):
        raw_symbols = [raw_symbols]
    if not isinstance(raw_symbols, list):
        return []
    return [
        str(s).strip().upper()
        for s in raw_symbols
        if str(s).strip()
        and not _is_auto_us_symbol_token(str(s))
    ]


def trade_symbols_from_strategy_spec(spec: RuleBasedStrategySpec) -> List[str]:
    return _trade_symbols_from_list(spec.symbols)


def strategy_spec_uses_auto_us_universe(spec_or_raw) -> bool:
    if isinstance(spec_or_raw, RuleBasedStrategySpec):
        return _list_requests_auto_us(spec_or_raw.symbols)
    if isinstance(spec_or_raw, dict):
        raw_symbols = spec_or_raw.get("symbols", [])
        if isinstance(raw_symbols, str):
            raw_symbols = [raw_symbols]
        if not isinstance(raw_symbols, list):
            return False
        return _list_requests_auto_us(raw_symbols)
    return False


def referenced_price_symbols(spec: RuleBasedStrategySpec) -> List[str]:
    """Extract symbols referenced as <symbol>_<field> in entry/exit expressions
    AND symbols declared in external-type indicators."""
    expr = " ".join(
        [
            spec.entry_rule or "",
            spec.entry_rule_long or "",
            spec.entry_rule_short or "",
            spec.exit_rule or "",
        ]
    ).lower()
    pattern = re.compile(r"\b([a-z][a-z0-9]{0,9})_(open|high|low|close|volume)\b")
    out = set()
    for prefix, _field in pattern.findall(expr):
        if prefix in _PRICE_FIELDS:
            continue
        out.add(prefix.upper())
    # Also include symbols from external-type indicators.
    for ind in (spec.indicators or []):
        if str(ind.get("type", "")).strip().lower() == "external":
            sym = str(ind.get("symbol", "")).strip().upper()
            if sym:
                out.add(sym)
    return sorted(out)


def save_strategy_spec(raw_spec: dict, path: str):
    spec = RuleBasedStrategySpec.from_dict(raw_spec)
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(spec.to_dict(), indent=2), encoding="utf-8")


def load_strategy_spec(bt_config: BacktestConfig) -> RuleBasedStrategySpec:
    # Prefer file path when present so manual edits to working_strategy.json
    # are picked up on re-runs instead of stale in-memory specs.
    if bt_config.strategy_spec_path:
        path = Path(bt_config.strategy_spec_path)
        if not path.exists():
            if isinstance(bt_config.strategy_spec, dict):
                return RuleBasedStrategySpec.from_dict(bt_config.strategy_spec)
            raise ValueError(f"Strategy spec file not found: {bt_config.strategy_spec_path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return RuleBasedStrategySpec.from_dict(raw)
        except Exception:
            if isinstance(bt_config.strategy_spec, dict):
                return RuleBasedStrategySpec.from_dict(bt_config.strategy_spec)
            raise

    if isinstance(bt_config.strategy_spec, dict):
        return RuleBasedStrategySpec.from_dict(bt_config.strategy_spec)

    raise ValueError("Rule-based strategy mode enabled but no strategy_spec provided")


class RuleBasedBacktestEngine:
    """Executes user/AI-authored strategy rules over daily OHLCV data."""

    def __init__(
        self,
        config: Config,
        bt_config: BacktestConfig,
        data_cache: BacktestDataCache,
        strategy_spec: RuleBasedStrategySpec,
    ):
        self.config = config
        self.bt_config = bt_config
        self.data_cache = data_cache
        self.spec = strategy_spec
        self.portfolio = Portfolio(bt_config.starting_capital)
        self.on_progress: Optional[Callable[[int, int, str], None]] = None
        self._frames: Dict[str, pd.DataFrame] = {}
        self._last_close: Dict[str, float] = {}
        self._trade_symbols: List[str] = []
        self._cancelled = False
        self._current_regime: str = "TREND"
        self._daily_loss_r: float = 0.0
        self._current_day: Optional[datetime] = None

    def cancel(self):
        self._cancelled = True

    def run(self) -> dict:
        start = pd.Timestamp(self.bt_config.start_date)
        end = pd.Timestamp(self.bt_config.end_date)
        self._prepare_symbol_frames(start, end)

        if self._cancelled:
            logger.info("Rule-based backtest cancelled during preparation")
        if not self._frames:
            if self._cancelled:
                return {"error": "Backtest cancelled before any symbols qualified"}
            return {"error": "No data available for strategy symbols"}

        if self._is_intraday_mode():
            return self._run_intraday_backtest(start, end)

        return self._run_daily_backtest(start, end)

    def _run_daily_backtest(self, start: pd.Timestamp, end: pd.Timestamp) -> dict:
        trading_dates = self._collect_trading_dates(start, end)
        if not trading_dates:
            return {"error": "No trading dates available for strategy"}

        total_days = len(trading_dates)
        idx = 0

        for idx, current_date in enumerate(trading_dates):
            if self._cancelled:
                logger.info("Rule-based backtest cancelled by user")
                break

            self.data_cache.set_current_date(current_date)
            if self._current_day != current_date:
                self._daily_loss_r = 0.0
                self._current_day = current_date
            self._update_last_close_snapshot(current_date)
            self._manage_open_positions(current_date)
            self._scan_entries(current_date)
            self._manage_open_positions(current_date)

            close_prices = {
                sym: self._last_close.get(sym, pos.entry_price)
                for sym, pos in self.portfolio.positions.items()
            }
            self.portfolio.record_equity(current_date, close_prices, "CUSTOM")

            if self.on_progress:
                self.on_progress(idx + 1, total_days, current_date.strftime("%Y-%m-%d"))

        if trading_dates:
            last_date = trading_dates[min(idx, len(trading_dates) - 1)] if self._cancelled else trading_dates[-1]
            self._liquidate_at_end(last_date)

        return compute_statistics(self.portfolio, self.bt_config)

    def _run_intraday_backtest(self, start: pd.Timestamp, end: pd.Timestamp) -> dict:
        trading_times = self._collect_trading_timestamps(start, end)
        if not trading_times:
            return {"error": "No intraday timestamps available for strategy"}

        trading_days = sorted({pd.Timestamp(ts).normalize() for ts in trading_times})
        total_days = len(trading_days)
        completed_days = 0
        idx = 0
        current_day_key: Optional[pd.Timestamp] = None

        for idx, current_time in enumerate(trading_times):
            if self._cancelled:
                logger.info("Rule-based intraday backtest cancelled by user")
                break

            ts = pd.Timestamp(current_time)
            day_key = ts.normalize()

            self.data_cache.set_current_date(day_key)
            if self._current_day != day_key.to_pydatetime():
                self._daily_loss_r = 0.0
                self._current_day = day_key.to_pydatetime()

            self._update_last_close_snapshot(ts.to_pydatetime())
            self._manage_open_positions(ts.to_pydatetime())
            self._scan_entries(ts.to_pydatetime())
            self._manage_open_positions(ts.to_pydatetime())

            is_day_end = (
                idx == len(trading_times) - 1
                or pd.Timestamp(trading_times[idx + 1]).normalize() != day_key
            )
            if is_day_end:
                close_prices = {
                    sym: self._last_close.get(sym, pos.entry_price)
                    for sym, pos in self.portfolio.positions.items()
                }
                self.portfolio.record_equity(day_key.to_pydatetime(), close_prices, "CUSTOM")
                completed_days += 1
                current_day_key = day_key
                if self.on_progress:
                    self.on_progress(completed_days, total_days, day_key.strftime("%Y-%m-%d"))

        if trading_times:
            if self._cancelled:
                final_time = trading_times[min(idx, len(trading_times) - 1)]
            else:
                final_time = trading_times[-1]
            self._liquidate_at_end(final_time)
            final_day = pd.Timestamp(final_time).normalize()
            # Ensure last day's equity snapshot exists even if run is cancelled mid-session.
            if current_day_key is None or current_day_key != final_day:
                close_prices = {
                    sym: self._last_close.get(sym, pos.entry_price)
                    for sym, pos in self.portfolio.positions.items()
                }
                self.portfolio.record_equity(final_day.to_pydatetime(), close_prices, "CUSTOM")

        return compute_statistics(self.portfolio, self.bt_config)

    def _prepare_symbol_frames(self, start: pd.Timestamp, end: pd.Timestamp):
        """Single-pass tiered filter: eliminate symbols tier-by-tier, compute
        full indicator frames only for survivors."""
        self._frames = {}
        self._trade_symbols = self._resolve_trade_symbols()
        intraday_mode = self._is_intraday_mode()
        ref_frames = self._build_reference_frames(start, end, intraday=intraday_mode)

        pipeline = _build_tiered_pipeline(
            self.spec.indicators, self.spec.entry_rule_long
        )

        candidates = self._trade_symbols
        initial_count = len(candidates)

        for symbol in candidates:
            if self._cancelled:
                break

            if intraday_mode:
                bars = self.data_cache.get_intraday_bars(symbol, session_date=None)
            else:
                bars = self.data_cache.get_full_bars(symbol)
            if bars is None or bars.empty:
                continue

            df = bars.copy()
            if "datetime" not in df.columns:
                continue

            df["datetime"] = pd.to_datetime(df["datetime"])
            if intraday_mode:
                end_ts = end + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
                df = df[(df["datetime"] >= start) & (df["datetime"] <= end_ts)].copy()
                if df.empty:
                    continue
                df["session_date"] = df["datetime"].dt.normalize()
            else:
                df["datetime"] = df["datetime"].dt.normalize()
                df = df[(df["datetime"] >= start) & (df["datetime"] <= end)].copy()
            if df.empty:
                continue

            # Merge reference symbol data (SPY, etc.)
            for ref_df in ref_frames:
                df = df.merge(ref_df, on="datetime", how="left")

            frame = df.sort_values("datetime").reset_index(drop=True)

            if intraday_mode:
                # Session VWAP used for intraday trailing exits when enabled.
                typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
                cum_vol = frame.groupby("session_date")["volume"].cumsum()
                frame["session_vwap"] = (typical * frame["volume"]).groupby(frame["session_date"]).cumsum() / cum_vol.replace(0, np.nan)

            eliminated = False

            # Walk through tiers: compute indicators, check conditions, bail early
            for stage in pipeline:
                for ind in stage["indicators"]:
                    try:
                        self._apply_indicator(frame, ind)
                    except Exception as exc:
                        logger.warning("Indicator skipped (%s): %s", ind, exc)

                # If any condition is never True across entire history,
                # skip this symbol.  Require >= 200 rows to avoid false
                # elimination on short test data with all-NaN indicators.
                if len(frame) >= 200:
                    for cond in stage["conditions"]:
                        try:
                            result = _evaluate_rule_series(cond, frame)
                            if not result.any():
                                eliminated = True
                                break
                        except Exception:
                            pass
                if eliminated:
                    break

            if eliminated:
                continue

            # Symbol survived all tiers — evaluate full entry rule
            frame["entry_signal_long"] = _evaluate_rule_series(self.spec.entry_rule_long, frame)
            if (self.spec.entry_rule_short or "").strip():
                frame["entry_signal_short"] = _evaluate_rule_series(self.spec.entry_rule_short, frame)
            else:
                frame["entry_signal_short"] = pd.Series(False, index=frame.index)
            frame = frame.set_index("datetime", drop=False)
            self._frames[symbol] = frame

        skipped = initial_count - len(self._frames)
        logger.info(
            "Tiered filter: %d/%d symbols survived (%d eliminated)",
            len(self._frames), initial_count, skipped,
        )

    def _resolve_trade_symbols(self) -> List[str]:
        explicit = trade_symbols_from_strategy_spec(self.spec)
        if explicit:
            return explicit

        # ALL_US mode fallback: derive tradable symbols from bt_config symbols
        ref_symbols = set(referenced_price_symbols(self.spec))
        benchmark = (self.bt_config.benchmark or "").strip().upper()
        out = []
        seen = set()
        for sym in self.bt_config.symbols:
            symbol = str(sym).strip().upper()
            if not symbol:
                continue
            if symbol == benchmark or symbol in ref_symbols:
                continue
            if _is_auto_us_symbol_token(symbol):
                continue
            if symbol not in seen:
                seen.add(symbol)
                out.append(symbol)
        return out

    def _build_reference_frames(
        self, start: pd.Timestamp, end: pd.Timestamp, intraday: bool = False
    ) -> List[pd.DataFrame]:
        symbols = set(referenced_price_symbols(self.spec))
        benchmark = (self.bt_config.benchmark or "").strip().upper()
        if benchmark:
            symbols.add(benchmark)

        out: List[pd.DataFrame] = []
        for symbol in sorted(symbols):
            if intraday:
                bars = self.data_cache.get_intraday_bars(symbol, session_date=None)
            else:
                bars = self.data_cache.get_full_bars(symbol)
            if bars is None or bars.empty or "datetime" not in bars.columns:
                continue

            ref = bars.copy()
            ref["datetime"] = pd.to_datetime(ref["datetime"])
            if intraday:
                end_ts = end + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
                ref = ref[(ref["datetime"] >= start) & (ref["datetime"] <= end_ts)].copy()
            else:
                ref["datetime"] = ref["datetime"].dt.normalize()
                ref = ref[(ref["datetime"] >= start) & (ref["datetime"] <= end)].copy()
            if ref.empty:
                continue

            for field in _PRICE_FIELDS:
                if field not in ref.columns:
                    ref[field] = np.nan

            ref_cols = ["datetime", *_PRICE_FIELDS]
            rename_map = {field: f"{symbol.lower()}_{field}" for field in _PRICE_FIELDS}
            out.append(ref[ref_cols].rename(columns=rename_map))
        return out

    def _collect_trading_dates(self, start: pd.Timestamp, end: pd.Timestamp) -> List[datetime]:
        all_dates = set()
        for frame in self._frames.values():
            all_dates.update(pd.to_datetime(frame["datetime"]).dt.to_pydatetime().tolist())
        dates = sorted(d for d in all_dates if start <= pd.Timestamp(d) <= end)
        return dates

    def _collect_trading_timestamps(self, start: pd.Timestamp, end: pd.Timestamp) -> List[datetime]:
        all_times = set()
        end_ts = end + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        for frame in self._frames.values():
            all_times.update(pd.to_datetime(frame["datetime"]).dt.to_pydatetime().tolist())
        times = sorted(t for t in all_times if start <= pd.Timestamp(t) <= end_ts)
        return times

    def _is_intraday_mode(self) -> bool:
        return self.spec.backtest_timeframe in _INTRADAY_BACKTEST_INTERVALS

    def _build_indicator_frame(self, df: pd.DataFrame) -> pd.DataFrame:
        frame = df.copy().sort_values("datetime").reset_index(drop=True)
        for ind in _sorted_indicators(self.spec.indicators):
            try:
                self._apply_indicator(frame, ind)
            except Exception as exc:
                logger.warning("Indicator skipped (%s): %s", ind, exc)
        return frame

    def _apply_indicator(self, frame: pd.DataFrame, ind: dict):
        if not isinstance(ind, dict):
            return

        ind_type = str(ind.get("type", "")).strip().lower()
        if not ind_type:
            return
        name = str(ind.get("name", "")).strip().lower()

        # --- External symbol reference (e.g., SPY close) ---
        if ind_type == "external":
            symbol = str(ind.get("symbol", "")).strip().upper()
            ext_field = str(ind.get("field", "close")).strip().lower()
            if not symbol:
                raise ValueError("External indicator requires a 'symbol' field")
            ref_col = f"{symbol.lower()}_{ext_field}"
            if ref_col in frame.columns:
                if name:
                    frame[name] = frame[ref_col].copy()
                # Column already exists from reference merge; nothing else to do.
            else:
                raise ValueError(
                    f"External reference column '{ref_col}' not found. "
                    f"Ensure symbol {symbol} is fetched as reference data."
                )
            return

        # --- Custom formula evaluation ---
        if ind_type == "custom":
            formula = str(ind.get("formula", "")).strip()
            if not formula:
                raise ValueError("Custom indicator requires a 'formula' field")
            if not name:
                raise ValueError("Custom indicator requires a 'name' field")
            frame[name] = _evaluate_custom_formula(formula, frame)
            return

        # --- Standard indicator types ---
        length = _to_int(ind.get("length"), 20, "indicators[].length")
        if length <= 0:
            raise ValueError("indicators[].length must be >= 1")
        source = str(ind.get("source", "close")).strip().lower()
        if not name:
            name = f"{ind_type}_{length}"

        if source not in frame.columns:
            raise ValueError(f"Unknown source column: {source}")

        series = frame[source]
        if ind_type == "sma":
            frame[name] = series.rolling(length, min_periods=length).mean()
        elif ind_type == "ema":
            frame[name] = series.ewm(span=length, adjust=False).mean()
        elif ind_type == "rsi":
            frame[name] = _rsi(series, length)
        elif ind_type == "zscore":
            mean = series.rolling(length, min_periods=length).mean()
            std = series.rolling(length, min_periods=length).std()
            frame[name] = (series - mean) / std.replace(0, np.nan)
        elif ind_type == "atr":
            frame[name] = _atr(frame, length)
        elif ind_type == "stddev":
            frame[name] = series.rolling(length, min_periods=length).std()
        elif ind_type in {"rolling_max", "max"}:
            frame[name] = series.rolling(length, min_periods=length).max()
        elif ind_type in {"rolling_min", "min"}:
            frame[name] = series.rolling(length, min_periods=length).min()
        elif ind_type == "lag":
            periods = _to_int(ind.get("periods", length), length, "indicators[].periods")
            frame[name] = series.shift(periods)
        elif ind_type in {"vwap", "vwap_proxy"}:
            frame[name] = (frame["high"] + frame["low"] + frame["close"]) / 3.0
        else:
            raise ValueError(f"Unsupported indicator type: {ind_type}")

    def _update_last_close_snapshot(self, current_date: datetime):
        key = pd.Timestamp(current_date)
        if not self._is_intraday_mode():
            key = key.normalize()
        for symbol, frame in self._frames.items():
            if key in frame.index:
                row = frame.loc[key]
                if isinstance(row, pd.DataFrame):
                    row = row.iloc[-1]
                self._last_close[symbol] = float(row["close"])

    def _manage_open_positions(self, current_date: datetime):
        for symbol in list(self.portfolio.positions.keys()):
            row = self._get_row(symbol, current_date)
            if row is None:
                continue

            pos = self.portfolio.positions[symbol]
            side = (pos.side or "LONG").upper()
            days_held = (pd.Timestamp(current_date) - pd.Timestamp(pos.entry_date)).days
            exit_price = None
            exit_reason = None

            if self.spec.use_intraday_vwap_stop:
                if self._is_intraday_mode():
                    vwap_px = float(row.get("session_vwap", np.nan))
                    close_px = float(row["close"])
                    if np.isfinite(vwap_px):
                        if side == "SHORT" and close_px > vwap_px:
                            exit_price = close_px
                            exit_reason = "INTRADAY_VWAP_STOP"
                        elif side != "SHORT" and close_px < vwap_px:
                            exit_price = close_px
                            exit_reason = "INTRADAY_VWAP_STOP"
                else:
                    intraday_exit = self._intraday_vwap_exit_price(symbol, side, current_date)
                    if intraday_exit is not None:
                        exit_price = intraday_exit
                        exit_reason = "INTRADAY_VWAP_STOP"

            if exit_price is None and (self.spec.stop_loss_pct > 0 or self.spec.stop_loss_field):
                stop_price = pos.current_stop
                if side == "SHORT":
                    if float(row["high"]) >= stop_price:
                        exit_price = stop_price
                        exit_reason = "STOP_LOSS"
                else:
                    if float(row["low"]) <= stop_price:
                        exit_price = stop_price
                        exit_reason = "STOP_LOSS"

            # Staged take-profit rules (partial exits + stop adjustment)
            if exit_price is None and self.spec.take_profit_rules:
                for rule_idx, tp_rule in enumerate(self.spec.take_profit_rules):
                    if rule_idx in pos.triggered_tp_rules:
                        continue
                    trigger_r = float(tp_rule.get("trigger_r", 0))
                    trigger_pct = float(tp_rule.get("trigger_pct", 0))
                    if trigger_r > 0:
                        # R-multiple based trigger (e.g. 2R = 2x the risk distance)
                        r_distance = trigger_r * pos.r_value
                        if side == "SHORT":
                            trigger_price = pos.entry_price - r_distance
                            triggered = float(row["low"]) <= trigger_price
                        else:
                            trigger_price = pos.entry_price + r_distance
                            triggered = float(row["high"]) >= trigger_price
                    elif trigger_pct > 0:
                        if side == "SHORT":
                            trigger_price = pos.entry_price * (1 - trigger_pct / 100.0)
                            triggered = float(row["low"]) <= trigger_price
                        else:
                            trigger_price = pos.entry_price * (1 + trigger_pct / 100.0)
                            triggered = float(row["high"]) >= trigger_price
                    else:
                        continue
                    if not triggered:
                        continue
                    pos.triggered_tp_rules.append(rule_idx)
                    action = str(tp_rule.get("action", "")).strip().lower()
                    sell_pct = float(tp_rule.get("sell_pct_of_position", 100))
                    slippage_pct = self.bt_config.slippage_pct / 100.0
                    partial_price = trigger_price
                    if side == "SHORT":
                        partial_price *= (1 + slippage_pct)
                    else:
                        partial_price *= (1 - slippage_pct)
                    if action == "sell_partial" and sell_pct < 100:
                        partial_trade = self.portfolio.reduce_position(
                            symbol, sell_pct, partial_price,
                            current_date, f"TP_RULE_{rule_idx}",
                        )
                        if partial_trade and partial_trade.get("pnl_r", 0) < 0:
                            self._daily_loss_r += abs(partial_trade["pnl_r"])
                        self._update_regime()
                    else:
                        exit_price = trigger_price
                        exit_reason = f"TP_RULE_{rule_idx}"
                    # Adjust stop if specified and position still open
                    move_stop = tp_rule.get("move_stop_to_pct")
                    if move_stop is not None and symbol in self.portfolio.positions:
                        new_stop_pct = float(move_stop)
                        if side == "SHORT":
                            new_stop = pos.entry_price * (1 - new_stop_pct / 100.0)
                            pos.current_stop = min(pos.current_stop, new_stop)
                        else:
                            new_stop = pos.entry_price * (1 + new_stop_pct / 100.0)
                            pos.current_stop = max(pos.current_stop, new_stop)
                    # If position was fully closed by reduce_position, stop processing
                    if symbol not in self.portfolio.positions:
                        break

            # If position was fully closed by partial exit rules, skip remaining checks
            if symbol not in self.portfolio.positions:
                continue

            if exit_price is None and self.spec.take_profit_pct > 0:
                if side == "SHORT":
                    tp_price = pos.entry_price * (1 - self.spec.take_profit_pct / 100.0)
                    if float(row["low"]) <= tp_price:
                        exit_price = tp_price
                        exit_reason = "TAKE_PROFIT"
                else:
                    tp_price = pos.entry_price * (1 + self.spec.take_profit_pct / 100.0)
                    if float(row["high"]) >= tp_price:
                        exit_price = tp_price
                        exit_reason = "TAKE_PROFIT"

            if exit_price is None and self.spec.max_holding_days > 0:
                if days_held >= self.spec.max_holding_days:
                    exit_price = float(row["close"])
                    exit_reason = "TIME_EXIT"

            if exit_price is None:
                # When exit_rule_after_tp is set, only evaluate exit rule after
                # at least one take-profit rule has been triggered.
                should_eval_exit = True
                if self.spec.exit_rule_after_tp and self.spec.take_profit_rules:
                    should_eval_exit = len(pos.triggered_tp_rules) > 0

                if should_eval_exit:
                    try:
                        should_exit = _evaluate_rule_scalar(
                            self.spec.exit_rule,
                            row,
                            position_side=side,
                            context={
                                "entry_price": float(pos.entry_price),
                                "initial_stop": float(pos.initial_stop),
                                "current_stop": float(pos.current_stop),
                                "stop_price": float(pos.current_stop),
                                "r_value": float(pos.r_value),
                                "shares": int(pos.shares),
                                "days_held": int(days_held),
                            },
                        )
                    except Exception as exc:
                        raise ValueError(
                            f"Exit rule error for {symbol} on {pd.Timestamp(current_date).date()}: {exc}"
                        ) from exc

                    if should_exit:
                        exit_price = float(row["close"])
                        exit_reason = "RULE_EXIT"

            if exit_price is not None and exit_reason:
                # Apply slippage to exit: unfavorable direction for the trader
                slippage_pct = self.bt_config.slippage_pct / 100.0
                if side == "SHORT":
                    exit_price *= (1 + slippage_pct)  # worse fill for short covers
                else:
                    exit_price *= (1 - slippage_pct)  # worse fill for long sells
                trade = self.portfolio.close_position(symbol, exit_price, current_date, exit_reason)
                if trade and trade.get("pnl_r", 0) < 0:
                    self._daily_loss_r += abs(trade["pnl_r"])
                self._update_regime()

    def _intraday_vwap_exit_price(
        self, symbol: str, side: str, current_date: datetime
    ) -> Optional[float]:
        bars = self.data_cache.get_intraday_bars(symbol, session_date=current_date)
        if bars is None or bars.empty:
            return None
        required = {"high", "low", "close", "volume"}
        if not required.issubset(set(bars.columns)):
            return None

        intraday = bars.sort_values("datetime").copy()
        typical = (intraday["high"] + intraday["low"] + intraday["close"]) / 3.0
        cum_vol = intraday["volume"].cumsum()
        intraday["vwap"] = (typical * intraday["volume"]).cumsum() / cum_vol.replace(0, np.nan)

        for row in intraday.itertuples(index=False):
            close_px = float(row.close)
            vwap_px = float(row.vwap) if pd.notna(row.vwap) else float("nan")
            if not np.isfinite(vwap_px):
                continue
            if side == "SHORT":
                if close_px > vwap_px:
                    return close_px
            else:
                if close_px < vwap_px:
                    return close_px
        return None

    def _scan_entries(self, current_date: datetime):
        # Daily loss lock: block all new entries after losing N R in one day
        if self.spec.daily_loss_lock_r > 0 and self._daily_loss_r >= self.spec.daily_loss_lock_r:
            return

        max_pos = self._effective_max_positions()
        if self.portfolio.open_position_count >= max_pos:
            return

        # Collect all signals, then rank by volume ratio (highest volume expansion first)
        candidates = []
        for symbol in self._trade_symbols:
            if symbol in self.portfolio.positions:
                continue

            row = self._get_row(symbol, current_date)
            if row is None:
                continue

            long_signal = bool(row.get("entry_signal_long", False))
            short_signal = bool(row.get("entry_signal_short", False))
            if long_signal and short_signal:
                continue

            if long_signal or short_signal:
                # Rank by custom field or default to volume expansion ratio
                if self.spec.ranking_field:
                    rank_val = float(row.get(self.spec.ranking_field, 0))
                else:
                    vol = float(row.get("volume", 0))
                    vol_avg = float(row.get("vol_sma_20", 1))
                    rank_val = vol / max(vol_avg, 1.0)
                side = "LONG" if long_signal else "SHORT"
                candidates.append((rank_val, symbol, row, side))

        # Sort by ranking value descending (highest first)
        candidates.sort(key=lambda x: x[0], reverse=True)

        for _, symbol, row, side in candidates:
            if self.portfolio.open_position_count >= max_pos:
                break
            # Portfolio risk cap: block new entries when total open risk exceeds limit
            if self.spec.max_total_open_risk_r > 0:
                if self._total_open_risk_r() >= self.spec.max_total_open_risk_r:
                    break
            self._open_position(symbol, row, current_date, side=side)

    def _open_position(self, symbol: str, row: pd.Series, current_date: datetime, side: str):
        entry_field = self.spec.entry_price_field
        entry_price = float(row.get(entry_field, row.get("close", 0.0)))
        if entry_price <= 0:
            return

        # Apply slippage: unfavorable direction for the trader
        slippage_pct = self.bt_config.slippage_pct / 100.0
        if side == "SHORT":
            entry_price *= (1 - slippage_pct)  # worse fill for shorts
        else:
            entry_price *= (1 + slippage_pct)  # worse fill for longs

        close_prices = {
            sym: self._last_close.get(sym, pos.entry_price)
            for sym, pos in self.portfolio.positions.items()
        }
        equity = self.portfolio.total_equity(close_prices)

        if self.spec.stop_loss_field:
            field_val = float(row.get(self.spec.stop_loss_field, 0))
            if field_val > 0:
                if side == "SHORT":
                    stop_price = entry_price + field_val
                else:
                    stop_price = entry_price - field_val
            else:
                stop_price = entry_price * (1.01 if side == "SHORT" else 0.99)
        elif self.spec.stop_loss_pct > 0:
            if side == "SHORT":
                stop_price = entry_price * (1 + self.spec.stop_loss_pct / 100.0)
            else:
                stop_price = entry_price * (1 - self.spec.stop_loss_pct / 100.0)
        else:
            stop_price = entry_price * (1.01 if side == "SHORT" else 0.99)

        r_value = max(abs(entry_price - stop_price), entry_price * 0.005)
        if self.spec.position_size_mode == "risk_pct":
            risk_dollars = equity * (self.spec.risk_per_trade_pct / 100.0)
            # Reduce position size in CHOP regime
            if self._current_regime == "CHOP" and self.spec.regime_chop_size_multiplier > 0:
                risk_dollars *= self.spec.regime_chop_size_multiplier
            shares = math.floor(risk_dollars / r_value)
        else:
            position_value = equity * (self.spec.position_size_pct / 100.0)
            shares = math.floor(position_value / entry_price)

        if shares <= 0:
            return

        pos = BacktestPosition(
            symbol=symbol,
            shares=shares,
            side=side,
            entry_price=entry_price,
            entry_date=current_date,
            initial_stop=stop_price,
            current_stop=stop_price,
            r_value=r_value,
            regime_at_entry=self._current_regime,
            signal_type=f"RULE_{side}",
        )
        self.portfolio.open_position(pos)

    def _liquidate_at_end(self, last_date: datetime):
        slippage_pct = self.bt_config.slippage_pct / 100.0
        for symbol in list(self.portfolio.positions.keys()):
            pos = self.portfolio.positions[symbol]
            exit_price = self._last_close.get(symbol, pos.entry_price)
            side = (pos.side or "LONG").upper()
            if side == "SHORT":
                exit_price *= (1 + slippage_pct)
            else:
                exit_price *= (1 - slippage_pct)
            self.portfolio.close_position(symbol, exit_price, last_date, "END_OF_TEST")

    def _get_row(self, symbol: str, current_date: datetime) -> Optional[pd.Series]:
        frame = self._frames.get(symbol)
        if frame is None:
            return None
        key = pd.Timestamp(current_date)
        if not self._is_intraday_mode():
            key = key.normalize()
        if key not in frame.index:
            return None
        row = frame.loc[key]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[-1]
        return row

    def _effective_max_positions(self) -> int:
        """Return regime-dependent max positions."""
        if self._current_regime == "CHOP" and self.spec.max_positions_chop > 0:
            return self.spec.max_positions_chop
        if self._current_regime == "TREND" and self.spec.max_positions_trend > 0:
            return self.spec.max_positions_trend
        return self.spec.max_positions

    def _update_regime(self):
        """Recompute TREND/CHOP regime from recent closed-trade hit rate."""
        if self.spec.regime_trend_threshold <= 0:
            self._current_regime = "TREND"
            return
        window = max(5, self.spec.regime_hit_rate_window)
        recent = self.portfolio.closed_trades[-window:]
        if len(recent) < 5:
            self._current_regime = "TREND"
            return
        wins = sum(1 for t in recent if t.get("pnl_r", 0) > 0)
        hit_rate = wins / len(recent)
        self._current_regime = "TREND" if hit_rate >= self.spec.regime_trend_threshold else "CHOP"

    def _total_open_risk_r(self) -> float:
        """Sum of current risk in R-multiples for all open positions."""
        total = 0.0
        for pos in self.portfolio.positions.values():
            if pos.r_value <= 0:
                continue
            if pos.side == "SHORT":
                risk = (pos.current_stop - pos.entry_price) / pos.r_value
            else:
                risk = (pos.entry_price - pos.current_stop) / pos.r_value
            total += max(0.0, risk)
        return total


# ---------------------------------------------------------------------------
# Tiered lazy evaluation helpers
# ---------------------------------------------------------------------------

# Columns available for free (raw OHLCV + lag/shift)
_FREE_COLUMNS = {"open", "high", "low", "close", "volume", "datetime"}
# Built-in function names that are NOT indicator column references
_BUILTIN_NAMES = {
    "abs", "min", "max", "lag", "pct_change", "sma", "ema", "rsi", "zscore",
    "atr", "crosses_above", "crosses_below", "np", "pd", "True", "False",
}


def _split_entry_conditions(entry_rule: str) -> List[str]:
    """Split an entry rule into individual AND-conditions.

    Respects parenthesized groups so ``(a | b | c)`` stays as one unit.
    Returns the list of top-level condition strings joined by ``&``.
    """
    if not entry_rule or not entry_rule.strip():
        return []

    expr = entry_rule.strip()
    # Walk through the expression splitting on top-level '&' only
    conditions: List[str] = []
    depth = 0
    current: List[str] = []
    for ch in expr:
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "&" and depth == 0:
            token = "".join(current).strip()
            if token:
                conditions.append(token)
            current = []
        else:
            current.append(ch)
    token = "".join(current).strip()
    if token:
        conditions.append(token)
    return conditions


def _condition_columns(condition: str, indicator_names: set) -> set:
    """Return the set of indicator column names referenced in *condition*.

    Only returns names that are known indicators (not raw price fields or
    built-in function names).
    """
    # Extract all identifier-like tokens
    tokens = set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", condition))
    # Keep only those that are indicator names
    return tokens & indicator_names


def _assign_indicator_tier(ind: dict) -> int:
    """Assign a cost tier (0-3) to an indicator definition.

    Tier 0 — free (lag/shift, raw price fields)
    Tier 1 — cheap (SMA, EMA — single rolling window)
    Tier 2 — medium (RSI, ATR, rolling_max/min, external refs)
    Tier 3 — expensive (custom formulas)
    """
    ind_type = str(ind.get("type", "")).strip().lower()
    if ind_type == "lag":
        return 0
    if ind_type in {"sma", "ema", "stddev"}:
        return 1
    if ind_type in {"rsi", "atr", "rolling_max", "max", "rolling_min", "min",
                     "zscore", "external", "vwap", "vwap_proxy"}:
        return 2
    if ind_type == "custom":
        return 3
    return 2  # unknown → medium


def _build_tiered_pipeline(
    indicators: List[dict],
    entry_rule: str,
) -> List[dict]:
    """Build a tier pipeline: list of {tier, indicators, conditions}.

    Each element represents one tier of work. Processing stops early if any
    tier's conditions are never satisfied for a symbol.
    """
    # Build set of all indicator names and their base tiers
    ind_name_to_tier: Dict[str, int] = {}
    sorted_inds = _sorted_indicators(indicators)
    for ind in sorted_inds:
        name = str(ind.get("name", "")).strip().lower()
        tier = _assign_indicator_tier(ind)
        if name:
            ind_name_to_tier[name] = tier

    # Promote indicators whose source depends on a higher-tier indicator.
    # e.g., sma(source=spy_close) is tier 1, but spy_close is tier 2 external
    # → promote the SMA to tier 2 so it runs after the external.
    for ind in sorted_inds:
        name = str(ind.get("name", "")).strip().lower()
        source = str(ind.get("source", "")).strip().lower()
        if source and source in ind_name_to_tier:
            source_tier = ind_name_to_tier[source]
            if name and ind_name_to_tier.get(name, 0) < source_tier:
                ind_name_to_tier[name] = source_tier

    # Group indicators into their (possibly promoted) tiers
    ind_by_tier: Dict[int, List[dict]] = {0: [], 1: [], 2: [], 3: []}
    for ind in sorted_inds:
        name = str(ind.get("name", "")).strip().lower()
        tier = ind_name_to_tier.get(name, _assign_indicator_tier(ind))
        ind_by_tier[tier].append(ind)

    all_ind_names = set(ind_name_to_tier.keys())

    # Split entry rule into conditions and assign each to the highest tier
    # of indicator it references
    conditions = _split_entry_conditions(entry_rule)
    cond_by_tier: Dict[int, List[str]] = {0: [], 1: [], 2: [], 3: []}
    for cond in conditions:
        cols = _condition_columns(cond, all_ind_names)
        if not cols:
            # Condition uses only raw price fields / lag → tier 0
            cond_by_tier[0].append(cond)
        else:
            max_tier = max(ind_name_to_tier.get(c, 0) for c in cols)
            cond_by_tier[max_tier].append(cond)

    # Build the pipeline (skip empty tiers)
    pipeline = []
    for tier in sorted(ind_by_tier.keys()):
        inds = ind_by_tier[tier]
        conds = cond_by_tier.get(tier, [])
        if inds or conds:
            pipeline.append({
                "tier": tier,
                "indicators": inds,
                "conditions": conds,
            })
    return pipeline


def _sorted_indicators(indicators: List[dict]) -> List[dict]:
    """Sort indicators so externals are processed first, then standard, then customs.
    This ensures dependency order: external columns exist before standard indicators
    that reference them, and custom formulas can reference any prior column."""
    externals = []
    customs = []
    standard = []
    for ind in (indicators or []):
        ind_type = str(ind.get("type", "")).strip().lower()
        if ind_type == "external":
            externals.append(ind)
        elif ind_type == "custom":
            customs.append(ind)
        else:
            standard.append(ind)
    return externals + standard + customs


def _evaluate_custom_formula(formula: str, frame: pd.DataFrame) -> pd.Series:
    """Evaluate a custom pandas-compatible formula in the context of frame columns.

    Handles ThinkScript-style syntax:
      - ``name[0:N]`` → ``name.rolling(N)``  (rolling window)
      - ``name[N]``   → ``name.shift(N)``    (bar lookback / lag)
    """
    _validate_expression(formula)

    expr = formula.strip()

    # Translate paired rolling operations: a[0:N] OP b[0:N] -> (a OP b).rolling(N)
    # This handles arithmetic between same-window rolling references (e.g., high[0:20] - low[0:20])
    expr = re.sub(
        r"\b([A-Za-z_]\w*)\s*\[\s*0\s*:\s*(\d+)\s*\]\s*([+\-*/])\s*([A-Za-z_]\w*)\s*\[\s*0\s*:\s*\2\s*\]",
        lambda m: f"({m.group(1)} {m.group(3)} {m.group(4)}).rolling({m.group(2)})",
        expr,
    )

    # Translate remaining standalone slice notation: name[0:N] -> name.rolling(N)
    expr = re.sub(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\[\s*0\s*:\s*(\d+)\s*\]",
        lambda m: f"{m.group(1)}.rolling({m.group(2)})",
        expr,
    )

    # Translate simple lookback: name[N] -> name.shift(N)
    expr = re.sub(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\[\s*(\d+)\s*\]",
        lambda m: f"{m.group(1)}.shift({m.group(2)})",
        expr,
    )

    local_env = {col: frame[col] for col in frame.columns}
    local_env.update(
        {
            "np": np,
            "pd": pd,
            "abs": _safe_abs,
            "min": _safe_min,
            "max": _safe_max,
        }
    )

    try:
        result = eval(expr, {"__builtins__": {}}, local_env)
    except Exception as exc:
        raise ValueError(f"Custom formula error: {exc}  (formula: {formula})") from exc

    if isinstance(result, pd.Series):
        return result
    return pd.Series(result, index=frame.index)


def _evaluate_rule_series(expression: str, frame: pd.DataFrame) -> pd.Series:
    expr = _normalize_expression(expression)
    _validate_expression(expr)

    local_env = _build_rule_env_series(frame)

    try:
        result = eval(expr, {"__builtins__": {}}, local_env)
    except NameError as exc:
        raise ValueError(f"{exc}. Unknown variable in strategy rule.") from exc
    return _coerce_to_bool_series(result, frame.index)


def _evaluate_rule_scalar(
    expression: str,
    row: pd.Series,
    position_side: str = "LONG",
    context: Optional[dict] = None,
) -> bool:
    expr = _normalize_expression(expression)
    _validate_expression(expr)

    local_env = _build_rule_env_scalar(row, position_side=position_side, context=context)
    try:
        result = eval(expr, {"__builtins__": {}}, local_env)
    except NameError as exc:
        raise ValueError(f"{exc}. Unknown variable in strategy rule.") from exc

    if isinstance(result, (np.bool_, bool)):
        return bool(result)
    if isinstance(result, (int, float, np.number)):
        return bool(result)
    return False


def _build_rule_env_series(frame: pd.DataFrame) -> dict:
    env = {col: frame[col] for col in frame.columns}
    env.update(
        {
            "np": np,
            "pd": pd,
            "abs": _safe_abs,
            "min": _safe_min,
            "max": _safe_max,
            "sma": lambda s, n: pd.Series(s).rolling(int(n), min_periods=int(n)).mean(),
            "ema": lambda s, n: pd.Series(s).ewm(span=int(n), adjust=False).mean(),
            "rsi": lambda s, n: _rsi(pd.Series(s), int(n)),
            "zscore": lambda s, n: _zscore(pd.Series(s), int(n)),
            "atr": lambda n=14: _atr(frame, int(n)),
            "lag": lambda s, n=1: pd.Series(s).shift(int(n)),
            "pct_change": lambda s, n=1: pd.Series(s).pct_change(int(n)),
            "crosses_above": _crosses_above,
            "crosses_below": _crosses_below,
        }
    )
    return env


def _build_rule_env_scalar(
    row: pd.Series, position_side: str = "LONG", context: Optional[dict] = None
) -> dict:
    env = dict(row)
    side = str(position_side or "LONG").upper()
    if isinstance(context, dict):
        for key, value in context.items():
            env[str(key)] = value
    env.update(
        {
            "np": np,
            "pd": pd,
            "abs": _safe_abs,
            "min": _safe_min,
            "max": _safe_max,
            "position_side": side,
            "is_long": side == "LONG",
            "is_short": side == "SHORT",
        }
    )
    return env


def _normalize_expression(expression: str) -> str:
    expr = str(expression or "").strip()
    if not expr:
        return "False"
    # Translate common lookback syntax (e.g., close[20]) to lag(close, 20).
    # This prevents pandas label-index lookups (KeyError) and matches
    # bar-lookback intent from scripts like ThinkScript.
    expr = re.sub(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\[\s*(-?\d+)\s*\]",
        lambda m: f"lag({m.group(1)}, {int(m.group(2))})",
        expr,
    )
    expr = re.sub(r"\band\b", "&", expr, flags=re.IGNORECASE)
    expr = re.sub(r"\bor\b", "|", expr, flags=re.IGNORECASE)
    parts = re.split(r"(\&|\|)", expr)
    if len(parts) > 1:
        normalized_parts: List[str] = []
        for part in parts:
            token = part.strip()
            if not token:
                continue
            if token in {"&", "|"}:
                normalized_parts.append(token)
            else:
                if not (token.startswith("(") and token.endswith(")")):
                    token = f"({token})"
                normalized_parts.append(token)
        expr = " ".join(normalized_parts)
    return expr


def _validate_expression(expression: str):
    expr_low = expression.lower()
    for token in _DISALLOWED_EXPR_TOKENS:
        if token in expr_low:
            raise ValueError(f"Disallowed token in expression: {token}")


def _coerce_to_bool_series(value, index: pd.Index) -> pd.Series:
    if isinstance(value, pd.Series):
        return value.reindex(index, fill_value=False).fillna(False).astype(bool)
    if isinstance(value, (list, tuple, np.ndarray)):
        series = pd.Series(value, index=index[: len(value)])
        return series.reindex(index, fill_value=False).fillna(False).astype(bool)
    if isinstance(value, (bool, int, float, np.bool_)):
        return pd.Series(bool(value), index=index)
    return pd.Series(False, index=index)


def _rsi(series: pd.Series, length: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(length, min_periods=length).mean()
    avg_loss = loss.rolling(length, min_periods=length).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _zscore(series: pd.Series, length: int) -> pd.Series:
    mean = series.rolling(length, min_periods=length).mean()
    std = series.rolling(length, min_periods=length).std()
    return (series - mean) / std.replace(0, np.nan)


def _atr(frame: pd.DataFrame, length: int) -> pd.Series:
    high = frame["high"]
    low = frame["low"]
    close = frame["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(length, min_periods=length).mean()


def _crosses_above(a: Iterable, b: Iterable) -> pd.Series:
    a_series = pd.Series(a)
    b_series = pd.Series(b)
    return (a_series > b_series) & (a_series.shift(1) <= b_series.shift(1))


def _crosses_below(a: Iterable, b: Iterable) -> pd.Series:
    a_series = pd.Series(a)
    b_series = pd.Series(b)
    return (a_series < b_series) & (a_series.shift(1) >= b_series.shift(1))


def _safe_abs(value):
    return np.abs(value)


def _safe_min(*values):
    if len(values) == 1:
        return values[0]
    return np.minimum.reduce(values)


def _safe_max(*values):
    if len(values) == 1:
        return values[0]
    return np.maximum.reduce(values)


def _normalize_symbol_token(value: str) -> str:
    token = re.sub(r"[^A-Z0-9]+", "_", str(value or "").upper()).strip("_")
    return token


def _is_auto_us_symbol_token(value: str) -> bool:
    normalized = _normalize_symbol_token(value)
    return normalized in _AUTO_US_UNIVERSE_TOKENS


def _list_requests_auto_us(values: Iterable[str]) -> bool:
    for value in values:
        if _is_auto_us_symbol_token(str(value)):
            return True
    return False


def _trade_symbols_from_list(values: Iterable[str]) -> List[str]:
    out = []
    seen = set()
    for value in values:
        sym = str(value).strip().upper()
        if not sym or _is_auto_us_symbol_token(sym):
            continue
        if sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def _to_float(raw_value, default: float, field_name: str) -> float:
    if raw_value is None or raw_value == "":
        return float(default)
    if isinstance(raw_value, str):
        cleaned = raw_value.strip().lower()
        if cleaned in {"", "none", "null", "na", "n/a"}:
            return float(default)
        cleaned = cleaned.replace(",", "")
        if cleaned.endswith("%"):
            cleaned = cleaned[:-1].strip()
        raw_value = cleaned
    try:
        return float(raw_value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a number")


def _to_int(raw_value, default: int, field_name: str) -> int:
    if raw_value is None or raw_value == "":
        return int(default)
    if isinstance(raw_value, str):
        cleaned = raw_value.strip().lower()
        if cleaned in {"", "none", "null", "na", "n/a"}:
            return int(default)
        cleaned = cleaned.replace(",", "")
        if cleaned.endswith("%"):
            cleaned = cleaned[:-1].strip()
        raw_value = cleaned
    try:
        if isinstance(raw_value, str) and "." in raw_value:
            as_float = float(raw_value)
            if not as_float.is_integer():
                raise ValueError
            return int(as_float)
        return int(raw_value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be an integer")


def _to_bool(raw_value, default: bool = False) -> bool:
    if raw_value is None:
        return bool(default)
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, (int, float)):
        return bool(raw_value)
    if isinstance(raw_value, str):
        cleaned = raw_value.strip().lower()
        if cleaned in {"1", "true", "yes", "y", "on"}:
            return True
        if cleaned in {"0", "false", "no", "n", "off", "", "none", "null"}:
            return False
    return bool(default)
