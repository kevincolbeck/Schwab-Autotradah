"""
DuckDB tick archive — stores raw equity trade ticks at full resolution,
plus derived analytics data needed for walk-forward signal replay.

Tables:
  tick_archive          — every equity trade tick (price, size, Lee-Ready side)
  footprint_candles     — every closed 1-minute footprint candle (delta, vol, absorption)
  l2_snapshots          — L2 book state sampled at each signal eval cycle
  options_flow_events   — every detected sweep or large print event
  quote_snapshots       — bid/ask/last sampled every ~15s per ticker

SQLite is unsuitable for this volume. DuckDB handles analytical queries
(min/max in window, cumulative delta, replay) orders of magnitude faster.
"""

import duckdb
import logging
import time as _time
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
                side    VARCHAR NOT NULL
            )
        """)
        _conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tick_ticker_ts
            ON tick_archive (ticker, ts)
        """)

        _conn.execute("""
            CREATE TABLE IF NOT EXISTS footprint_candles (
                ts_open         DOUBLE NOT NULL,
                ts_close        DOUBLE NOT NULL,
                ticker          VARCHAR NOT NULL,
                open_price      DOUBLE,
                high_price      DOUBLE,
                low_price       DOUBLE,
                close_price     DOUBLE,
                total_volume    BIGINT,
                ask_volume      BIGINT,
                bid_volume      BIGINT,
                delta           DOUBLE,
                cumulative_delta DOUBLE,
                imbalance       DOUBLE,
                absorption_side VARCHAR
            )
        """)
        _conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_fp_ticker_ts
            ON footprint_candles (ticker, ts_open)
        """)

        _conn.execute("""
            CREATE TABLE IF NOT EXISTS l2_snapshots (
                ts              DOUBLE NOT NULL,
                ticker          VARCHAR NOT NULL,
                best_bid        DOUBLE,
                best_ask        DOUBLE,
                mid_price       DOUBLE,
                imbalance       DOUBLE,
                bid_wall_price  DOUBLE,
                bid_wall_size   DOUBLE,
                ask_wall_price  DOUBLE,
                ask_wall_size   DOUBLE
            )
        """)
        _conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_l2_ticker_ts
            ON l2_snapshots (ticker, ts)
        """)

        _conn.execute("""
            CREATE TABLE IF NOT EXISTS options_flow_events (
                ts              DOUBLE NOT NULL,
                ticker          VARCHAR NOT NULL,
                event_type      VARCHAR NOT NULL,
                direction       VARCHAR NOT NULL,
                option_symbol   VARCHAR,
                strike          DOUBLE,
                price           DOUBLE,
                size            INTEGER,
                notional        DOUBLE,
                vol_oi_ratio    DOUBLE,
                opt_side        VARCHAR
            )
        """)
        _conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_flow_ticker_ts
            ON options_flow_events (ticker, ts)
        """)

        _conn.execute("""
            CREATE TABLE IF NOT EXISTS quote_snapshots (
                ts          DOUBLE NOT NULL,
                ticker      VARCHAR NOT NULL,
                bid         DOUBLE,
                ask         DOUBLE,
                last        DOUBLE,
                volume      BIGINT,
                vwap        DOUBLE,
                rvol        DOUBLE
            )
        """)
        _conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_quote_ticker_ts
            ON quote_snapshots (ticker, ts)
        """)

        logger.info(f"DuckDB archive opened: {settings.duckdb_path}")
    return _conn


class TickArchive:
    """Batched writer for all DuckDB tables. Flushes every N ticks or on demand."""

    BATCH_SIZE = 500

    def __init__(self):
        self._tick_buf: list[tuple] = []
        self._candle_buf: list[tuple] = []
        self._l2_buf: list[tuple] = []
        self._flow_buf: list[tuple] = []
        self._quote_buf: list[tuple] = []

    # ── Tick archive ─────────────────────────────────────────────────────────

    def write_tick(self, ticker: str, price: float, size: int, side: str, ts: float) -> None:
        self._tick_buf.append((ts, ticker, price, size, side))
        if len(self._tick_buf) >= self.BATCH_SIZE:
            self._flush_ticks()

    def _flush_ticks(self) -> None:
        if not self._tick_buf:
            return
        try:
            conn = _get_conn()
            conn.executemany(
                "INSERT INTO tick_archive (ts, ticker, price, size, side) VALUES (?, ?, ?, ?, ?)",
                self._tick_buf,
            )
            self._tick_buf.clear()
        except Exception as e:
            logger.error(f"Tick archive flush error: {e}")

    # ── Footprint candles ─────────────────────────────────────────────────────

    def write_footprint_candle(self, candle) -> None:
        """Write a closed FootprintCandle to the archive."""
        ask_vol = sum(candle.ask_vol.values())
        bid_vol = sum(candle.bid_vol.values())
        total = ask_vol + bid_vol
        imbalance = ask_vol / total if total > 0 else 0.5
        self._candle_buf.append((
            candle.open_ts, candle.close_ts, candle.ticker,
            candle.open_price, candle.high_price, candle.low_price, candle.close_price,
            candle.total_volume, int(ask_vol), int(bid_vol),
            candle.delta, candle.cumulative_delta, imbalance,
            None,  # absorption_side filled separately if needed
        ))
        if len(self._candle_buf) >= 50:
            self._flush_candles()

    def _flush_candles(self) -> None:
        if not self._candle_buf:
            return
        try:
            conn = _get_conn()
            conn.executemany(
                """INSERT INTO footprint_candles
                   (ts_open, ts_close, ticker, open_price, high_price, low_price, close_price,
                    total_volume, ask_volume, bid_volume, delta, cumulative_delta, imbalance, absorption_side)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                self._candle_buf,
            )
            self._candle_buf.clear()
        except Exception as e:
            logger.error(f"Candle archive flush error: {e}")

    # ── L2 snapshots ──────────────────────────────────────────────────────────

    def write_l2_snapshot(self, ticker: str, state) -> None:
        """Write an OrderBookMonitor BookState to the archive."""
        bids = state.bid_wall_price and state.bid_wall_size
        asks = state.ask_wall_price and state.ask_wall_size
        self._l2_buf.append((
            _time.time(), ticker,
            None, None,        # best_bid / best_ask — use mid as proxy
            state.mid_price,
            state.imbalance,
            state.bid_wall_price if bids else None,
            state.bid_wall_size  if bids else None,
            state.ask_wall_price if asks else None,
            state.ask_wall_size  if asks else None,
        ))
        if len(self._l2_buf) >= 100:
            self._flush_l2()

    def _flush_l2(self) -> None:
        if not self._l2_buf:
            return
        try:
            conn = _get_conn()
            conn.executemany(
                """INSERT INTO l2_snapshots
                   (ts, ticker, best_bid, best_ask, mid_price, imbalance,
                    bid_wall_price, bid_wall_size, ask_wall_price, ask_wall_size)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                self._l2_buf,
            )
            self._l2_buf.clear()
        except Exception as e:
            logger.error(f"L2 snapshot flush error: {e}")

    # ── Options flow events ───────────────────────────────────────────────────

    def write_options_flow_event(
        self, ticker: str, event_type: str, direction: str,
        option_symbol: str, strike: float, price: float,
        size: int, notional: float, vol_oi_ratio: float, opt_side: str,
        ts: float = None,
    ) -> None:
        self._flow_buf.append((
            ts or _time.time(), ticker, event_type, direction,
            option_symbol, strike, price, size, notional, vol_oi_ratio, opt_side,
        ))
        if len(self._flow_buf) >= 20:
            self._flush_flow()

    def _flush_flow(self) -> None:
        if not self._flow_buf:
            return
        try:
            conn = _get_conn()
            conn.executemany(
                """INSERT INTO options_flow_events
                   (ts, ticker, event_type, direction, option_symbol, strike,
                    price, size, notional, vol_oi_ratio, opt_side)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                self._flow_buf,
            )
            self._flow_buf.clear()
        except Exception as e:
            logger.error(f"Flow event flush error: {e}")

    # ── Quote snapshots ───────────────────────────────────────────────────────

    def write_quote_snapshot(
        self, ticker: str, bid: float, ask: float, last: float,
        volume: int, vwap: float = None, rvol: float = None,
    ) -> None:
        self._quote_buf.append((
            _time.time(), ticker, bid, ask, last, volume, vwap, rvol,
        ))
        if len(self._quote_buf) >= 200:
            self._flush_quotes()

    def _flush_quotes(self) -> None:
        if not self._quote_buf:
            return
        try:
            conn = _get_conn()
            conn.executemany(
                """INSERT INTO quote_snapshots
                   (ts, ticker, bid, ask, last, volume, vwap, rvol)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                self._quote_buf,
            )
            self._quote_buf.clear()
        except Exception as e:
            logger.error(f"Quote snapshot flush error: {e}")

    # ── Master flush ──────────────────────────────────────────────────────────

    def flush(self) -> None:
        """Flush all buffers — called from position_check_loop every 5s."""
        self._flush_ticks()
        self._flush_candles()
        self._flush_l2()
        self._flush_flow()
        self._flush_quotes()

    # ── Analytical queries ────────────────────────────────────────────────────

    def get_ticks(self, ticker: str, from_ts: float, to_ts: float) -> list[dict]:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT ts, price, size, side FROM tick_archive "
            "WHERE ticker = ? AND ts >= ? AND ts <= ? ORDER BY ts",
            [ticker, from_ts, to_ts],
        ).fetchall()
        return [{"ts": r[0], "price": r[1], "size": r[2], "side": r[3]} for r in rows]

    def get_cumulative_delta(self, ticker: str, from_ts: float, to_ts: float) -> float:
        conn = _get_conn()
        row = conn.execute(
            "SELECT SUM(CASE WHEN side = 'ASK' THEN size ELSE -size END) "
            "FROM tick_archive WHERE ticker = ? AND ts >= ? AND ts <= ?",
            [ticker, from_ts, to_ts],
        ).fetchone()
        return float(row[0] or 0)

    def get_price_at(self, ticker: str, ts: float, tolerance_seconds: float = 5.0) -> float:
        conn = _get_conn()
        row = conn.execute(
            "SELECT price FROM tick_archive WHERE ticker = ? AND ts <= ? AND ts >= ? "
            "ORDER BY ts DESC LIMIT 1",
            [ticker, ts, ts - tolerance_seconds],
        ).fetchone()
        return float(row[0]) if row else 0.0

    def get_footprint_candles(
        self, ticker: str, from_ts: float, to_ts: float
    ) -> list[dict]:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT ts_open, ts_close, open_price, high_price, low_price, close_price, "
            "total_volume, ask_volume, bid_volume, delta, cumulative_delta, imbalance "
            "FROM footprint_candles WHERE ticker = ? AND ts_open >= ? AND ts_open <= ? "
            "ORDER BY ts_open",
            [ticker, from_ts, to_ts],
        ).fetchall()
        keys = ["ts_open", "ts_close", "open", "high", "low", "close",
                "total_vol", "ask_vol", "bid_vol", "delta", "cum_delta", "imbalance"]
        return [dict(zip(keys, r)) for r in rows]

    def get_l2_snapshots(
        self, ticker: str, from_ts: float, to_ts: float
    ) -> list[dict]:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT ts, mid_price, imbalance, bid_wall_price, bid_wall_size, "
            "ask_wall_price, ask_wall_size FROM l2_snapshots "
            "WHERE ticker = ? AND ts >= ? AND ts <= ? ORDER BY ts",
            [ticker, from_ts, to_ts],
        ).fetchall()
        keys = ["ts", "mid", "imbalance", "bid_wall_p", "bid_wall_s", "ask_wall_p", "ask_wall_s"]
        return [dict(zip(keys, r)) for r in rows]

    def vacuum_old(self, days_to_keep: int = 90) -> None:
        cutoff = _time.time() - (days_to_keep * 86400)
        conn = _get_conn()
        for table in ("tick_archive", "footprint_candles", "l2_snapshots",
                      "options_flow_events", "quote_snapshots"):
            conn.execute(f"DELETE FROM {table} WHERE ts < ?", [cutoff])
        logger.info(f"DuckDB archive: vacuumed data older than {days_to_keep} days")


# Singleton
tick_archive = TickArchive()
