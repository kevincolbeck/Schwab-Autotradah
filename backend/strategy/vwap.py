"""
Session VWAP computation.

VWAP = Σ(price × volume) / Σ(volume), cumulated from 9:30 AM ET.

Updated on every L1 trade tick (price × size contribution).
Used for context (dist_from_vwap_pct) and as a soft filter:
  price far below VWAP + long signal → lower conviction (fighting trend)
  price far above VWAP + short signal → lower conviction
"""

from datetime import datetime, date
from typing import Optional
import pytz

from backend.config import ET


class VwapCalculator:
    """Per-ticker session VWAP."""

    def __init__(self, ticker: str):
        self.ticker = ticker
        self._cum_pv: float = 0.0     # cumulative price × volume
        self._cum_vol: float = 0.0    # cumulative volume
        self._session_date: Optional[date] = None
        self.vwap: Optional[float] = None

    def _reset_if_new_session(self) -> None:
        today = datetime.now(ET).date()
        if self._session_date != today:
            self._cum_pv = 0.0
            self._cum_vol = 0.0
            self._session_date = today
            self.vwap = None

    def on_tick(self, symbol: str, price: float, size: int, ts: float) -> None:
        if symbol != self.ticker:
            return
        self._reset_if_new_session()
        self._cum_pv += price * size
        self._cum_vol += size
        if self._cum_vol > 0:
            self.vwap = self._cum_pv / self._cum_vol

    def get_vwap(self) -> Optional[float]:
        return self.vwap

    def dist_from_vwap_pct(self, price: float) -> Optional[float]:
        """Returns signed % distance from VWAP (positive = above VWAP)."""
        if self.vwap and self.vwap > 0:
            return (price - self.vwap) / self.vwap
        return None

    def vwap_bias(self, price: float) -> Optional[str]:
        """Returns 'ABOVE', 'BELOW', or None."""
        d = self.dist_from_vwap_pct(price)
        if d is None:
            return None
        return "ABOVE" if d >= 0 else "BELOW"


# ── Registry ──────────────────────────────────────────────────────────────────

_calculators: dict[str, VwapCalculator] = {}


def get_vwap_calc(ticker: str) -> VwapCalculator:
    if ticker not in _calculators:
        _calculators[ticker] = VwapCalculator(ticker)
    return _calculators[ticker]
