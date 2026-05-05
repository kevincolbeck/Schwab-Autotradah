"""
Footprint candle builder.

Receives raw trade ticks from the Schwab TIMESALE_EQUITY stream.
For each tick, determines whether it was buyer-initiated (ask side) or
seller-initiated (bid side) using the Lee-Ready tick rule:
  - price > last bid/ask midpoint → buyer-initiated
  - price < midpoint              → seller-initiated
  - price == midpoint             → use uptick/downtick rule

Aggregates ticks into N-second candles, tracking:
  - bid_vol[price_level]: volume transacted at bid (selling)
  - ask_vol[price_level]: volume transacted at ask (buying)
  - delta = ask_vol - bid_vol (net buying pressure)

Divergence detection:
  - Price makes a new high but cumulative delta is declining → bearish absorption
  - Price makes a new low but cumulative delta is rising → bullish absorption
  Both are strong reversal signals used by footprint traders.
"""

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from backend.config import FOOTPRINT_CANDLE_SECONDS


@dataclass
class FootprintCandle:
    open_ts: float
    close_ts: float
    ticker: str
    open_price: float = 0.0
    high_price: float = 0.0
    low_price: float = float("inf")
    close_price: float = 0.0
    total_volume: int = 0
    bid_vol: dict = field(default_factory=lambda: defaultdict(int))   # price → vol
    ask_vol: dict = field(default_factory=lambda: defaultdict(int))
    delta: float = 0.0        # total ask_vol - bid_vol for candle
    cumulative_delta: float = 0.0   # running delta across candles (set by builder)
    is_closed: bool = False

    @property
    def imbalance(self) -> float:
        """Ask fraction of total volume. 0.5 = neutral. >0.6 = buy pressure."""
        total = sum(self.ask_vol.values()) + sum(self.bid_vol.values())
        if total == 0:
            return 0.5
        return sum(self.ask_vol.values()) / total


class FootprintBuilder:
    """
    Per-ticker footprint candle builder.
    Registered as a tick callback on the stream client.
    """

    def __init__(self, ticker: str, candle_seconds: int = FOOTPRINT_CANDLE_SECONDS):
        self.ticker = ticker
        self.candle_seconds = candle_seconds

        self._current: Optional[FootprintCandle] = None
        self._closed: list[FootprintCandle] = []    # last N closed candles
        self._max_history = 30                       # keep 30 min of 1m candles

        self._last_price: float = 0.0
        self._last_bid: float = 0.0
        self._last_ask: float = 0.0
        self._cumulative_delta: float = 0.0

        # Divergence state
        self._divergence_bars: int = 0         # consecutive bars with absorption
        self._divergence_side: Optional[str] = None

    def update_quote(self, bid: float, ask: float) -> None:
        """Called when the L1 quote updates — needed for tick rule."""
        self._last_bid = bid
        self._last_ask = ask

    def on_tick(self, symbol: str, price: float, size: int, ts: float) -> None:
        """Tick callback registered on stream_client.on_equity_tick()."""
        if symbol != self.ticker:
            return
        self._ensure_candle(ts, price)
        side = self._classify_tick(price)
        price_level = round(price, 2)

        if side == "ASK":
            self._current.ask_vol[price_level] += size
        else:
            self._current.bid_vol[price_level] += size

        self._current.total_volume += size
        if self._current.open_price == 0:
            self._current.open_price = price
        self._current.high_price  = max(self._current.high_price, price)
        self._current.low_price   = min(self._current.low_price, price)
        self._current.close_price = price
        self._last_price = price

        # Check if candle should close
        if ts >= self._current.close_ts:
            self._close_candle()

    def _ensure_candle(self, ts: float, price: float) -> None:
        if self._current is None or ts >= self._current.close_ts:
            self._close_candle()
            candle_start = (ts // self.candle_seconds) * self.candle_seconds
            self._current = FootprintCandle(
                open_ts=candle_start,
                close_ts=candle_start + self.candle_seconds,
                ticker=self.ticker,
            )

    def _close_candle(self) -> None:
        if self._current is None:
            return
        c = self._current
        c.delta = sum(c.ask_vol.values()) - sum(c.bid_vol.values())
        self._cumulative_delta += c.delta
        c.cumulative_delta = self._cumulative_delta
        c.is_closed = True
        self._closed.append(c)
        if len(self._closed) > self._max_history:
            self._closed.pop(0)
        self._current = None
        self._update_divergence()

    def _classify_tick(self, price: float) -> str:
        """
        Lee-Ready rule: classify tick as ASK (buyer) or BID (seller).
        Falls back to uptick rule when price == midpoint.
        """
        if self._last_bid <= 0 or self._last_ask <= 0:
            # No quote data yet — use uptick rule
            if price > self._last_price:
                return "ASK"
            return "BID"
        mid = (self._last_bid + self._last_ask) / 2
        if price >= self._last_ask:
            return "ASK"
        if price <= self._last_bid:
            return "BID"
        # At mid — use uptick/downtick
        if price > self._last_price:
            return "ASK"
        return "BID"

    def _update_divergence(self) -> None:
        """
        Detect absorption pattern:
        - Bullish absorption: price lower low but delta higher (buyers absorbing selling)
        - Bearish absorption: price higher high but delta lower (sellers absorbing buying)
        """
        if len(self._closed) < 2:
            self._divergence_bars = 0
            self._divergence_side = None
            return

        cur  = self._closed[-1]
        prev = self._closed[-2]

        price_higher = cur.high_price > prev.high_price
        price_lower  = cur.low_price  < prev.low_price
        delta_higher = cur.delta > prev.delta
        delta_lower  = cur.delta < prev.delta

        bearish_absorption = price_higher and delta_lower   # price up, selling pressure
        bullish_absorption = price_lower  and delta_higher  # price down, buying pressure

        if bearish_absorption:
            if self._divergence_side == "BEARISH":
                self._divergence_bars += 1
            else:
                self._divergence_bars = 1
                self._divergence_side = "BEARISH"
        elif bullish_absorption:
            if self._divergence_side == "BULLISH":
                self._divergence_bars += 1
            else:
                self._divergence_bars = 1
                self._divergence_side = "BULLISH"
        else:
            self._divergence_bars = 0
            self._divergence_side = None

    # ── Public accessors ──────────────────────────────────────────────────────

    def get_delta_1m(self) -> float:
        """Delta of the most recently closed 1-minute candle."""
        if not self._closed:
            return 0.0
        return self._closed[-1].delta

    def get_delta_5m(self) -> float:
        """Sum of delta over the last 5 closed candles."""
        recent = self._closed[-5:] if len(self._closed) >= 5 else self._closed
        return sum(c.delta for c in recent)

    def get_absorption(self) -> tuple[bool, Optional[str]]:
        """Returns (absorption_detected, side) based on divergence_bars threshold."""
        from backend.config import FOOTPRINT_DIVERGENCE_BARS
        if self._divergence_bars >= FOOTPRINT_DIVERGENCE_BARS:
            return (True, self._divergence_side)
        return (False, None)

    def get_recent_candles(self, n: int = 5) -> list[FootprintCandle]:
        return self._closed[-n:] if self._closed else []


# ── Registry ──────────────────────────────────────────────────────────────────

_builders: dict[str, FootprintBuilder] = {}


def get_builder(ticker: str) -> FootprintBuilder:
    if ticker not in _builders:
        _builders[ticker] = FootprintBuilder(ticker)
    return _builders[ticker]


def all_builders() -> dict[str, FootprintBuilder]:
    return _builders
