"""Technical indicator calculations using pandas/numpy."""

import numpy as np
import pandas as pd
from typing import Optional, Tuple


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average."""
    return series.rolling(window=period, min_periods=period).mean()


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range."""
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return true_range.rolling(window=period, min_periods=period).mean()


def atr_percent(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """ATR as a percentage of close price."""
    atr_val = atr(high, low, close, period)
    return (atr_val / close) * 100


def atr_percentile(
    high: pd.Series, low: pd.Series, close: pd.Series,
    atr_period: int = 14, lookback: int = 120
) -> pd.Series:
    """Rolling percentile rank of ATR% over a lookback window."""
    atr_pct = atr_percent(high, low, close, atr_period)
    def pct_rank(x):
        if len(x) < 2:
            return 50.0
        current = x.iloc[-1]
        return (x < current).sum() / (len(x) - 1) * 100
    return atr_pct.rolling(window=lookback, min_periods=lookback).apply(pct_rank, raw=False)


def highest_high(high: pd.Series, period: int) -> pd.Series:
    """Highest high over a rolling window."""
    return high.rolling(window=period, min_periods=period).max()


def lowest_low(low: pd.Series, period: int) -> pd.Series:
    """Lowest low over a rolling window."""
    return low.rolling(window=period, min_periods=period).min()


def range_ratio(high: pd.Series, low: pd.Series, close: pd.Series,
                short_period: int = 10, long_period: int = 30) -> Tuple[pd.Series, pd.Series]:
    """Range contraction: short range vs long range as fraction of close."""
    hh_short = highest_high(high, short_period)
    ll_short = lowest_low(low, short_period)
    range_short = (hh_short - ll_short) / close

    hh_long = highest_high(high, long_period)
    ll_long = lowest_low(low, long_period)
    range_long = (hh_long - ll_long) / close

    return range_short, range_long


def tight_closes(close: pd.Series, atr_series: pd.Series, window: int = 5, mult: float = 1.5) -> pd.Series:
    """Check if last N closes are within mult * ATR of their mean.
    Returns True/False series.
    """
    results = pd.Series(False, index=close.index)
    if len(close) < window:
        return results

    close_mean = close.rolling(window=window, min_periods=window).mean()
    max_dev = pd.Series(np.nan, index=close.index)

    for i in range(window - 1, len(close)):
        window_closes = close.iloc[i - window + 1:i + 1]
        mean_val = close_mean.iloc[i]
        dev = (window_closes - mean_val).abs().max()
        max_dev.iloc[i] = dev

    atr_threshold = atr_series * mult
    results = max_dev <= atr_threshold
    return results.fillna(False)


def sma_slope(sma_series: pd.Series, lookback: int = 10) -> pd.Series:
    """Check if SMA is rising: current > lookback periods ago."""
    return sma_series > sma_series.shift(lookback)


def sma_declining(sma_series: pd.Series) -> pd.Series:
    """Check if SMA is declining day-over-day."""
    return sma_series < sma_series.shift(1)


def sma_rising(sma_series: pd.Series) -> pd.Series:
    """Check if SMA is rising day-over-day."""
    return sma_series > sma_series.shift(1)


def consecutive_true(series: pd.Series, n: int) -> pd.Series:
    """Check if series has been True for N consecutive periods (including current)."""
    result = series.copy().astype(float)
    # Count consecutive True values ending at each point
    count = pd.Series(0, index=series.index, dtype=int)
    for i in range(len(series)):
        if series.iloc[i]:
            count.iloc[i] = (count.iloc[i-1] + 1) if i > 0 else 1
        else:
            count.iloc[i] = 0
    return count >= n


def relative_strength_vs_benchmark(
    stock_close: pd.Series, bench_close: pd.Series, period_days: int = 63
) -> pd.Series:
    """Relative strength: stock return vs benchmark over period.
    Positive = outperforming.
    """
    stock_ret = stock_close.pct_change(periods=period_days)
    bench_ret = bench_close.pct_change(periods=period_days)
    return stock_ret - bench_ret


def breakout_level(high: pd.Series, lookback: int = 40) -> pd.Series:
    """Rolling highest high as breakout level."""
    return highest_high(high, lookback)


def volume_pace(
    cumulative_volume_today: float,
    avg_daily_volume_20d: float,
    minutes_since_open: float,
    total_minutes: float = 390.0,
) -> float:
    """Relative volume pace: actual vs expected at this time of day."""
    if avg_daily_volume_20d <= 0 or minutes_since_open <= 0:
        return 0.0
    day_fraction = minutes_since_open / total_minutes
    expected = avg_daily_volume_20d * day_fraction
    if expected <= 0:
        return 0.0
    return cumulative_volume_today / expected


def spread_percent(bid: float, ask: float) -> float:
    """Bid-ask spread as a percentage of midpoint."""
    if bid <= 0 or ask <= 0:
        return float('inf')
    mid = (bid + ask) / 2
    return ((ask - bid) / mid) * 100


def avg_dollar_volume(close: pd.Series, volume: pd.Series, period: int = 20) -> pd.Series:
    """Average daily dollar volume over period."""
    dollar_vol = close * volume
    return dollar_vol.rolling(window=period, min_periods=period).mean()
