"""
Key levels — pre-market computation + intraday tracking.

Levels computed at pre-market startup (9:00 AM ET) and refreshed intraday:
  GEX:           call wall, put wall, gamma flip (from options chain)
  Previous day:  PDH, PDL, PDC (previous day high/low/close)
  Opening range: high/low of first 15 minutes (9:30–9:45 ET) — computed live
  VWAP:          session VWAP (live, from vwap.py)
  Round numbers: nearest significant round-number zone

Role in strategy:
  ENTRY:  approaching a level from the correct side reinforces the signal
  EXIT:   approaching an OPPOSING level (resistance against position direction)
          while microstructure confirms → trigger early exit from paper_trader

The bot respects live market data above all — key levels are context, not commands.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, date, time as dtime
from typing import Optional

from backend.config import ET, ALL_TICKERS

logger = logging.getLogger(__name__)


@dataclass
class KeyLevels:
    ticker: str
    computed_at: datetime = field(default_factory=lambda: datetime.now(ET))

    # GEX levels (from gex_calculator)
    gex_call_wall: Optional[float] = None
    gex_put_wall: Optional[float] = None
    gex_gamma_flip: Optional[float] = None

    # Previous day structure
    prev_day_high: Optional[float] = None
    prev_day_low: Optional[float] = None
    prev_day_close: Optional[float] = None

    # Opening range (9:30–9:45 ET) — filled intraday
    opening_range_high: Optional[float] = None
    opening_range_low: Optional[float] = None
    opening_range_complete: bool = False

    # Nearest significant round numbers (above and below spot)
    round_number_above: Optional[float] = None
    round_number_below: Optional[float] = None

    # All levels consolidated — rebuilt on any change
    _support_levels: list[float] = field(default_factory=list)
    _resistance_levels: list[float] = field(default_factory=list)

    def rebuild(self, spot_price: float) -> None:
        """Recompute support/resistance from all available levels."""
        all_levels = [
            self.gex_call_wall,
            self.gex_put_wall,
            self.gex_gamma_flip,
            self.prev_day_high,
            self.prev_day_low,
            self.opening_range_high,
            self.opening_range_low,
            self.round_number_above,
            self.round_number_below,
        ]
        valid = [l for l in all_levels if l is not None]
        self._support_levels    = sorted([l for l in valid if l < spot_price], reverse=True)
        self._resistance_levels = sorted([l for l in valid if l > spot_price])

    def nearest_support(self, spot_price: float) -> Optional[float]:
        supports = [l for l in self._support_levels if l < spot_price]
        return supports[0] if supports else None

    def nearest_resistance(self, spot_price: float) -> Optional[float]:
        resistances = [l for l in self._resistance_levels if l > spot_price]
        return resistances[0] if resistances else None

    def is_approaching_resistance(
        self, spot_price: float, threshold_pct: float = 0.002
    ) -> tuple[bool, Optional[float]]:
        """True if price is within threshold_pct of any resistance level above."""
        res = self.nearest_resistance(spot_price)
        if res is None:
            return False, None
        pct = (res - spot_price) / spot_price
        return pct <= threshold_pct, res

    def is_approaching_support(
        self, spot_price: float, threshold_pct: float = 0.002
    ) -> tuple[bool, Optional[float]]:
        """True if price is within threshold_pct of any support level below."""
        sup = self.nearest_support(spot_price)
        if sup is None:
            return False, None
        pct = (spot_price - sup) / spot_price
        return pct <= threshold_pct, sup

    def should_exit_long(
        self,
        spot_price: float,
        footprint_delta_1m: Optional[float],
        l2_imbalance: Optional[float],
        resistance_threshold_pct: float = 0.002,
    ) -> tuple[bool, str]:
        """
        Returns (True, reason) if a LONG position should exit early.
        Requires BOTH an approaching resistance level AND confirming bearish microstructure.
        """
        near_res, res_level = self.is_approaching_resistance(spot_price, resistance_threshold_pct)
        if not near_res:
            return False, ""

        bearish_delta = footprint_delta_1m is not None and footprint_delta_1m < 0
        bearish_l2    = l2_imbalance is not None and l2_imbalance < -0.30

        if bearish_delta and bearish_l2:
            return True, f"OPPOSING_LEVEL_EXIT_LONG @ {res_level:.2f} (delta<0 + ask-heavy L2)"
        if bearish_delta:
            return True, f"OPPOSING_LEVEL_EXIT_LONG @ {res_level:.2f} (delta<0 near resistance)"
        return False, ""

    def should_exit_short(
        self,
        spot_price: float,
        footprint_delta_1m: Optional[float],
        l2_imbalance: Optional[float],
        support_threshold_pct: float = 0.002,
    ) -> tuple[bool, str]:
        """
        Returns (True, reason) if a SHORT position should exit early.
        Requires BOTH an approaching support level AND confirming bullish microstructure.
        """
        near_sup, sup_level = self.is_approaching_support(spot_price, support_threshold_pct)
        if not near_sup:
            return False, ""

        bullish_delta = footprint_delta_1m is not None and footprint_delta_1m > 0
        bullish_l2    = l2_imbalance is not None and l2_imbalance > 0.30

        if bullish_delta and bullish_l2:
            return True, f"OPPOSING_LEVEL_EXIT_SHORT @ {sup_level:.2f} (delta>0 + bid-heavy L2)"
        if bullish_delta:
            return True, f"OPPOSING_LEVEL_EXIT_SHORT @ {sup_level:.2f} (delta>0 near support)"
        return False, ""

    def to_dict(self) -> dict:
        return {
            "gex_call_wall": self.gex_call_wall,
            "gex_put_wall": self.gex_put_wall,
            "gex_gamma_flip": self.gex_gamma_flip,
            "prev_day_high": self.prev_day_high,
            "prev_day_low": self.prev_day_low,
            "prev_day_close": self.prev_day_close,
            "opening_range_high": self.opening_range_high,
            "opening_range_low": self.opening_range_low,
            "opening_range_complete": self.opening_range_complete,
            "round_number_above": self.round_number_above,
            "round_number_below": self.round_number_below,
        }


# ── Round number computation ──────────────────────────────────────────────────

def _round_number_interval(ticker: str) -> float:
    """Return the round-number snap interval for a ticker."""
    if ticker in ("SPY", "QQQ"):
        return 5.0
    if ticker in ("NVDA", "AMZN", "GOOGL", "TSLA"):
        return 10.0
    return 5.0   # AAPL, MSFT, META


def compute_round_numbers(ticker: str, spot_price: float) -> tuple[float, float]:
    interval = _round_number_interval(ticker)
    below = (spot_price // interval) * interval
    above = below + interval
    return above, below


# ── Previous day high/low/close ───────────────────────────────────────────────

async def fetch_prev_day_levels(ticker: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Fetch previous trading day OHLCV and return (high, low, close)."""
    try:
        from backend.data.schwab_rest import get_daily_bars
        bars = await get_daily_bars(ticker, days=5)
        if len(bars) >= 1:
            prev = bars[-1]   # last bar = previous trading day (called pre-market, no today bar)
            return float(prev["high"]), float(prev["low"]), float(prev["close"])
    except Exception as e:
        logger.warning(f"Key levels: prev day fetch failed for {ticker}: {e}")
    return None, None, None


# ── Opening range tracker ─────────────────────────────────────────────────────

class OpeningRangeTracker:
    """Tracks the 9:30–9:45 ET opening range for each ticker from the tick stream."""

    RANGE_END = dtime(9, 45)

    def __init__(self):
        self._levels: dict[str, KeyLevels] = {}
        self._session_date: Optional[date] = None

    def register(self, ticker: str, levels: KeyLevels) -> None:
        self._levels[ticker] = levels

    def on_tick(self, symbol: str, price: float, size: int, ts: float) -> None:
        if symbol not in self._levels:
            return
        now_et = datetime.now(ET)
        if now_et.time() >= self.RANGE_END:
            if not self._levels[symbol].opening_range_complete:
                self._levels[symbol].opening_range_complete = True
            return
        if now_et.time() < dtime(9, 30):
            return

        levels = self._levels[symbol]
        if levels.opening_range_high is None or price > levels.opening_range_high:
            levels.opening_range_high = price
        if levels.opening_range_low is None or price < levels.opening_range_low:
            levels.opening_range_low = price


# ── Registry ──────────────────────────────────────────────────────────────────

_key_levels: dict[str, KeyLevels] = {}
_opening_range_tracker = OpeningRangeTracker()


def get_key_levels(ticker: str) -> Optional[KeyLevels]:
    return _key_levels.get(ticker)


async def init_key_levels(ticker: str, spot_price: float, gex_levels=None) -> KeyLevels:
    """Build initial key levels for a ticker. Called during pre-market startup."""
    pdh, pdl, pdc = await fetch_prev_day_levels(ticker)
    round_above, round_below = compute_round_numbers(ticker, spot_price)

    levels = KeyLevels(
        ticker=ticker,
        prev_day_high=pdh,
        prev_day_low=pdl,
        prev_day_close=pdc,
        round_number_above=round_above,
        round_number_below=round_below,
    )

    if gex_levels:
        levels.gex_call_wall  = gex_levels.call_wall
        levels.gex_put_wall   = gex_levels.put_wall
        levels.gex_gamma_flip = gex_levels.gamma_flip

    levels.rebuild(spot_price)
    _key_levels[ticker] = levels
    _opening_range_tracker.register(ticker, levels)

    logger.info(
        f"Key levels {ticker}: PDH={pdh} PDL={pdl} "
        f"call_wall={gex_levels.call_wall if gex_levels else None} "
        f"put_wall={gex_levels.put_wall if gex_levels else None}"
    )
    return levels


def update_gex_in_key_levels(ticker: str, gex_levels, spot_price: float) -> None:
    """Sync updated GEX into key levels after each GEX refresh."""
    levels = _key_levels.get(ticker)
    if not levels or not gex_levels:
        return
    levels.gex_call_wall  = gex_levels.call_wall
    levels.gex_put_wall   = gex_levels.put_wall
    levels.gex_gamma_flip = gex_levels.gamma_flip
    round_above, round_below = compute_round_numbers(ticker, spot_price)
    levels.round_number_above = round_above
    levels.round_number_below = round_below
    levels.rebuild(spot_price)


def get_opening_range_tracker() -> OpeningRangeTracker:
    return _opening_range_tracker
