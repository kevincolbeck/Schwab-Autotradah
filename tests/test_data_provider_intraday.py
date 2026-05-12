"""Tests for intraday helper functions in data provider."""

import pandas as pd

from backtest.data_provider import HistoricalDataProvider, _build_date_chunks, _intraday_chunk_days


def test_build_date_chunks_splits_expected_ranges():
    chunks = _build_date_chunks("2024-01-01", "2024-01-10", 3)
    assert chunks == [
        (pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-03")),
        (pd.Timestamp("2024-01-04"), pd.Timestamp("2024-01-06")),
        (pd.Timestamp("2024-01-07"), pd.Timestamp("2024-01-09")),
        (pd.Timestamp("2024-01-10"), pd.Timestamp("2024-01-10")),
    ]


def test_intraday_chunk_days_by_interval():
    assert _intraday_chunk_days("1m") == 6
    assert _intraday_chunk_days("5m") == 30
    assert _intraday_chunk_days("15m") == 30


def test_normalize_download_df_handles_multiindex_columns(tmp_path):
    provider = HistoricalDataProvider(cache_db_path=str(tmp_path / "cache.db"))
    idx = pd.date_range("2024-01-01", periods=2, freq="h")
    cols = pd.MultiIndex.from_product(
        [["AAPL"], ["Open", "High", "Low", "Close", "Volume"]],
        names=["Ticker", "Price"],
    )
    raw = pd.DataFrame(
        [
            [100, 101, 99, 100.5, 1000],
            [100.5, 102, 100, 101, 1200],
        ],
        index=idx,
        columns=cols,
    )
    out = provider._normalize_download_df(raw)
    provider.close()
    assert not out.empty
    assert list(out.columns) == ["datetime", "open", "high", "low", "close", "volume"]
