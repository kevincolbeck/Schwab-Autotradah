"""
DuckDB tick archive — stores raw equity trade ticks at full resolution.

SQLite is unsuitable for this volume (Mag7 + SPY + QQQ can produce millions of
ticks per day). DuckDB is columnar and handles analytical queries (min/max price
in a window, cumulative volume, replaying tick-by-tick) orders of magnitude faster.

Schema:
  tick_archive (ts DOUBLE, ticker VARCHAR, price DOUBLE, size INTEGER, side VARCHAR)

  side: 'ASK' (buyer-initiated) | 'BID' (seller-initiated) — from Lee-Ready rule
"""

import duckdb
import logging
import os
from datetime import datetime

from backend.config import settings

logger = logging.getLogger(__name__)

_conn: duckdb.DuckDBPyConnection = None


def _get_conn() -> duckdb.DuckDBPyConnection:
    global _conn
    if _conn is None:
        _conn = duckdb.connect(settings.duckdb_path)
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS tick_archive (
                ts      DOUBLE NOT NULL,
                ticker  VARCHAR NOT NULL,
                price   DOUBLE NOT NULL,
                size    INTEGER NOT NULL,
                side    VARCHAR NOT NULL,
                dt      TIMESTAMP GENERATED ALWAYS AS (
                    epoch_ms(CAST(ts * 1000 AS BIGINT))
                ) VIRTUAL
            )
        """)
        _conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tick_ticker_ts
            ON tick_archive (ticker, ts)
        """)
        logger.info(f"DuckDB tick archive opened: {settings.duckdb_path}")
    return _conn


class TickArchive:
    """Batched writer for equity ticks. Flushes every N ticks or T seconds."""

    BATCH_SIZE = 500

    def __init__(self):
        self._buffer: list[tuple] = []

    def write_tick(self, ticker: str, price: float, size: int, side: str, ts: float) -> None:
        self._buffer.append((ts, ticker, price, size, side))
        if len(self._buffer) >= self.BATCH_SIZE:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        try:
            conn = _get_conn()
            conn.executemany(
                "INSERT INTO tick_archive (ts, ticker, price, size, side) VALUES (?, ?, ?, ?, ?)",
                self._buffer,
            )
            self._buffer.clear()
        except Exception as e:
            logger.error(f"Tick archive flush error: {e}")

    # ── Analytical queries (used by walk-forward analysis scripts) ────────────

    def get_ticks(self, ticker: str, from_ts: float, to_ts: float) -> list[dict]:
        """Retrieve all ticks for a ticker in a time window."""
        conn = _get_conn()
        rows = conn.execute(
            """
            SELECT ts, price, size, side
            FROM tick_archive
            WHERE ticker = ? AND ts >= ? AND ts <= ?
            ORDER BY ts
            """,
            [ticker, from_ts, to_ts],
        ).fetchall()
        return [{"ts": r[0], "price": r[1], "size": r[2], "side": r[3]} for r in rows]

    def get_cumulative_delta(self, ticker: str, from_ts: float, to_ts: float) -> float:
        """Net buy minus sell volume in window."""
        conn = _get_conn()
        row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN side = 'ASK' THEN size ELSE -size END) AS net_delta
            FROM tick_archive
            WHERE ticker = ? AND ts >= ? AND ts <= ?
            """,
            [ticker, from_ts, to_ts],
        ).fetchone()
        return float(row[0] or 0)

    def get_price_at(self, ticker: str, ts: float, tolerance_seconds: float = 5.0) -> float:
        """Last trade price at or just before a given timestamp."""
        conn = _get_conn()
        row = conn.execute(
            """
            SELECT price FROM tick_archive
            WHERE ticker = ? AND ts <= ? AND ts >= ?
            ORDER BY ts DESC LIMIT 1
            """,
            [ticker, ts, ts - tolerance_seconds],
        ).fetchone()
        return float(row[0]) if row else 0.0

    def vacuum_old(self, days_to_keep: int = 90) -> None:
        """Remove ticks older than N days to manage disk usage."""
        import time as _time
        cutoff = _time.time() - (days_to_keep * 86400)
        conn = _get_conn()
        conn.execute("DELETE FROM tick_archive WHERE ts < ?", [cutoff])
        logger.info(f"Tick archive: vacuumed ticks older than {days_to_keep} days")


# Singleton
tick_archive = TickArchive()
