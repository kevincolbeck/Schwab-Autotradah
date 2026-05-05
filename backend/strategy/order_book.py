"""
L2 order book analysis.

Receives NYSE_BOOK / NASDAQ_BOOK updates from the stream.
Tracks:
  - Largest bid and ask walls (price levels with highest size)
  - Book imbalance ratio (bid depth vs ask depth in a ±0.5% band around mid)
  - Large wall presence near GEX levels

Output used by signal engine as a confirmation signal.
"""

from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from backend.config import L2_IMBALANCE_THRESHOLD


@dataclass
class BookState:
    ticker: str
    bid_wall_price: float = 0.0
    bid_wall_size: float = 0.0
    ask_wall_price: float = 0.0
    ask_wall_size: float = 0.0
    imbalance: float = 0.0      # +1 = all bids, -1 = all asks, 0 = neutral
    mid_price: float = 0.0
    last_update: float = 0.0

    def imbalance_direction(self) -> Optional[str]:
        """Returns 'LONG' if heavy bids, 'SHORT' if heavy asks, None if neutral."""
        if self.imbalance >= L2_IMBALANCE_THRESHOLD:
            return "LONG"
        if self.imbalance <= -L2_IMBALANCE_THRESHOLD:
            return "SHORT"
        return None

    def wall_near_level(self, level: float, tolerance_pct: float = 0.002) -> Optional[str]:
        """Returns 'BID' / 'ASK' if a large wall is within tolerance_pct of a given price level."""
        if self.mid_price <= 0:
            return None
        if (abs(self.bid_wall_price - level) / self.mid_price) <= tolerance_pct:
            return "BID"
        if (abs(self.ask_wall_price - level) / self.mid_price) <= tolerance_pct:
            return "ASK"
        return None


class OrderBookMonitor:
    """Maintains L2 state for one ticker."""

    def __init__(self, ticker: str, depth_pct: float = 0.005):
        self.ticker = ticker
        self.depth_pct = depth_pct      # analyse ±0.5% around mid for imbalance
        self._bids: dict[float, float] = {}   # price → size
        self._asks: dict[float, float] = {}
        self.state = BookState(ticker=ticker)

    def on_l2_update(self, symbol: str, bids: list, asks: list) -> None:
        if symbol != self.ticker:
            return

        # Parse bid/ask levels — Schwab sends [price, size, count] per level
        self._bids = {}
        self._asks = {}
        for entry in bids:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                try:
                    self._bids[float(entry[0])] = float(entry[1])
                except (ValueError, TypeError):
                    pass

        for entry in asks:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                try:
                    self._asks[float(entry[0])] = float(entry[1])
                except (ValueError, TypeError):
                    pass

        self._recompute()

    def _recompute(self) -> None:
        import time as _time

        if not self._bids or not self._asks:
            return

        best_bid = max(self._bids)
        best_ask = min(self._asks)
        mid = (best_bid + best_ask) / 2
        self.state.mid_price = mid

        # Largest walls
        if self._bids:
            bwp = max(self._bids, key=self._bids.get)
            self.state.bid_wall_price = bwp
            self.state.bid_wall_size  = self._bids[bwp]

        if self._asks:
            awp = max(self._asks, key=self._asks.get)
            self.state.ask_wall_price = awp
            self.state.ask_wall_size  = self._asks[awp]

        # Imbalance within ±depth_pct band
        band = mid * self.depth_pct
        bid_depth = sum(v for p, v in self._bids.items() if p >= mid - band)
        ask_depth = sum(v for p, v in self._asks.items() if p <= mid + band)
        total = bid_depth + ask_depth
        if total > 0:
            # +1 = fully bid-heavy, -1 = fully ask-heavy
            self.state.imbalance = (bid_depth - ask_depth) / total
        else:
            self.state.imbalance = 0.0

        self.state.last_update = _time.time()

    def get_state(self) -> BookState:
        return self.state


# ── Registry ──────────────────────────────────────────────────────────────────

_monitors: dict[str, OrderBookMonitor] = {}


def get_monitor(ticker: str) -> OrderBookMonitor:
    if ticker not in _monitors:
        _monitors[ticker] = OrderBookMonitor(ticker)
    return _monitors[ticker]
