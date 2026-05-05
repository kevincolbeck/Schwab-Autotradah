"""
Schwab REST API client.

Endpoints used:
  /marketdata/v1/quotes          — real-time quotes (fallback / snapshot)
  /marketdata/v1/chains          — full options chain (for GEX + IV)
  /marketdata/v1/pricehistory    — historical OHLCV bars (for RVOL baseline)
"""

import httpx
import logging
from typing import Optional

from backend.data.schwab_auth import get_valid_token
from backend.config import SCHWAB_API_BASE

logger = logging.getLogger(__name__)

BASE = f"{SCHWAB_API_BASE}/marketdata/v1"


async def _get(path: str, params: dict = None) -> dict:
    token = await get_valid_token()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params or {},
        )
        resp.raise_for_status()
        return resp.json()


# ── Quotes ────────────────────────────────────────────────────────────────────

async def get_quotes(symbols: list[str]) -> dict:
    """Returns dict keyed by symbol with full quote data."""
    data = await _get("/quotes", {"symbols": ",".join(symbols), "indicative": "false"})
    return data


async def get_quote(symbol: str) -> dict:
    data = await get_quotes([symbol])
    return data.get(symbol, {})


# ── Options chain ─────────────────────────────────────────────────────────────

async def get_options_chain(
    symbol: str,
    contract_type: str = "ALL",     # ALL / CALL / PUT
    expiry_type: str = "DTE",       # DTE / ALL
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    strike_count: int = 40,          # strikes above + below ATM
) -> dict:
    """
    Returns the full options chain for a symbol.
    strike_count: number of strikes above AND below ATM to include.
    """
    params = {
        "symbol": symbol,
        "contractType": contract_type,
        "strikeCount": strike_count,
        "includeUnderlyingQuote": "true",
        "strategy": "SINGLE",
        "optionType": "S",           # standard (non-mini)
    }
    if from_date:
        params["fromDate"] = from_date
    if to_date:
        params["toDate"] = to_date

    return await _get("/chains", params)


# ── Price history ─────────────────────────────────────────────────────────────

async def get_price_history(
    symbol: str,
    period_type: str = "day",
    period: int = 20,
    frequency_type: str = "minute",
    frequency: int = 1,
    need_extended_hours: bool = False,
) -> dict:
    """
    Returns OHLCV bars.
    Default: 20 days of 1-minute bars (for RVOL baseline computation).
    """
    params = {
        "symbol": symbol,
        "periodType": period_type,
        "period": period,
        "frequencyType": frequency_type,
        "frequency": frequency,
        "needExtendedHoursData": str(need_extended_hours).lower(),
    }
    return await _get("/pricehistory", params)


async def get_daily_bars(symbol: str, days: int = 30) -> list[dict]:
    """Returns list of daily OHLCV dicts for IV rank computation."""
    # Schwab API: periodType=day only supports frequencyType=minute.
    # Daily bars require periodType=year with frequencyType=daily.
    data = await get_price_history(
        symbol,
        period_type="year",
        period=1,
        frequency_type="daily",
        frequency=1,
    )
    candles = data.get("candles", [])
    return candles[-days:] if len(candles) > days else candles


async def get_minute_bars(symbol: str, days: int = 10) -> list[dict]:
    """Returns list of 1-minute OHLCV dicts for RVOL baseline."""
    data = await get_price_history(
        symbol,
        period_type="day",
        period=days,
        frequency_type="minute",
        frequency=1,
        need_extended_hours=False,
    )
    return data.get("candles", [])
