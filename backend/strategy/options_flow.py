"""
Options flow sweep and large print detection.

Sweep: a single option strike sees volume >> its open interest in a short window,
       suggesting an institutional trader is aggressively filling across multiple
       exchanges (market order, directional conviction).

Large print: a single options trade with notional value > $200k (configurable).
             Notional = price × size × 100.

Both signals are confirmations, not gates. They add weight to the conviction score.
"""

import time
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from backend.config import SWEEP_VOL_OI_RATIO, LARGE_PRINT_NOTIONAL

logger = logging.getLogger(__name__)


@dataclass
class FlowEvent:
    ts: float
    underlying: str
    option_symbol: str
    side: str          # CALL / PUT (derived from option symbol)
    direction: str     # LONG (bought) or SHORT (sold) — inferred from aggressor
    strike: float
    price: float
    size: int
    notional: float
    event_type: str    # SWEEP or LARGE_PRINT
    vol_oi_ratio: Optional[float] = None


@dataclass
class FlowState:
    last_sweep: Optional[FlowEvent] = None
    last_large_print: Optional[FlowEvent] = None
    sweep_detected: bool = False
    large_print_detected: bool = False
    sweep_direction: Optional[str] = None
    large_print_direction: Optional[str] = None
    sweep_age_seconds: float = 999.0
    large_print_age_seconds: float = 999.0

    def is_sweep_fresh(self, max_age_seconds: float = 300) -> bool:
        return self.sweep_detected and self.sweep_age_seconds <= max_age_seconds

    def is_large_print_fresh(self, max_age_seconds: float = 300) -> bool:
        return self.large_print_detected and self.large_print_age_seconds <= max_age_seconds


def _parse_option_symbol(symbol: str) -> tuple[Optional[str], Optional[float], Optional[str]]:
    """
    Parse OCC option symbol: AAPL  250117C00150000
    Returns (underlying, strike, option_type).
    """
    try:
        symbol = symbol.strip()
        underlying = symbol[:6].strip()
        # characters 6-11: YYMMDD
        # character 12: C or P
        opt_type = "CALL" if symbol[12] == "C" else "PUT"
        strike = float(symbol[13:]) / 1000.0
        return underlying, strike, opt_type
    except Exception:
        return None, None, None


class OptionsFlowMonitor:
    """
    Tracks options sweeps and large prints for all tickers.
    Consumes option trade ticks from the stream client.
    """

    def __init__(self, lookback_seconds: int = 60):
        self.lookback_seconds = lookback_seconds
        # ticker → list of recent option volume ticks
        self._recent_ticks: dict[str, list[dict]] = defaultdict(list)
        # open interest snapshot (strike → oi), updated when chain is polled
        self._open_interest: dict[str, dict[float, int]] = defaultdict(dict)
        # last events per underlying
        self._flow_state: dict[str, FlowState] = defaultdict(FlowState)
        # Optional archive callback: fn(ticker, event_type, direction, ...)
        self._archive_cb = None

    def set_archive_callback(self, cb) -> None:
        """Register callback for persisting flow events to DuckDB."""
        self._archive_cb = cb

    def update_open_interest(self, underlying: str, chain_data: dict) -> None:
        """Called after each options chain poll to update OI per strike."""
        oi_map: dict[float, int] = {}
        for date_key, strikes in chain_data.get("callExpDateMap", {}).items():
            for strike_str, contracts in strikes.items():
                strike = float(strike_str.split(":")[0])
                oi = sum(c.get("openInterest", 0) or 0 for c in contracts)
                oi_map[strike] = oi_map.get(strike, 0) + oi
        for date_key, strikes in chain_data.get("putExpDateMap", {}).items():
            for strike_str, contracts in strikes.items():
                strike = float(strike_str.split(":")[0])
                oi = sum(c.get("openInterest", 0) or 0 for c in contracts)
                oi_map[strike] = oi_map.get(strike, 0) + oi
        self._open_interest[underlying] = oi_map

    def on_option_tick(self, symbol: str, price: float, size: int, ts: float) -> None:
        """Called for every option trade tick from the stream."""
        underlying, strike, opt_type = _parse_option_symbol(symbol)
        if not underlying or underlying not in {t for t in self._flow_state}:
            # Only track our tickers
            return

        notional = price * size * 100
        tick = {
            "ts": ts, "symbol": symbol, "underlying": underlying,
            "strike": strike, "opt_type": opt_type,
            "price": price, "size": size, "notional": notional,
        }
        self._recent_ticks[underlying].append(tick)
        self._purge_old(underlying, ts)
        self._evaluate(underlying, tick, ts)

    def register_ticker(self, ticker: str) -> None:
        """Ensure ticker is tracked."""
        if ticker not in self._flow_state:
            self._flow_state[ticker] = FlowState()

    def _purge_old(self, underlying: str, now: float) -> None:
        cutoff = now - self.lookback_seconds
        self._recent_ticks[underlying] = [
            t for t in self._recent_ticks[underlying] if t["ts"] >= cutoff
        ]

    def _evaluate(self, underlying: str, latest_tick: dict, ts: float) -> None:
        strike = latest_tick["strike"]
        if strike is None:
            return

        # Aggregate volume on this strike in the lookback window
        strike_vol = sum(
            t["size"] for t in self._recent_ticks[underlying]
            if t["strike"] == strike
        )
        oi = self._open_interest[underlying].get(strike, 0)

        state = self._flow_state[underlying]
        state.sweep_age_seconds = ts - (state.last_sweep.ts if state.last_sweep else 0)
        state.large_print_age_seconds = ts - (
            state.last_large_print.ts if state.last_large_print else 0)

        # Sweep detection
        if oi > 0 and (strike_vol / oi) >= SWEEP_VOL_OI_RATIO:
            direction = "LONG" if latest_tick["opt_type"] == "CALL" else "SHORT"
            ratio = strike_vol / oi
            event = FlowEvent(
                ts=ts, underlying=underlying, option_symbol=latest_tick["symbol"],
                side=latest_tick["opt_type"], direction=direction,
                strike=strike, price=latest_tick["price"], size=latest_tick["size"],
                notional=latest_tick["notional"], event_type="SWEEP",
                vol_oi_ratio=ratio,
            )
            state.last_sweep = event
            state.sweep_detected = True
            state.sweep_direction = direction
            state.sweep_age_seconds = 0
            logger.info(f"OPTIONS SWEEP {underlying}: {direction} on {strike} "
                        f"vol/OI={ratio:.1f}x")
            if self._archive_cb:
                try:
                    self._archive_cb(
                        underlying, "SWEEP", direction, latest_tick["symbol"],
                        strike, latest_tick["price"], latest_tick["size"],
                        latest_tick["notional"], ratio, latest_tick["opt_type"], ts,
                    )
                except Exception:
                    pass

        # Large print detection
        if latest_tick["notional"] >= LARGE_PRINT_NOTIONAL:
            direction = "LONG" if latest_tick["opt_type"] == "CALL" else "SHORT"
            event = FlowEvent(
                ts=ts, underlying=underlying, option_symbol=latest_tick["symbol"],
                side=latest_tick["opt_type"], direction=direction,
                strike=strike, price=latest_tick["price"], size=latest_tick["size"],
                notional=latest_tick["notional"], event_type="LARGE_PRINT",
            )
            state.last_large_print = event
            state.large_print_detected = True
            state.large_print_direction = direction
            state.large_print_age_seconds = 0
            logger.info(f"LARGE PRINT {underlying}: ${latest_tick['notional']:,.0f} "
                        f"{direction} on {strike}")
            if self._archive_cb:
                try:
                    self._archive_cb(
                        underlying, "LARGE_PRINT", direction, latest_tick["symbol"],
                        strike, latest_tick["price"], latest_tick["size"],
                        latest_tick["notional"], None, latest_tick["opt_type"], ts,
                    )
                except Exception:
                    pass

    def get_state(self, ticker: str) -> FlowState:
        now = time.time()
        state = self._flow_state.get(ticker, FlowState())
        if state.last_sweep:
            state.sweep_age_seconds = now - state.last_sweep.ts
        if state.last_large_print:
            state.large_print_age_seconds = now - state.last_large_print.ts
        return state


# Singleton
options_flow_monitor = OptionsFlowMonitor()
