"""
IV Rank computation.

IV Rank = (current_IV - 52w_low) / (52w_high - 52w_low) × 100

A rank of 0 = IV at yearly low (cheap options).
A rank of 100 = IV at yearly high (expensive options).

Gate: don't buy options when IV rank > 60 (we'd be paying too much premium).

ATM IV is extracted from the options chain nearest-expiry ATM strike.
Daily values are stored in the iv_history table so rank compounds over time.
"""

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import select, func

from backend.database import AsyncSessionLocal, IvHistory
from backend.config import IV_RANK_MAX, IV_RANK_LOOKBACK_DAYS

logger = logging.getLogger(__name__)

# In-memory cache: ticker → (iv_rank, computed_at)
_cache: dict[str, tuple[float, datetime]] = {}
_CACHE_SECONDS = 300   # recompute at most every 5 min


def _extract_atm_iv(chain_data: dict, spot_price: float) -> Optional[float]:
    """Find the ATM IV from the options chain (nearest-expiry, nearest-to-spot strike)."""
    best_iv: Optional[float] = None
    best_distance = float("inf")

    for date_key, strikes in chain_data.get("callExpDateMap", {}).items():
        for strike_str, contracts in strikes.items():
            strike = float(strike_str.split(":")[0])
            dist = abs(strike - spot_price)
            if dist < best_distance:
                for c in contracts:
                    iv = c.get("volatility")
                    if iv and iv > 0:
                        best_iv = iv / 100.0   # Schwab returns as percentage
                        best_distance = dist
                        break
    return best_iv


async def record_daily_iv(ticker: str, chain_data: dict, spot_price: float) -> None:
    """Extract and store today's ATM IV. Called once per day per ticker."""
    atm_iv = _extract_atm_iv(chain_data, spot_price)
    if atm_iv is None:
        logger.warning(f"IV rank: could not extract ATM IV for {ticker}")
        return

    today_str = date.today().isoformat()
    async with AsyncSessionLocal() as session:
        existing = await session.execute(
            select(IvHistory).where(
                IvHistory.ticker == ticker,
                IvHistory.date == today_str,
            )
        )
        row = existing.scalar_one_or_none()
        if row:
            row.atm_iv = atm_iv
        else:
            session.add(IvHistory(date=today_str, ticker=ticker, atm_iv=atm_iv))
        await session.commit()


async def get_iv_rank(ticker: str) -> Optional[float]:
    """Return current IV rank (0–100) using stored history."""
    now = datetime.utcnow()
    cached = _cache.get(ticker)
    if cached and (now - cached[1]).total_seconds() < _CACHE_SECONDS:
        return cached[0]

    cutoff = (date.today() - timedelta(days=IV_RANK_LOOKBACK_DAYS)).isoformat()

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(IvHistory).where(
                IvHistory.ticker == ticker,
                IvHistory.date >= cutoff,
            ).order_by(IvHistory.date)
        )
        rows = result.scalars().all()

    if len(rows) < 3:
        logger.debug(f"IV rank: insufficient history for {ticker} ({len(rows)} days)")
        return None

    ivs = [r.atm_iv for r in rows]
    current_iv = ivs[-1]
    low_iv  = min(ivs)
    high_iv = max(ivs)

    if high_iv == low_iv:
        rank = 50.0
    else:
        rank = (current_iv - low_iv) / (high_iv - low_iv) * 100

    _cache[ticker] = (rank, now)
    return rank


async def iv_rank_passes_gate(ticker: str) -> tuple[bool, Optional[float]]:
    """Returns (passes, iv_rank). Gate fails if IV rank > IV_RANK_MAX."""
    rank = await get_iv_rank(ticker)
    if rank is None:
        return (True, None)   # no history yet → allow, don't gate on uncertainty
    return (rank <= IV_RANK_MAX, rank)
