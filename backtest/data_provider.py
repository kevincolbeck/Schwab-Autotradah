"""Fetch and cache OHLCV data for backtesting across multiple timeframes."""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

CACHE_DB_PATH = "backtest_data.db"
BATCH_DOWNLOAD_SIZE = 100
INTRADAY_DEFAULT_INTERVAL = "5m"
POLYGON_BASE_URL = "https://api.polygon.io"
EASTERN_TZ = ZoneInfo("America/New_York")

SUPPORTED_INTRADAY_INTERVALS = {"1m", "2m", "5m", "15m", "30m", "60m", "90m"}
SUPPORTED_HIGHER_TIMEFRAMES = {"1wk", "1mo"}
SUPPORTED_TIMEFRAMES = {"1d", *SUPPORTED_INTRADAY_INTERVALS, *SUPPORTED_HIGHER_TIMEFRAMES}
DEFAULT_IMPORT_TIMEFRAMES = ("1d", "1wk", "1mo", INTRADAY_DEFAULT_INTERVAL)


class HistoricalDataProvider:
    """Fetches and caches daily/intraday/weekly/monthly OHLCV bars in SQLite."""

    def __init__(
        self,
        cache_db_path: str = CACHE_DB_PATH,
        data_source: str = "auto",
        polygon_api_key: Optional[str] = None,
        default_history_years: int = 20,
    ):
        self.cache_db_path = cache_db_path
        self.data_source = (data_source or "auto").strip().lower()
        if self.data_source not in {"auto", "yfinance", "polygon"}:
            logger.warning("Unknown data source '%s', falling back to auto", self.data_source)
            self.data_source = "auto"
        self.polygon_api_key = (polygon_api_key or os.getenv("POLYGON_API_KEY", "")).strip()
        self.default_history_years = max(int(default_history_years or 1), 1)
        self._conn: Optional[sqlite3.Connection] = None
        self._http: Optional[httpx.Client] = None
        if self.polygon_api_key:
            self._http = httpx.Client(
                timeout=httpx.Timeout(timeout=40.0, connect=10.0),
                headers={"User-Agent": "schwab-momentum-bot/backtest-data"},
            )
        self._ensure_schema()

    def _ensure_schema(self):
        conn = self._get_conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_bars (
                symbol TEXT NOT NULL,
                date TEXT NOT NULL,
                open REAL, high REAL, low REAL, close REAL, volume REAL,
                PRIMARY KEY (symbol, date)
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fetch_log (
                symbol TEXT PRIMARY KEY,
                last_fetched TEXT,
                start_date TEXT,
                end_date TEXT,
                row_count INTEGER
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS intraday_bars (
                symbol TEXT NOT NULL,
                interval TEXT NOT NULL,
                datetime TEXT NOT NULL,
                open REAL, high REAL, low REAL, close REAL, volume REAL,
                PRIMARY KEY (symbol, interval, datetime)
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fetch_log_intraday (
                symbol TEXT NOT NULL,
                interval TEXT NOT NULL,
                last_fetched TEXT,
                start_date TEXT,
                end_date TEXT,
                row_count INTEGER,
                PRIMARY KEY (symbol, interval)
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS timeframe_bars (
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                datetime TEXT NOT NULL,
                open REAL, high REAL, low REAL, close REAL, volume REAL,
                PRIMARY KEY (symbol, timeframe, datetime)
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fetch_log_timeframe (
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                last_fetched TEXT,
                start_date TEXT,
                end_date TEXT,
                row_count INTEGER,
                source TEXT,
                PRIMARY KEY (symbol, timeframe)
            )
        """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_timeframe_bars_lookup "
            "ON timeframe_bars(symbol, timeframe, datetime)"
        )
        conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.cache_db_path)
        return self._conn

    def fetch_symbol(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """Fetch daily bars for a symbol. Uses cache unless force_refresh."""
        if start_date is None:
            start_date = _default_start_date(years_back=self.default_history_years)
        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")

        if not force_refresh and self._is_cached(symbol, start_date, end_date):
            return self._load_from_cache(symbol, start_date, end_date)

        logger.info("Fetching daily %s: %s to %s", symbol, start_date, end_date)
        df = self._download_daily_symbol(symbol, start_date, end_date)
        if df.empty:
            logger.warning("No daily data returned for %s", symbol)
            return pd.DataFrame()

        self._store_to_cache(symbol, df)
        return df

    def fetch_universe(
        self,
        symbols: List[str],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        force_refresh: bool = False,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> Dict[str, pd.DataFrame]:
        """Fetch daily data for all symbols. Returns {symbol: DataFrame}."""
        if start_date is None:
            start_date = _default_start_date(years_back=self.default_history_years)
        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")

        unique_symbols = [s for s in dict.fromkeys(symbols) if s]
        results: Dict[str, pd.DataFrame] = {}
        total = len(unique_symbols)

        if self._should_use_polygon("1d", start_date, end_date):
            completed = 0
            for symbol in unique_symbols:
                try:
                    df = self.fetch_symbol(
                        symbol,
                        start_date=start_date,
                        end_date=end_date,
                        force_refresh=force_refresh,
                    )
                    if not df.empty:
                        results[symbol] = df
                except Exception as exc:
                    logger.error("Error fetching daily %s: %s", symbol, exc)
                completed += 1
                if progress_callback:
                    progress_callback(completed, total, symbol)
            return results

        cached_symbols: List[str] = []
        uncached_symbols: List[str] = []
        if force_refresh:
            uncached_symbols = unique_symbols
        else:
            cached_set = self._batch_check_cached(unique_symbols, start_date, end_date)
            for symbol in unique_symbols:
                if symbol in cached_set:
                    cached_symbols.append(symbol)
                else:
                    uncached_symbols.append(symbol)

        if cached_symbols:
            batch_loaded = self._batch_load_from_cache(cached_symbols, start_date, end_date)
            results.update(batch_loaded)
            if progress_callback:
                progress_callback(len(cached_symbols), total, f"{len(batch_loaded)} cached")

        completed = len(cached_symbols)
        for batch in _chunked(uncached_symbols, BATCH_DOWNLOAD_SIZE):
            downloaded = self._download_batch(batch, start_date, end_date, interval="1d")
            for symbol in batch:
                df = downloaded.get(symbol, pd.DataFrame())
                if not df.empty:
                    try:
                        self._store_to_cache(symbol, df)
                        results[symbol] = df
                    except Exception as exc:
                        logger.error("Error storing %s to cache: %s", symbol, exc)
                else:
                    logger.warning("No daily data returned for %s", symbol)
                completed += 1
                if progress_callback:
                    progress_callback(completed, total, symbol)

        return results

    def fetch_intraday_symbol(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        interval: str = INTRADAY_DEFAULT_INTERVAL,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """Fetch intraday bars for one symbol."""
        interval = _normalize_timeframe(interval)
        if interval not in SUPPORTED_INTRADAY_INTERVALS:
            raise ValueError(f"Unsupported intraday interval '{interval}'")
        if start_date is None:
            start_date = _default_start_date(days_back=60)
        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")

        if not self._should_use_polygon(interval, start_date, end_date):
            start_ts = pd.Timestamp(start_date).normalize()
            end_ts = pd.Timestamp(end_date).normalize()
            days_requested = int((end_ts - start_ts).days) + 1
            max_days = _yfinance_intraday_max_days(interval)
            if days_requested > max_days:
                logger.warning(
                    "Skipping intraday fetch for %s [%s]: requested %d days (%s..%s) "
                    "exceeds yfinance retention of %d days. Configure Polygon for deep intraday history.",
                    symbol,
                    interval,
                    days_requested,
                    start_date,
                    end_date,
                    max_days,
                )
                return pd.DataFrame()

        if not force_refresh and self._is_intraday_cached(symbol, interval, start_date, end_date):
            return self._load_intraday_from_cache(symbol, interval, start_date, end_date)

        df = self._download_intraday_symbol(symbol, start_date, end_date, interval)
        if df.empty:
            logger.warning(
                "No intraday data returned for %s interval=%s (%s to %s)",
                symbol,
                interval,
                start_date,
                end_date,
            )
            return pd.DataFrame()

        self._store_intraday_to_cache(symbol, interval, df)
        return df

    def fetch_intraday_universe(
        self,
        symbols: List[str],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        interval: str = INTRADAY_DEFAULT_INTERVAL,
        force_refresh: bool = False,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> Dict[str, pd.DataFrame]:
        """Fetch intraday data for all symbols. Returns {symbol: DataFrame}."""
        interval = _normalize_timeframe(interval)
        if start_date is None:
            start_date = _default_start_date(days_back=60)
        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")

        unique_symbols = [s for s in dict.fromkeys(symbols) if s]
        results: Dict[str, pd.DataFrame] = {}
        total = len(unique_symbols)
        completed = 0

        for symbol in unique_symbols:
            try:
                df = self.fetch_intraday_symbol(
                    symbol=symbol,
                    start_date=start_date,
                    end_date=end_date,
                    interval=interval,
                    force_refresh=force_refresh,
                )
                if not df.empty:
                    results[symbol] = df
            except Exception as exc:
                logger.error("Error fetching intraday %s [%s]: %s", symbol, interval, exc)
            completed += 1
            if progress_callback:
                progress_callback(completed, total, f"{symbol} ({interval})")
        return results

    def fetch_timeframe_symbol(
        self,
        symbol: str,
        timeframe: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """Fetch bars for any supported timeframe."""
        timeframe = _normalize_timeframe(timeframe)
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe '{timeframe}'")

        if timeframe == "1d":
            return self.fetch_symbol(
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                force_refresh=force_refresh,
            )
        if timeframe in SUPPORTED_INTRADAY_INTERVALS:
            return self.fetch_intraday_symbol(
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                interval=timeframe,
                force_refresh=force_refresh,
            )

        if start_date is None:
            start_date = _default_start_date(years_back=self.default_history_years)
        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")

        if not force_refresh and self._is_timeframe_cached(symbol, timeframe, start_date, end_date):
            return self._load_timeframe_from_cache(symbol, timeframe, start_date, end_date)

        df = self._download_timeframe_symbol(symbol, start_date, end_date, timeframe)
        if df.empty:
            logger.warning(
                "No timeframe data returned for %s [%s] (%s to %s)",
                symbol,
                timeframe,
                start_date,
                end_date,
            )
            return pd.DataFrame()

        self._store_timeframe_to_cache(symbol, timeframe, df)
        return df

    def fetch_timeframe_universe(
        self,
        symbols: Sequence[str],
        timeframe: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        force_refresh: bool = False,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> Dict[str, pd.DataFrame]:
        """Fetch bars for all symbols at one timeframe."""
        timeframe = _normalize_timeframe(timeframe)
        unique_symbols = [s for s in dict.fromkeys(symbols) if s]
        total = len(unique_symbols)
        completed = 0
        out: Dict[str, pd.DataFrame] = {}
        for symbol in unique_symbols:
            try:
                df = self.fetch_timeframe_symbol(
                    symbol=symbol,
                    timeframe=timeframe,
                    start_date=start_date,
                    end_date=end_date,
                    force_refresh=force_refresh,
                )
                if not df.empty:
                    out[symbol] = df
            except Exception as exc:
                logger.error("Error fetching %s [%s]: %s", symbol, timeframe, exc)
            completed += 1
            if progress_callback:
                progress_callback(completed, total, f"{symbol} ({timeframe})")
        return out

    def import_market_data(
        self,
        symbols: Sequence[str],
        start_date: str,
        end_date: str,
        timeframes: Sequence[str] = DEFAULT_IMPORT_TIMEFRAMES,
        force_refresh: bool = False,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> Dict[str, object]:
        """Bulk-import data for many symbols and timeframes into local cache."""
        normalized_timeframes = [_normalize_timeframe(tf) for tf in timeframes]
        normalized_timeframes = [tf for tf in dict.fromkeys(normalized_timeframes) if tf]
        invalid = [tf for tf in normalized_timeframes if tf not in SUPPORTED_TIMEFRAMES]
        if invalid:
            raise ValueError(f"Unsupported timeframe(s): {', '.join(invalid)}")

        unique_symbols = [s.strip().upper() for s in dict.fromkeys(symbols) if str(s).strip()]
        total_tasks = len(unique_symbols) * len(normalized_timeframes)
        completed = 0
        imported_counts: Dict[str, int] = {tf: 0 for tf in normalized_timeframes}
        failures: List[str] = []

        for symbol in unique_symbols:
            for timeframe in normalized_timeframes:
                label = f"{symbol} [{timeframe}]"
                try:
                    df = self.fetch_timeframe_symbol(
                        symbol=symbol,
                        timeframe=timeframe,
                        start_date=start_date,
                        end_date=end_date,
                        force_refresh=force_refresh,
                    )
                    if not df.empty:
                        imported_counts[timeframe] += 1
                    else:
                        failures.append(label)
                except Exception as exc:
                    logger.error("Import failed for %s: %s", label, exc)
                    failures.append(label)
                completed += 1
                if progress_callback:
                    progress_callback(completed, total_tasks, label)

        return {
            "symbols_requested": len(unique_symbols),
            "timeframes": normalized_timeframes,
            "imported_symbols_per_timeframe": imported_counts,
            "failed_tasks": failures,
        }

    def _download_daily_symbol(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        if self._should_use_polygon("1d", start_date, end_date):
            return self._download_polygon_aggregates(symbol, start_date, end_date, "1d")
        batch_data = self._download_batch([symbol], start_date, end_date, interval="1d")
        return batch_data.get(symbol, pd.DataFrame())

    def _download_timeframe_symbol(
        self, symbol: str, start_date: str, end_date: str, timeframe: str
    ) -> pd.DataFrame:
        if self._should_use_polygon(timeframe, start_date, end_date):
            return self._download_polygon_aggregates(symbol, start_date, end_date, timeframe)
        batch_data = self._download_batch([symbol], start_date, end_date, interval=timeframe)
        return batch_data.get(symbol, pd.DataFrame())

    def _download_batch(
        self,
        symbols: List[str],
        start_date: str,
        end_date: str,
        interval: str = "1d",
    ) -> Dict[str, pd.DataFrame]:
        if not symbols:
            return {}

        try:
            raw = yf.download(
                tickers=symbols,
                start=start_date,
                end=end_date,
                interval=interval,
                auto_adjust=True,
                progress=False,
                group_by="ticker",
                threads=True,
                prepost=interval in SUPPORTED_INTRADAY_INTERVALS,
            )
        except Exception as exc:
            logger.error(
                "Batch yfinance download failed for %d symbols interval=%s: %s",
                len(symbols),
                interval,
                exc,
            )
            return {}

        if raw is None or raw.empty:
            return {}

        out: Dict[str, pd.DataFrame] = {}
        if isinstance(raw.columns, pd.MultiIndex):
            available = set(raw.columns.get_level_values(0))
            for symbol in symbols:
                if symbol not in available:
                    continue
                parsed = self._normalize_download_df(raw[symbol])
                if not parsed.empty:
                    out[symbol] = parsed
        else:
            symbol = symbols[0]
            parsed = self._normalize_download_df(raw)
            if not parsed.empty:
                out[symbol] = parsed
        return out

    def _normalize_download_df(self, df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            col0 = set(df.columns.get_level_values(0))
            col1 = set(df.columns.get_level_values(1))
            if {"Open", "High", "Low", "Close", "Volume"}.issubset(col0):
                df = df.droplevel(1, axis=1)
            elif {"Open", "High", "Low", "Close", "Volume"}.issubset(col1):
                df = df.droplevel(0, axis=1)
            elif len(col0) == 1:
                first = next(iter(col0))
                df = df[first]
            else:
                return pd.DataFrame()
        if "Close" not in df.columns:
            return pd.DataFrame()

        out = df.copy().dropna(subset=["Close"])
        if out.empty:
            return pd.DataFrame()

        out = out.reset_index()
        datetime_col = "Date"
        if "Datetime" in out.columns:
            datetime_col = "Datetime"
        elif "Date" not in out.columns:
            datetime_col = out.columns[0]

        out = out.rename(
            columns={
                datetime_col: "datetime",
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            }
        )
        out["datetime"] = pd.to_datetime(out["datetime"]).dt.tz_localize(None)
        for required_col in ["open", "high", "low", "close", "volume"]:
            if required_col not in out.columns:
                out[required_col] = 0.0
        out = out[["datetime", "open", "high", "low", "close", "volume"]]
        return out.drop_duplicates(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)

    def _download_intraday_symbol(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        interval: str,
    ) -> pd.DataFrame:
        if self._should_use_polygon(interval, start_date, end_date):
            return self._download_polygon_aggregates(symbol, start_date, end_date, interval)

        chunks = _build_date_chunks(start_date, end_date, _intraday_chunk_days(interval))
        frames: List[pd.DataFrame] = []

        for chunk_start, chunk_end in chunks:
            chunk_start_s = chunk_start.strftime("%Y-%m-%d")
            chunk_end_s = (chunk_end + timedelta(days=1)).strftime("%Y-%m-%d")
            try:
                raw = yf.download(
                    tickers=symbol,
                    start=chunk_start_s,
                    end=chunk_end_s,
                    interval=interval,
                    auto_adjust=True,
                    progress=False,
                    threads=False,
                    prepost=True,
                )
            except Exception as exc:
                logger.warning(
                    "Intraday yfinance fetch failed for %s [%s] chunk %s..%s: %s",
                    symbol,
                    interval,
                    chunk_start_s,
                    chunk_end.strftime("%Y-%m-%d"),
                    exc,
                )
                continue

            parsed = self._normalize_download_df(raw)
            if not parsed.empty:
                frames.append(parsed)

        if not frames:
            return pd.DataFrame()

        out = pd.concat(frames, ignore_index=True)
        out = out.drop_duplicates(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
        return out

    def _download_polygon_aggregates(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        timeframe: str,
    ) -> pd.DataFrame:
        if not self.polygon_api_key:
            logger.warning("Polygon API key missing; cannot fetch %s [%s] from polygon", symbol, timeframe)
            return pd.DataFrame()

        multiplier, timespan = _polygon_params_for_timeframe(timeframe)
        chunk_days = _polygon_chunk_days(timeframe)
        chunks = _build_date_chunks(start_date, end_date, chunk_days)
        frames: List[pd.DataFrame] = []

        for chunk_start, chunk_end in chunks:
            frame = self._download_polygon_chunk(
                symbol=symbol,
                multiplier=multiplier,
                timespan=timespan,
                start_date=chunk_start.strftime("%Y-%m-%d"),
                end_date=chunk_end.strftime("%Y-%m-%d"),
            )
            if not frame.empty:
                frames.append(frame)

        if not frames:
            return pd.DataFrame()

        out = pd.concat(frames, ignore_index=True)
        out = out.drop_duplicates(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
        return out

    def _download_polygon_chunk(
        self,
        symbol: str,
        multiplier: int,
        timespan: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        url = (
            f"{POLYGON_BASE_URL}/v2/aggs/ticker/{symbol.upper()}"
            f"/range/{multiplier}/{timespan}/{start_date}/{end_date}"
        )
        params: Optional[dict] = {
            "adjusted": "true",
            "sort": "asc",
            "limit": 50000,
            "apiKey": self.polygon_api_key,
        }
        seen_urls: set[str] = set()
        frames: List[pd.DataFrame] = []

        while url and url not in seen_urls:
            seen_urls.add(url)
            payload = self._polygon_get(url, params=params)
            params = None
            if payload is None:
                break

            results = payload.get("results") or []
            if results:
                parsed = self._normalize_polygon_results(results)
                if not parsed.empty:
                    frames.append(parsed)

            next_url = payload.get("next_url")
            if not next_url:
                break
            if "apiKey=" not in next_url:
                sep = "&" if "?" in next_url else "?"
                next_url = f"{next_url}{sep}apiKey={self.polygon_api_key}"
            url = next_url

        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True)
        return out.drop_duplicates(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)

    def _polygon_get(self, url: str, params: Optional[dict] = None) -> Optional[dict]:
        if self._http is None:
            return None

        backoff = 1.0
        for _ in range(5):
            try:
                resp = self._http.get(url, params=params)
            except Exception as exc:
                logger.warning("Polygon request failed (%s): %s", url, exc)
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 8.0)
                continue

            if resp.status_code == 200:
                try:
                    return resp.json()
                except Exception as exc:
                    logger.warning("Polygon returned non-JSON response for %s: %s", url, exc)
                    return None

            if resp.status_code in {429, 500, 502, 503, 504}:
                logger.warning("Polygon transient error %s for %s; retrying", resp.status_code, url)
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 8.0)
                continue

            logger.warning("Polygon request failed %s for %s: %s", resp.status_code, url, resp.text[:200])
            return None

        logger.warning("Polygon request exhausted retries for %s", url)
        return None

    def _normalize_polygon_results(self, results: Sequence[dict]) -> pd.DataFrame:
        if not results:
            return pd.DataFrame()
        df = pd.DataFrame(results)
        if "t" not in df.columns or "c" not in df.columns:
            return pd.DataFrame()

        out = df.rename(
            columns={
                "t": "datetime",
                "o": "open",
                "h": "high",
                "l": "low",
                "c": "close",
                "v": "volume",
            }
        )
        out["datetime"] = pd.to_datetime(out["datetime"], unit="ms", utc=True).dt.tz_convert(EASTERN_TZ).dt.tz_localize(None)
        for required_col in ["open", "high", "low", "close", "volume"]:
            if required_col not in out.columns:
                out[required_col] = 0.0
        out = out[["datetime", "open", "high", "low", "close", "volume"]]
        out = out.dropna(subset=["close"])
        return out.sort_values("datetime").reset_index(drop=True)

    def _batch_check_cached(self, symbols: List[str], start_date: str, end_date: str) -> set:
        if not symbols:
            return set()
        conn = self._get_conn()
        today = datetime.now().strftime("%Y-%m-%d")
        now = datetime.now()

        placeholders = ",".join("?" for _ in symbols)
        rows = conn.execute(
            f"SELECT symbol, start_date, end_date, last_fetched "
            f"FROM fetch_log WHERE symbol IN ({placeholders})",
            symbols,
        ).fetchall()

        cached = set()
        for symbol, cached_start, cached_end, last_fetched in rows:
            last_dt = datetime.fromisoformat(last_fetched)
            if cached_start <= start_date and cached_end >= end_date:
                if end_date < today or (now - last_dt).total_seconds() < 86400:
                    cached.add(symbol)
        return cached

    def _batch_load_from_cache(
        self, symbols: List[str], start_date: str, end_date: str
    ) -> Dict[str, pd.DataFrame]:
        if not symbols:
            return {}
        conn = self._get_conn()
        placeholders = ",".join("?" for _ in symbols)
        df = pd.read_sql_query(
            f"SELECT symbol, date as datetime, open, high, low, close, volume "
            f"FROM daily_bars WHERE symbol IN ({placeholders}) "
            f"AND date >= ? AND date <= ? ORDER BY symbol, date",
            conn,
            params=(*symbols, start_date, end_date),
        )
        if df.empty:
            return {}

        df["datetime"] = pd.to_datetime(df["datetime"])
        results: Dict[str, pd.DataFrame] = {}
        for symbol, group in df.groupby("symbol"):
            results[str(symbol)] = group.drop(columns=["symbol"]).reset_index(drop=True)
        return results

    def _is_cached(self, symbol: str, start_date: str, end_date: str) -> bool:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT start_date, end_date, last_fetched FROM fetch_log WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        if not row:
            return False
        cached_start, cached_end, last_fetched = row
        last_dt = datetime.fromisoformat(last_fetched)
        today = datetime.now().strftime("%Y-%m-%d")
        if cached_start <= start_date and cached_end >= end_date:
            if end_date < today or (datetime.now() - last_dt).total_seconds() < 86400:
                return True
        return False

    def _load_from_cache(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        conn = self._get_conn()
        df = pd.read_sql_query(
            "SELECT date as datetime, open, high, low, close, volume "
            "FROM daily_bars WHERE symbol = ? AND date >= ? AND date <= ? ORDER BY date",
            conn,
            params=(symbol, start_date, end_date),
        )
        df["datetime"] = pd.to_datetime(df["datetime"])
        return df.reset_index(drop=True)

    def _is_intraday_cached(self, symbol: str, interval: str, start_date: str, end_date: str) -> bool:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT start_date, end_date, last_fetched "
            "FROM fetch_log_intraday WHERE symbol = ? AND interval = ?",
            (symbol, interval),
        ).fetchone()
        if not row:
            return False
        cached_start, cached_end, last_fetched = row
        last_dt = datetime.fromisoformat(last_fetched)
        today = datetime.now().strftime("%Y-%m-%d")
        if cached_start <= start_date and cached_end >= end_date:
            if end_date < today or (datetime.now() - last_dt).total_seconds() < 86400:
                return True
        return False

    def _load_intraday_from_cache(
        self, symbol: str, interval: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        conn = self._get_conn()
        df = pd.read_sql_query(
            "SELECT datetime, open, high, low, close, volume "
            "FROM intraday_bars "
            "WHERE symbol = ? AND interval = ? AND datetime >= ? AND datetime < ? "
            "ORDER BY datetime",
            conn,
            params=(
                symbol,
                interval,
                f"{start_date} 00:00:00",
                f"{(pd.Timestamp(end_date) + pd.Timedelta(days=1)).strftime('%Y-%m-%d')} 00:00:00",
            ),
        )
        if df.empty:
            return df
        df["datetime"] = pd.to_datetime(df["datetime"])
        return df.reset_index(drop=True)

    def _is_timeframe_cached(self, symbol: str, timeframe: str, start_date: str, end_date: str) -> bool:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT start_date, end_date, last_fetched "
            "FROM fetch_log_timeframe WHERE symbol = ? AND timeframe = ?",
            (symbol, timeframe),
        ).fetchone()
        if not row:
            return False
        cached_start, cached_end, last_fetched = row
        last_dt = datetime.fromisoformat(last_fetched)
        today = datetime.now().strftime("%Y-%m-%d")
        if cached_start <= start_date and cached_end >= end_date:
            if end_date < today or (datetime.now() - last_dt).total_seconds() < 86400:
                return True
        return False

    def _load_timeframe_from_cache(
        self, symbol: str, timeframe: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        conn = self._get_conn()
        df = pd.read_sql_query(
            "SELECT datetime, open, high, low, close, volume "
            "FROM timeframe_bars "
            "WHERE symbol = ? AND timeframe = ? AND datetime >= ? AND datetime < ? "
            "ORDER BY datetime",
            conn,
            params=(
                symbol,
                timeframe,
                f"{start_date} 00:00:00",
                f"{(pd.Timestamp(end_date) + pd.Timedelta(days=1)).strftime('%Y-%m-%d')} 00:00:00",
            ),
        )
        if df.empty:
            return df
        df["datetime"] = pd.to_datetime(df["datetime"])
        return df.reset_index(drop=True)

    def _store_to_cache(self, symbol: str, df: pd.DataFrame):
        conn = self._get_conn()
        conn.execute("DELETE FROM daily_bars WHERE symbol = ?", (symbol,))
        records = [
            (
                symbol,
                row.datetime.strftime("%Y-%m-%d"),
                row.open,
                row.high,
                row.low,
                row.close,
                row.volume,
            )
            for row in df.itertuples(index=False)
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO daily_bars (symbol, date, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            records,
        )
        start_s, end_s = _df_start_end_dates(df)
        conn.execute(
            "INSERT OR REPLACE INTO fetch_log (symbol, last_fetched, start_date, end_date, row_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                symbol,
                datetime.now().isoformat(),
                start_s,
                end_s,
                len(df),
            ),
        )
        conn.commit()

    def _store_intraday_to_cache(self, symbol: str, interval: str, df: pd.DataFrame):
        conn = self._get_conn()
        conn.execute(
            "DELETE FROM intraday_bars WHERE symbol = ? AND interval = ?",
            (symbol, interval),
        )
        records = [
            (
                symbol,
                interval,
                row.datetime.strftime("%Y-%m-%d %H:%M:%S"),
                row.open,
                row.high,
                row.low,
                row.close,
                row.volume,
            )
            for row in df.itertuples(index=False)
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO intraday_bars "
            "(symbol, interval, datetime, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            records,
        )
        start_s, end_s = _df_start_end_dates(df)
        conn.execute(
            "INSERT OR REPLACE INTO fetch_log_intraday "
            "(symbol, interval, last_fetched, start_date, end_date, row_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                symbol,
                interval,
                datetime.now().isoformat(),
                start_s,
                end_s,
                len(df),
            ),
        )
        conn.commit()

    def _store_timeframe_to_cache(self, symbol: str, timeframe: str, df: pd.DataFrame):
        conn = self._get_conn()
        conn.execute(
            "DELETE FROM timeframe_bars WHERE symbol = ? AND timeframe = ?",
            (symbol, timeframe),
        )
        records = [
            (
                symbol,
                timeframe,
                row.datetime.strftime("%Y-%m-%d %H:%M:%S"),
                row.open,
                row.high,
                row.low,
                row.close,
                row.volume,
            )
            for row in df.itertuples(index=False)
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO timeframe_bars "
            "(symbol, timeframe, datetime, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            records,
        )
        start_s, end_s = _df_start_end_dates(df)
        source_name = "polygon" if self._should_use_polygon(timeframe, start_s, end_s) else "yfinance"
        conn.execute(
            "INSERT OR REPLACE INTO fetch_log_timeframe "
            "(symbol, timeframe, last_fetched, start_date, end_date, row_count, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                symbol,
                timeframe,
                datetime.now().isoformat(),
                start_s,
                end_s,
                len(df),
                source_name,
            ),
        )
        conn.commit()

    def _should_use_polygon(self, timeframe: str, start_date: str, end_date: str) -> bool:
        if not self.polygon_api_key:
            return False
        if self.data_source == "polygon":
            return True
        if self.data_source == "yfinance":
            return False
        if timeframe not in SUPPORTED_INTRADAY_INTERVALS:
            return False
        start = pd.Timestamp(start_date).normalize()
        end = pd.Timestamp(end_date).normalize()
        days = int((end - start).days) + 1
        return days > _yfinance_intraday_max_days(timeframe)

    def close(self):
        if self._http is not None:
            self._http.close()
            self._http = None
        if self._conn:
            self._conn.close()
            self._conn = None


def _chunked(values: Iterable[str], size: int) -> List[List[str]]:
    chunk: List[str] = []
    out: List[List[str]] = []
    for value in values:
        chunk.append(value)
        if len(chunk) >= size:
            out.append(chunk)
            chunk = []
    if chunk:
        out.append(chunk)
    return out


def _build_date_chunks(start_date: str, end_date: str, max_days: int) -> List[Tuple[pd.Timestamp, pd.Timestamp]]:
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    if end < start:
        return []

    out: List[Tuple[pd.Timestamp, pd.Timestamp]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + pd.Timedelta(days=max_days - 1), end)
        out.append((cursor, chunk_end))
        cursor = chunk_end + pd.Timedelta(days=1)
    return out


def _intraday_chunk_days(interval: str) -> int:
    iv = _normalize_timeframe(interval)
    if iv in {"1m"}:
        return 6
    if iv in {"2m", "5m", "15m", "30m", "60m", "90m"}:
        return 30
    return 30


def _normalize_timeframe(timeframe: str) -> str:
    tf = (timeframe or "").strip().lower()
    aliases = {
        "d": "1d",
        "day": "1d",
        "daily": "1d",
        "w": "1wk",
        "wk": "1wk",
        "week": "1wk",
        "weekly": "1wk",
        "mth": "1mo",
        "mo": "1mo",
        "month": "1mo",
        "monthly": "1mo",
    }
    return aliases.get(tf, tf)


def _polygon_params_for_timeframe(timeframe: str) -> Tuple[int, str]:
    tf = _normalize_timeframe(timeframe)
    mapping = {
        "1m": (1, "minute"),
        "2m": (2, "minute"),
        "5m": (5, "minute"),
        "15m": (15, "minute"),
        "30m": (30, "minute"),
        "60m": (60, "minute"),
        "90m": (90, "minute"),
        "1d": (1, "day"),
        "1wk": (1, "week"),
        "1mo": (1, "month"),
    }
    if tf not in mapping:
        raise ValueError(f"Unsupported timeframe for polygon: {timeframe}")
    return mapping[tf]


def _polygon_chunk_days(timeframe: str) -> int:
    tf = _normalize_timeframe(timeframe)
    if tf == "1m":
        return 31
    if tf in {"2m", "5m", "15m"}:
        return 120
    if tf in {"30m", "60m", "90m"}:
        return 365
    if tf in {"1d", "1wk", "1mo"}:
        return 3650
    return 365


def _yfinance_intraday_max_days(interval: str) -> int:
    iv = _normalize_timeframe(interval)
    if iv == "1m":
        return 7
    if iv in {"2m", "5m", "15m", "30m", "60m", "90m"}:
        return 60
    return 60


def _default_start_date(days_back: Optional[int] = None, years_back: Optional[int] = None) -> str:
    if days_back is not None:
        dt = datetime.now() - timedelta(days=max(int(days_back), 1))
        return dt.strftime("%Y-%m-%d")
    if years_back is not None:
        dt = datetime.now() - timedelta(days=max(int(years_back), 1) * 365 + 60)
        return dt.strftime("%Y-%m-%d")
    return (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")


def _df_start_end_dates(df: pd.DataFrame) -> Tuple[str, str]:
    if df.empty:
        today = datetime.now().strftime("%Y-%m-%d")
        return today, today
    start_s = pd.Timestamp(df["datetime"].iloc[0]).strftime("%Y-%m-%d")
    end_s = pd.Timestamp(df["datetime"].iloc[-1]).strftime("%Y-%m-%d")
    return start_s, end_s
