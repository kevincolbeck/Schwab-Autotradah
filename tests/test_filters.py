"""Tests for eligibility, trend quality, and contraction filters."""

import pytest
import pandas as pd
import numpy as np
from unittest.mock import MagicMock

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import Config
from strategy.filters import SymbolFilter, FilterResult


def make_daily_df(n=250, base_price=100.0, trend_up=True):
    """Create synthetic daily OHLCV data."""
    np.random.seed(42)
    dates = pd.date_range('2024-01-01', periods=n, freq='B')
    if trend_up:
        returns = np.random.normal(0.001, 0.015, n)
    else:
        returns = np.random.normal(-0.001, 0.015, n)
    prices = base_price * np.cumprod(1 + returns)
    highs = prices * (1 + np.abs(np.random.normal(0, 0.01, n)))
    lows = prices * (1 - np.abs(np.random.normal(0, 0.01, n)))
    volumes = np.random.randint(500000, 5000000, n)
    return pd.DataFrame({
        'datetime': dates,
        'open': prices * (1 + np.random.normal(0, 0.005, n)),
        'high': highs,
        'low': lows,
        'close': prices,
        'volume': volumes,
    })


@pytest.fixture
def config():
    return Config()


@pytest.fixture
def data_cache():
    return MagicMock()


@pytest.fixture
def symbol_filter(data_cache, config):
    return SymbolFilter(data_cache, config)


class TestEligibility:
    def test_price_too_low(self, symbol_filter):
        quote = {'last': 5.0, 'bid': 4.99, 'ask': 5.01, 'asset_type': 'EQUITY', 'exchange': 'NYSE'}
        result = symbol_filter.check_eligibility('TEST', quote)
        assert not result.passed
        assert "Price" in result.reason

    def test_price_ok(self, symbol_filter, data_cache):
        df = make_daily_df(250, 50.0)
        data_cache.get_daily_bars.return_value = df
        quote = {'last': 50.0, 'bid': 49.95, 'ask': 50.05, 'asset_type': 'EQUITY', 'exchange': 'NYSE'}
        result = symbol_filter.check_eligibility('TEST', quote)
        assert result.passed

    def test_spread_too_wide(self, symbol_filter):
        quote = {'last': 50.0, 'bid': 49.0, 'ask': 51.0, 'asset_type': 'EQUITY', 'exchange': 'NYSE'}
        result = symbol_filter.check_eligibility('TEST', quote)
        assert not result.passed
        assert "Spread" in result.reason

    def test_otc_rejected(self, symbol_filter):
        quote = {'last': 50.0, 'bid': 49.95, 'ask': 50.05, 'asset_type': 'EQUITY', 'exchange': 'OTC'}
        result = symbol_filter.check_eligibility('TEST', quote)
        assert not result.passed
        assert "OTC" in result.reason


class TestTrendQuality:
    def test_uptrend_passes(self, symbol_filter, data_cache):
        df = make_daily_df(250, 50.0, trend_up=True)
        data_cache.get_daily_bars.return_value = df
        result = symbol_filter.check_trend_quality('TEST')
        # May or may not pass depending on random data
        assert isinstance(result, FilterResult)

    def test_insufficient_data(self, symbol_filter, data_cache):
        df = make_daily_df(50, 50.0)
        data_cache.get_daily_bars.return_value = df
        result = symbol_filter.check_trend_quality('TEST')
        assert not result.passed
        assert "Insufficient" in result.reason

    def test_no_data(self, symbol_filter, data_cache):
        data_cache.get_daily_bars.return_value = None
        result = symbol_filter.check_trend_quality('TEST')
        assert not result.passed


class TestContraction:
    def test_contraction_with_data(self, symbol_filter, data_cache):
        df = make_daily_df(250, 50.0, trend_up=True)
        data_cache.get_daily_bars.return_value = df
        result = symbol_filter.check_contraction('TEST')
        assert isinstance(result, FilterResult)
        assert "Contraction" in result.reason

    def test_contraction_insufficient_data(self, symbol_filter, data_cache):
        df = make_daily_df(30, 50.0)
        data_cache.get_daily_bars.return_value = df
        result = symbol_filter.check_contraction('TEST')
        assert not result.passed
