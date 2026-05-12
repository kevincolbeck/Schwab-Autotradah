"""Eligibility, trend quality, and volatility contraction filters."""

import logging
from typing import Dict, List, Optional, Tuple

import pandas as pd
import numpy as np

from core.config import Config
from data.market_data import MarketDataCache
from strategy.indicators import (
    sma, atr, atr_percent, atr_percentile, range_ratio, tight_closes,
    sma_slope, avg_dollar_volume, spread_percent, relative_strength_vs_benchmark,
)

logger = logging.getLogger(__name__)


class FilterResult:
    """Result of filtering a symbol."""
    def __init__(self, symbol: str, passed: bool, reason: str = ""):
        self.symbol = symbol
        self.passed = passed
        self.reason = reason

    def __repr__(self):
        status = "PASS" if self.passed else "FAIL"
        return f"FilterResult({self.symbol}: {status} - {self.reason})"


class SymbolFilter:
    """Applies all filters to determine if a symbol is tradeable."""

    def __init__(self, data_cache: MarketDataCache, config: Config):
        self.data = data_cache
        self.config = config

    def check_eligibility(self, symbol: str, quote: dict) -> FilterResult:
        """Hard eligibility filters: price, volume, spread, US equity."""
        # Price check
        last = quote.get('last', 0)
        if last < self.config.eligibility.min_price:
            return FilterResult(symbol, False, f"Price {last:.2f} < {self.config.eligibility.min_price}")

        # Spread check
        bid = quote.get('bid', 0)
        ask = quote.get('ask', 0)
        if bid > 0 and ask > 0:
            sprd = spread_percent(bid, ask)
            if sprd > self.config.eligibility.max_spread_pct:
                return FilterResult(symbol, False, f"Spread {sprd:.3f}% > {self.config.eligibility.max_spread_pct}%")

        # Asset type / exchange check
        if self.config.eligibility.us_equity_only:
            asset_type = quote.get('asset_type', '').upper()
            exchange = quote.get('exchange', '').upper()
            otc_exchanges = {'OTC', 'OTCBB', 'PINK', 'GREY'}
            if exchange in otc_exchanges:
                return FilterResult(symbol, False, f"OTC exchange: {exchange}")

        # Dollar volume check (from daily bars)
        df = self.data.get_daily_bars(symbol)
        if df is not None and len(df) >= 20:
            adv = avg_dollar_volume(df['close'], df['volume'], 20)
            latest_adv = adv.iloc[-1]
            if pd.notna(latest_adv) and latest_adv < self.config.eligibility.min_avg_dollar_volume_20d:
                return FilterResult(symbol, False, f"AvgDolVol ${latest_adv:,.0f} < ${self.config.eligibility.min_avg_dollar_volume_20d:,.0f}")

        return FilterResult(symbol, True, "Eligibility passed")

    def check_trend_quality(self, symbol: str) -> FilterResult:
        """Trend quality filter: above SMA50/200, SMA50 slope positive."""
        df = self.data.get_daily_bars(symbol)
        if df is None or len(df) < 200:
            return FilterResult(symbol, False, "Insufficient data for trend quality")

        close = df['close']
        latest_close = close.iloc[-1]

        # Close > SMA50
        if self.config.trend_quality.require_above_sma50:
            sma50 = sma(close, 50)
            if pd.isna(sma50.iloc[-1]) or latest_close <= sma50.iloc[-1]:
                return FilterResult(symbol, False, f"Close {latest_close:.2f} <= SMA50 {sma50.iloc[-1]:.2f}")

            # SMA50 slope positive
            lookback = self.config.trend_quality.sma50_slope_lookback
            if len(sma50.dropna()) > lookback:
                slope_ok = sma_slope(sma50, lookback)
                if not slope_ok.iloc[-1]:
                    return FilterResult(symbol, False, "SMA50 slope negative")

        # Close > SMA200
        if self.config.trend_quality.require_above_sma200:
            sma200 = sma(close, 200)
            if pd.isna(sma200.iloc[-1]) or latest_close <= sma200.iloc[-1]:
                return FilterResult(symbol, False, f"Close {latest_close:.2f} <= SMA200 {sma200.iloc[-1]:.2f}")

        return FilterResult(symbol, True, "Trend quality passed")

    def check_relative_strength(self, symbol: str, benchmark_df: Optional[pd.DataFrame]) -> FilterResult:
        """Optional: check relative strength vs SPY."""
        if not self.config.trend_quality.require_relative_strength_vs_spy:
            return FilterResult(symbol, True, "RS check disabled")

        df = self.data.get_daily_bars(symbol)
        if df is None or benchmark_df is None:
            return FilterResult(symbol, False, "Insufficient data for RS")

        months = self.config.trend_quality.relative_strength_months
        period_days = months * 21  # ~21 trading days per month

        if len(df) < period_days or len(benchmark_df) < period_days:
            return FilterResult(symbol, False, "Insufficient data for RS period")

        rs = relative_strength_vs_benchmark(df['close'], benchmark_df['close'], period_days)
        if pd.isna(rs.iloc[-1]) or rs.iloc[-1] <= 0:
            return FilterResult(symbol, False, f"RS vs SPY negative: {rs.iloc[-1]:.4f}")

        return FilterResult(symbol, True, f"RS vs SPY positive: {rs.iloc[-1]:.4f}")

    def check_contraction(self, symbol: str) -> FilterResult:
        """Volatility contraction filter: 2 of 3 conditions must pass."""
        df = self.data.get_daily_bars(symbol)
        if df is None or len(df) < self.config.contraction.atr_percentile_window:
            return FilterResult(symbol, False, "Insufficient data for contraction")

        high = df['high']
        low = df['low']
        close = df['close']
        cfg = self.config.contraction

        conditions_passed = 0
        reasons = []

        # 1) ATR contraction: ATR% in bottom 25th percentile of last 120 days
        atr_pct_rank = atr_percentile(high, low, close, cfg.atr_period, cfg.atr_percentile_window)
        if pd.notna(atr_pct_rank.iloc[-1]) and atr_pct_rank.iloc[-1] <= cfg.atr_percentile_threshold:
            conditions_passed += 1
            reasons.append(f"ATR%ile={atr_pct_rank.iloc[-1]:.1f}")
        else:
            reasons.append(f"ATR%ile={atr_pct_rank.iloc[-1]:.1f} (needs <={cfg.atr_percentile_threshold})")

        # 2) Range contraction: Range10 < Range30
        range_short, range_long = range_ratio(high, low, close, cfg.range_short, cfg.range_long)
        if pd.notna(range_short.iloc[-1]) and pd.notna(range_long.iloc[-1]):
            if range_short.iloc[-1] < range_long.iloc[-1]:
                conditions_passed += 1
                reasons.append(f"Range10={range_short.iloc[-1]:.4f}<Range30={range_long.iloc[-1]:.4f}")
            else:
                reasons.append(f"Range10={range_short.iloc[-1]:.4f}>=Range30={range_long.iloc[-1]:.4f}")

        # 3) Tight closes: max deviation of last 5 closes <= 1.5 * ATR(14)
        atr_series = atr(high, low, close, cfg.atr_period)
        tc = tight_closes(close, atr_series, cfg.tight_closes_window, cfg.tight_closes_atr_mult)
        if tc.iloc[-1]:
            conditions_passed += 1
            reasons.append("Tight closes: YES")
        else:
            reasons.append("Tight closes: NO")

        passed = conditions_passed >= cfg.min_conditions_pass
        reason_str = f"Contraction {conditions_passed}/3: {'; '.join(reasons)}"
        return FilterResult(symbol, passed, reason_str)

    def apply_all_filters(
        self, symbol: str, quote: dict, benchmark_df: Optional[pd.DataFrame] = None,
        stricter: bool = False,
    ) -> Tuple[bool, str]:
        """Apply all filters in sequence. Returns (passed, reason)."""
        # Eligibility
        result = self.check_eligibility(symbol, quote)
        if not result.passed:
            return False, result.reason

        # Trend quality
        result = self.check_trend_quality(symbol)
        if not result.passed:
            return False, result.reason

        # Relative strength (optional)
        result = self.check_relative_strength(symbol, benchmark_df)
        if not result.passed:
            return False, result.reason

        # Volatility contraction
        result = self.check_contraction(symbol)
        if not result.passed:
            return False, result.reason

        return True, "All filters passed"
