"""
VIX regime classification and position sizing modifiers.

Regimes:
  LOW      VIX < 15   → full sizing, all tickers active
  NORMAL   15–25      → full sizing, all tickers active
  ELEVATED 25–30      → Mag7 paused, SPY/QQQ only, size × 0.75
  HIGH     > 30       → SPY/QQQ only, size × 0.50

VIX is streamed via L1 quote on "$VIX" from Schwab.
"""

import logging
from typing import Optional

from backend.config import VIX_ELEVATED, VIX_HIGH, MAG7, ETFS

logger = logging.getLogger(__name__)

_current_vix: Optional[float] = None


def update_vix(vix_level: float) -> None:
    global _current_vix
    _current_vix = vix_level


def get_vix() -> Optional[float]:
    return _current_vix


def get_regime() -> str:
    if _current_vix is None:
        return "NORMAL"
    if _current_vix < 15:
        return "LOW"
    if _current_vix < VIX_ELEVATED:
        return "NORMAL"
    if _current_vix < VIX_HIGH:
        return "ELEVATED"
    return "HIGH"


def is_ticker_allowed(ticker: str) -> bool:
    """Return False if the current VIX regime blocks trading this ticker."""
    regime = get_regime()
    if regime in ("ELEVATED", "HIGH") and ticker in MAG7:
        return False
    return True


def size_modifier() -> float:
    """Returns a multiplier applied to position size based on VIX regime."""
    regime = get_regime()
    if regime == "ELEVATED":
        return 0.75
    if regime == "HIGH":
        return 0.50
    return 1.0


class VixMonitor:
    """Registered on the stream client to receive $VIX L1 quote updates."""

    def on_quote_update(self, symbol: str, quote: dict) -> None:
        if symbol not in ("$VIX", "VIX"):
            return
        level = float(quote.get("last", 0) or quote.get("bid", 0) or 0)
        if level > 0:
            update_vix(level)
            logger.debug(f"VIX updated: {level:.2f} ({get_regime()})")


vix_monitor = VixMonitor()
