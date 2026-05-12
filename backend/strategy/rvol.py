"""
Relative Volume (RVOL) computation.

RVOL = current_session_volume / avg_volume_at_same_time_of_day (20-day lookback)

Built from the 1-minute OHLCV bars fetched via Schwab REST at startup,
then updated in real-time from the streaming volume on L1 quotes.

RVOL > 1.3 required for a signal to pass Gate 2.
"""

import logging
from collections import defaultdict
from datetime import datetime, date, time as dtime
from typing import Optional

import pytz

from backend.config import ET, RVOL_MIN_THRESHOLD, RVOL_LOOKBACK_DAYS

logger = logging.getLogger(__name__)


class RvolMonitor:
    """Per-ticker relative volume tracker."""

    def __init__(self, ticker: str):
        self.ticker = ticker
        # avg_volume_by_minute[minute_of_day] = avg volume across 20 days
        self._avg_volume_by_minute: dict[int, float] = {}
        self._session_volume: float = 0.0
        self._session_start_volume: float = 0.0
        self._volume_initialized = False
        self._last_total_volume: float = 0.0

    def load_historical(self, minute_bars: list[dict]) -> None:
        """
        Pre-compute average intraday volume profile from historical 1-minute bars.
        minute_bars: list of {datetime (ms epoch), open, high, low, close, volume}
        """
        from collections import defaultdict
        by_minute: dict[int, list[float]] = defaultdict(list)

        for bar in minute_bars:
            ts = bar.get("datetime", 0)
            vol = bar.get("volume", 0)
            if not ts or not vol:
                continue
            dt = datetime.fromtimestamp(ts / 1000, tz=ET)
            # Only regular session bars
            if not (dtime(9, 30) <= dt.time() < dtime(16, 0)):
                continue
            minute_of_day = dt.hour * 60 + dt.minute
            by_minute[minute_of_day].append(float(vol))

        self._avg_volume_by_minute = {
            m: sum(vols) / len(vols)
            for m, vols in by_minute.items()
            if vols
        }
        logger.info(f"RVOL {self.ticker}: loaded {len(self._avg_volume_by_minute)} minute profiles")

    def on_quote_update(self, symbol: str, quote: dict) -> None:
        """Called on each L1 quote update; extracts running session volume."""
        if symbol != self.ticker:
            return
        total_vol = float(quote.get("total_volume", 0) or 0)
        if total_vol <= 0:
            return

        if not self._volume_initialized or total_vol < self._session_start_volume:
            # First quote, or daily counter reset overnight — re-baseline.
            self._session_start_volume = total_vol
            self._volume_initialized = True

        self._session_volume = total_vol - self._session_start_volume
        self._last_total_volume = total_vol

    def get_rvol(self) -> Optional[float]:
        """Return current RVOL ratio. None if no baseline data."""
        if not self._avg_volume_by_minute:
            return None

        now_et = datetime.now(ET)
        minute_of_day = now_et.hour * 60 + now_et.minute

        # Sum expected cumulative volume from open to now
        open_minute = 9 * 60 + 30
        expected_vol = sum(
            self._avg_volume_by_minute.get(m, 0)
            for m in range(open_minute, minute_of_day + 1)
        )
        if expected_vol <= 0:
            return None

        return self._session_volume / expected_vol

    def passes_gate(self) -> tuple[bool, Optional[float]]:
        rvol = self.get_rvol()
        if rvol is None:
            return (True, None)   # no baseline → don't block
        return (rvol >= RVOL_MIN_THRESHOLD, rvol)


# ── Registry ──────────────────────────────────────────────────────────────────

_monitors: dict[str, RvolMonitor] = {}


def get_rvol_monitor(ticker: str) -> RvolMonitor:
    if ticker not in _monitors:
        _monitors[ticker] = RvolMonitor(ticker)
    return _monitors[ticker]
