"""Bulk importer for backtest data (daily/weekly/monthly/intraday)."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta
from typing import List

from backtest.data_provider import (
    DEFAULT_IMPORT_TIMEFRAMES,
    HistoricalDataProvider,
    SUPPORTED_TIMEFRAMES,
    _normalize_timeframe,
)
from core.config import load_config
from data.us_universe import USMarketUniverseProvider

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    config = load_config("config.yaml")
    parser = argparse.ArgumentParser(
        description="Import and cache market data for long-range backtesting."
    )
    parser.add_argument(
        "--symbols",
        default="ALL_US",
        help="Comma-separated symbols or ALL_US (default).",
    )
    parser.add_argument(
        "--start-date",
        default=(datetime.now() - timedelta(days=365 * 20 + 60)).strftime("%Y-%m-%d"),
        help="Start date (YYYY-MM-DD). Default: ~20 years ago.",
    )
    parser.add_argument(
        "--end-date",
        default=datetime.now().strftime("%Y-%m-%d"),
        help="End date (YYYY-MM-DD). Default: today.",
    )
    parser.add_argument(
        "--timeframes",
        nargs="+",
        default=list(DEFAULT_IMPORT_TIMEFRAMES),
        help="Timeframes (space-separated and/or comma-separated), e.g. 1d 1wk 1mo 5m.",
    )
    parser.add_argument(
        "--source",
        default=config.data.backtest_data_source,
        choices=["auto", "yfinance", "polygon"],
        help="Data source policy.",
    )
    parser.add_argument(
        "--polygon-api-key",
        default=config.data.polygon_api_key,
        help="Polygon API key (optional; overrides config when provided).",
    )
    parser.add_argument(
        "--cache-db",
        default="backtest_data.db",
        help="SQLite cache path.",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Re-fetch even if cache covers the range.",
    )
    parser.add_argument(
        "--max-symbols",
        type=int,
        default=0,
        help="Cap number of symbols (0 = all).",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Print JSON summary in pretty format.",
    )
    return parser.parse_args()


def _resolve_symbols(symbols_arg: str, max_symbols: int) -> List[str]:
    raw = (symbols_arg or "").strip()
    if not raw:
        return []
    if raw.upper() == "ALL_US":
        symbols = USMarketUniverseProvider().get_symbols()
    else:
        symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    if max_symbols > 0:
        symbols = symbols[:max_symbols]
    return symbols


def _resolve_timeframes(timeframes_arg: List[str]) -> List[str]:
    raw_values: List[str] = []
    for token in timeframes_arg or []:
        parts = [p.strip() for p in str(token).split(",") if p.strip()]
        raw_values.extend(parts)
    values = [_normalize_timeframe(v) for v in raw_values]
    values = [v for v in dict.fromkeys(values) if v]
    invalid = [v for v in values if v not in SUPPORTED_TIMEFRAMES]
    if invalid:
        raise ValueError(f"Unsupported timeframe(s): {', '.join(invalid)}")
    return values


def main():
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    symbols = _resolve_symbols(args.symbols, args.max_symbols)
    if not symbols:
        raise SystemExit("No symbols to import.")
    timeframes = _resolve_timeframes(args.timeframes)
    logger.info(
        "Import starting: symbols=%d timeframes=%s range=%s..%s source=%s",
        len(symbols),
        ",".join(timeframes),
        args.start_date,
        args.end_date,
        args.source,
    )

    provider = HistoricalDataProvider(
        cache_db_path=args.cache_db,
        data_source=args.source,
        polygon_api_key=args.polygon_api_key,
        default_history_years=20,
    )
    try:
        summary = provider.import_market_data(
            symbols=symbols,
            start_date=args.start_date,
            end_date=args.end_date,
            timeframes=timeframes,
            force_refresh=args.force_refresh,
            progress_callback=lambda cur, tot, label: logger.info("Progress %d/%d %s", cur, tot, label),
        )
    finally:
        provider.close()

    text = json.dumps(summary, indent=2 if args.pretty else None)
    print(text)


if __name__ == "__main__":
    main()
