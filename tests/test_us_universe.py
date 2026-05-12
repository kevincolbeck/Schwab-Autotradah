"""Tests for automatic US market universe loading and filtering."""

import json

import pandas as pd

from data.us_universe import (
    USMarketUniverseProvider,
    _is_stock_like_name,
    _normalize_symbol,
)


def test_normalize_symbol_converts_dot_and_filters_invalid():
    assert _normalize_symbol("BRK.B") == "BRK-B"
    assert _normalize_symbol("AAPL") == "AAPL"
    assert _normalize_symbol("AAPLW") is None
    assert _normalize_symbol("BAD$") is None


def test_is_stock_like_name_filters_etf_and_fund_words():
    assert _is_stock_like_name("APPLE INC COMMON STOCK")
    assert not _is_stock_like_name("SPDR S&P 500 ETF TRUST")
    assert not _is_stock_like_name("SOME WARRANT")


def test_parse_listed_files_filters_test_issue_and_non_stock_names(tmp_path):
    provider = USMarketUniverseProvider(cache_path=str(tmp_path / "cache.json"))

    nasdaq_df = pd.DataFrame(
        [
            {"Symbol": "AAPL", "Security Name": "Apple Inc. Common Stock", "Test Issue": "N"},
            {"Symbol": "QQQ", "Security Name": "Invesco QQQ ETF", "Test Issue": "N"},
            {"Symbol": "TSLA", "Security Name": "Tesla Inc. Common Stock", "Test Issue": "Y"},
        ]
    )
    other_df = pd.DataFrame(
        [
            {"ACT Symbol": "BRK.B", "Security Name": "Berkshire Hathaway Inc.", "Test Issue": "N"},
            {"ACT Symbol": "XYZU", "Security Name": "Random Unit", "Test Issue": "N"},
        ]
    )

    symbols = set()
    symbols.update(provider._parse_nasdaq_listed(nasdaq_df))
    symbols.update(provider._parse_other_listed(other_df))

    assert symbols == {"AAPL", "BRK-B"}


def test_get_symbols_uses_cached_snapshot_if_download_fails(tmp_path, monkeypatch):
    cache_file = tmp_path / "auto_universe_cache.json"
    cache_file.write_text(
        json.dumps({"as_of": "2025-01-01", "symbols": ["MSFT", "AAPL"]}),
        encoding="utf-8",
    )

    provider = USMarketUniverseProvider(cache_path=str(cache_file))
    monkeypatch.setattr(provider, "_download_symbols", lambda: [])

    symbols = provider.get_symbols(force_refresh=False)
    assert symbols == ["AAPL", "MSFT"]
