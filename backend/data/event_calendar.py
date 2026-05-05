"""
Earnings and macro event calendar.

Uses Finnhub free-tier API for earnings dates.
Macro events (FOMC, CPI, NFP) are checked via a known schedule embedded here
and updated periodically.

Gate logic:
  - is_earnings_day(ticker): True if ticker has earnings within 24h
  - is_macro_event_today(): True if a major macro event is today (FOMC, CPI, NFP)
"""

import httpx
import logging
from datetime import date, datetime, timedelta
from typing import Optional
import pytz

from backend.config import settings, ET, ALL_TICKERS

logger = logging.getLogger(__name__)

FINNHUB_BASE = "https://finnhub.io/api/v1"

# Cache
_earnings_cache: dict[str, list[str]] = {}   # ticker → list of date strings "YYYY-MM-DD"
_earnings_last_fetch: Optional[datetime] = None
_CACHE_TTL_HOURS = 12

# Known 2025-2026 major macro dates (FOMC, CPI, NFP) — update periodically
# Format: "YYYY-MM-DD"
KNOWN_MACRO_DATES = {
    # FOMC decision days
    "2025-09-17", "2025-11-05", "2025-12-10",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-10",
    "2026-07-29", "2026-09-16", "2026-11-04", "2026-12-16",
    # CPI release days (first few of 2026)
    "2026-01-15", "2026-02-12", "2026-03-12", "2026-04-10",
    "2026-05-13", "2026-06-10", "2026-07-10",
    # NFP (first Friday of month)
    "2026-01-02", "2026-02-06", "2026-03-06", "2026-04-03",
    "2026-05-01", "2026-06-05", "2026-07-02",
}


async def _fetch_earnings(ticker: str, lookahead_days: int = 7) -> list[str]:
    """Fetch upcoming earnings dates for a ticker from Finnhub."""
    if not settings.finnhub_api_key:
        return []
    today = date.today()
    to_date = today + timedelta(days=lookahead_days)
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{FINNHUB_BASE}/calendar/earnings",
                params={
                    "from": today.isoformat(),
                    "to": to_date.isoformat(),
                    "symbol": ticker,
                    "token": settings.finnhub_api_key,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            dates = [
                item["date"]
                for item in data.get("earningsCalendar", [])
                if item.get("symbol") == ticker
            ]
            return dates
    except Exception as e:
        logger.warning(f"Earnings fetch failed for {ticker}: {e}")
        return []


async def refresh_earnings_cache() -> None:
    global _earnings_cache, _earnings_last_fetch
    for ticker in ALL_TICKERS:
        _earnings_cache[ticker] = await _fetch_earnings(ticker)
    _earnings_last_fetch = datetime.now(ET)
    logger.info(f"Earnings cache refreshed: {_earnings_cache}")


async def is_earnings_day(ticker: str) -> bool:
    """True if ticker reports earnings today or tomorrow (within 24h)."""
    global _earnings_cache, _earnings_last_fetch

    # Refresh if stale or first run
    now = datetime.now(ET)
    if (_earnings_last_fetch is None or
            (now - _earnings_last_fetch).total_seconds() > _CACHE_TTL_HOURS * 3600):
        await refresh_earnings_cache()

    today_str    = date.today().isoformat()
    tomorrow_str = (date.today() + timedelta(days=1)).isoformat()
    dates = _earnings_cache.get(ticker, [])
    return today_str in dates or tomorrow_str in dates


def is_macro_event_today() -> bool:
    """True if today is a known major macro event day."""
    today_str = date.today().isoformat()
    return today_str in KNOWN_MACRO_DATES


def is_fomc_window() -> bool:
    """
    True during the 4-hour window around a FOMC announcement.
    FOMC typically releases at 2:00 PM ET; window = 10:00 AM - 3:00 PM ET.
    """
    if not is_macro_event_today():
        return False
    now_et = datetime.now(ET)
    return 10 <= now_et.hour < 15
