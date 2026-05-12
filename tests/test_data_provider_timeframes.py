"""Tests for multi-timeframe support in HistoricalDataProvider."""

import pandas as pd

from backtest.data_provider import (
    HistoricalDataProvider,
    _normalize_timeframe,
    _polygon_params_for_timeframe,
)


def test_normalize_timeframe_aliases():
    assert _normalize_timeframe("daily") == "1d"
    assert _normalize_timeframe("week") == "1wk"
    assert _normalize_timeframe("monthly") == "1mo"
    assert _normalize_timeframe("5m") == "5m"


def test_polygon_params_for_timeframe():
    assert _polygon_params_for_timeframe("1d") == (1, "day")
    assert _polygon_params_for_timeframe("1wk") == (1, "week")
    assert _polygon_params_for_timeframe("1mo") == (1, "month")
    assert _polygon_params_for_timeframe("15m") == (15, "minute")


def test_timeframe_cache_round_trip(tmp_path):
    provider = HistoricalDataProvider(cache_db_path=str(tmp_path / "cache.db"))
    try:
        frame = pd.DataFrame(
            {
                "datetime": pd.to_datetime(["2024-01-05", "2024-01-12"]),
                "open": [10.0, 11.0],
                "high": [11.0, 12.0],
                "low": [9.5, 10.5],
                "close": [10.8, 11.5],
                "volume": [1000, 1200],
            }
        )
        provider._store_timeframe_to_cache("AAPL", "1wk", frame)
        assert provider._is_timeframe_cached("AAPL", "1wk", "2024-01-05", "2024-01-12")

        loaded = provider._load_timeframe_from_cache("AAPL", "1wk", "2024-01-01", "2024-01-31")
        assert len(loaded) == 2
        assert list(loaded.columns) == ["datetime", "open", "high", "low", "close", "volume"]
    finally:
        provider.close()
