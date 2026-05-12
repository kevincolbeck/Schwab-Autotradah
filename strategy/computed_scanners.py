"""Computed momentum scanner rules translated from ThinkScript."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Set

import pandas as pd


SCANNER_1M = "qullamaggie 1 month"
SCANNER_3M = "qullamaggie 3 month"
SCANNER_6M = "qullamaggie 6 month"
SCANNER_TEST = "test scanner"

_CANONICAL_ORDER = [SCANNER_1M, SCANNER_3M, SCANNER_6M, SCANNER_TEST]


def normalize_scanner_name(name: str) -> Optional[str]:
    value = (name or "").strip().lower()
    if not value:
        return None

    if "1" in value and "month" in value:
        return SCANNER_1M
    if "3" in value and "month" in value:
        return SCANNER_3M
    if "6" in value and "month" in value:
        return SCANNER_6M
    if "test" in value:
        return SCANNER_TEST
    return None


def canonicalize_scanner_names(names: Iterable[str]) -> List[str]:
    selected: Set[str] = set()
    for name in names:
        canon = normalize_scanner_name(name)
        if canon:
            selected.add(canon)
    return [name for name in _CANONICAL_ORDER if name in selected]


@dataclass
class ScannerRules:
    # 1M / 3M / 6M scanners
    min_adr_pct: float = 4.5
    min_dollar_vol: float = 20_000_000
    min_1m_return_pct: float = 35.0
    min_3m_return_pct: float = 35.0
    min_6m_return_pct: float = 40.0
    days_1m: int = 21
    days_3m: int = 63
    days_6m: int = 126

    # Test scanner
    test_lookback_days: int = 63
    test_adr_period: int = 20
    test_min_adr_pct: float = 7.0
    test_min_avg_dollar_vol: float = 16_500_000
    test_min_gain_pct: float = 100.0
    test_consolidation_window: int = 10


def _scanner_condition_series(
    daily_bars: pd.DataFrame,
    rules: ScannerRules,
) -> dict[str, pd.Series]:
    required_cols = {"close", "high", "low", "volume"}
    if daily_bars is None or daily_bars.empty:
        return {}
    if not required_cols.issubset(set(daily_bars.columns)):
        return {}

    close = daily_bars["close"]
    high = daily_bars["high"]
    low = daily_bars["low"]
    volume = daily_bars["volume"]

    dollar_vol = close * volume
    adr20_pct = ((high - low) / close).rolling(20, min_periods=20).mean() * 100

    ret_1m = (close / close.shift(rules.days_1m) - 1.0) * 100
    cond_1m = (
        ret_1m.notna()
        & adr20_pct.notna()
        & (ret_1m >= rules.min_1m_return_pct)
        & (adr20_pct >= rules.min_adr_pct)
        & (dollar_vol >= rules.min_dollar_vol)
    )

    ret_3m = (close / close.shift(rules.days_3m) - 1.0) * 100
    cond_3m = (
        ret_3m.notna()
        & adr20_pct.notna()
        & (ret_3m >= rules.min_3m_return_pct)
        & (adr20_pct >= rules.min_adr_pct)
        & (dollar_vol >= rules.min_dollar_vol)
    )

    ret_6m = (close / close.shift(rules.days_6m) - 1.0) * 100
    cond_6m = (
        ret_6m.notna()
        & adr20_pct.notna()
        & (ret_6m >= rules.min_6m_return_pct)
        & (adr20_pct >= rules.min_adr_pct)
        & (dollar_vol >= rules.min_dollar_vol)
    )

    lookback = rules.test_lookback_days
    win = rules.test_consolidation_window
    pct_gain = (close / close.shift(lookback) - 1.0) * 100
    strong_move = pct_gain >= rules.test_min_gain_pct

    recent_high = high.rolling(win, min_periods=win).max()
    recent_low = low.rolling(win, min_periods=win).min()
    contracting = (high < recent_high.shift(1)) & (low > recent_low.shift(1))

    adr_pct = ((high - low) / close * 100).rolling(
        rules.test_adr_period, min_periods=rules.test_adr_period
    ).mean()
    avg_dollar_vol = dollar_vol.rolling(20, min_periods=20).mean()
    liquid = avg_dollar_vol >= rules.test_min_avg_dollar_vol
    cond_test = strong_move & contracting & (adr_pct >= rules.test_min_adr_pct) & liquid

    return {
        SCANNER_1M: cond_1m.fillna(False),
        SCANNER_3M: cond_3m.fillna(False),
        SCANNER_6M: cond_6m.fillna(False),
        SCANNER_TEST: cond_test.fillna(False),
    }


def selected_scanner_mask(
    daily_bars: pd.DataFrame,
    selected_scanners: Iterable[str],
    rules: Optional[ScannerRules] = None,
) -> pd.Series:
    """Return per-bar mask where at least one selected scanner passes."""
    if daily_bars is None or daily_bars.empty:
        return pd.Series(dtype=bool)

    selected = set(canonicalize_scanner_names(selected_scanners))
    if not selected:
        return pd.Series(False, index=daily_bars.index)

    rules = rules or ScannerRules()
    conditions = _scanner_condition_series(daily_bars, rules)
    if not conditions:
        return pd.Series(False, index=daily_bars.index)

    combined = pd.Series(False, index=daily_bars.index)
    for scanner_name in selected:
        cond = conditions.get(scanner_name)
        if cond is not None:
            combined = combined | cond.reindex(daily_bars.index, fill_value=False)
    return combined.fillna(False)


def matched_scanners(
    daily_bars: pd.DataFrame,
    selected_scanners: Iterable[str],
    rules: Optional[ScannerRules] = None,
) -> Set[str]:
    """Return selected scanners that pass on the latest available bar."""
    if daily_bars is None or daily_bars.empty:
        return set()

    rules = rules or ScannerRules()
    selected = set(canonicalize_scanner_names(selected_scanners))
    if not selected:
        return set()

    conditions = _scanner_condition_series(daily_bars, rules)
    if not conditions:
        return set()

    out: Set[str] = set()
    for scanner_name in selected:
        cond = conditions.get(scanner_name)
        if cond is not None and not cond.empty and bool(cond.iloc[-1]):
            out.add(scanner_name)

    return out


def passes_selected_scanners(
    daily_bars: pd.DataFrame,
    selected_scanners: Iterable[str],
    rules: Optional[ScannerRules] = None,
) -> bool:
    return bool(matched_scanners(daily_bars, selected_scanners, rules))


def scanner_rules_from_config(universe_cfg) -> ScannerRules:
    """Build scanner rules from config.universe values."""
    return ScannerRules(
        min_adr_pct=float(getattr(universe_cfg, "scanner_min_adr_pct", 4.5)),
        min_dollar_vol=float(getattr(universe_cfg, "scanner_min_dollar_vol", 20_000_000)),
        min_1m_return_pct=float(getattr(universe_cfg, "scanner_1m_min_return_pct", 35.0)),
        min_3m_return_pct=float(getattr(universe_cfg, "scanner_3m_min_return_pct", 35.0)),
        min_6m_return_pct=float(getattr(universe_cfg, "scanner_6m_min_return_pct", 40.0)),
        test_lookback_days=int(getattr(universe_cfg, "test_scanner_lookback_days", 63)),
        test_min_gain_pct=float(getattr(universe_cfg, "test_scanner_min_gain_pct", 100.0)),
        test_consolidation_window=int(
            getattr(universe_cfg, "test_scanner_consolidation_window", 10)
        ),
        test_adr_period=int(getattr(universe_cfg, "test_scanner_adr_period", 20)),
        test_min_adr_pct=float(getattr(universe_cfg, "test_scanner_min_adr_pct", 7.0)),
        test_min_avg_dollar_vol=float(
            getattr(universe_cfg, "test_scanner_min_avg_dollar_vol", 16_500_000)
        ),
    )
