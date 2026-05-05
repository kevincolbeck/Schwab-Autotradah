"""
Lazy outcome filler — run as a cron job or after market close.

For every SignalOutcome row with unfilled horizons, fetches the price
at +5m, +15m, +30m, +1h, +2h from the DuckDB tick archive and
fills in the return and correct columns.

Usage:
  python scripts/outcome_filler.py
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
from backend.database import AsyncSessionLocal, SignalOutcome, SignalTick
from backend.data.tick_archive import tick_archive
from sqlalchemy import select


HORIZONS = {
    "5m":  5  * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h":  60 * 60,
    "2h":  120 * 60,
}


async def fill_outcomes():
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(SignalOutcome, SignalTick)
            .join(SignalTick, SignalOutcome.signal_tick_id == SignalTick.id)
            .where(SignalOutcome.price_1h == None)   # unfilled
        )
        rows = result.all()

    print(f"Filling {len(rows)} unfilled outcomes...")

    for outcome, tick in rows:
        entry_ts = tick.ts.timestamp()
        entry_price = outcome.entry_price or tick.price
        direction = outcome.direction or tick.direction

        if not direction or not entry_price:
            continue

        updates = {}
        now_ts = datetime.utcnow().timestamp()

        for label, offset in HORIZONS.items():
            target_ts = entry_ts + offset
            if target_ts > now_ts:
                continue   # horizon not elapsed yet

            if getattr(outcome, f"price_{label}") is not None:
                continue   # already filled

            price = tick_archive.get_price_at(tick.ticker, target_ts)
            if not price:
                continue

            ret = (price - entry_price) / entry_price
            if direction == "SHORT":
                ret = -ret
            correct = ret > 0

            updates[f"price_{label}"] = price
            updates[f"return_{label}_pct"] = round(ret * 100, 4)
            updates[f"correct_{label}"] = correct
            updates[f"ts_filled_{label}"] = datetime.utcnow()

        if updates:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(SignalOutcome).where(SignalOutcome.id == outcome.id)
                )
                row = result.scalar_one_or_none()
                if row:
                    for k, v in updates.items():
                        setattr(row, k, v)
                    await session.commit()

    print("Done.")


if __name__ == "__main__":
    asyncio.run(fill_outcomes())
