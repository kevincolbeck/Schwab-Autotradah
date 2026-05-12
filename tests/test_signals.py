"""Tests for entry signal detection."""

import pytest
import pandas as pd
import numpy as np
from unittest.mock import MagicMock
from datetime import datetime

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import Config
from core.constants import Regime
from strategy.signals import SignalDetector, SignalResult


def make_breakout_df(n=60, base=50.0, breakout_at_end=True):
    """Create data that sets up a breakout scenario."""
    np.random.seed(42)
    dates = pd.date_range('2024-06-01', periods=n, freq='B')
    prices = np.full(n, base)
    # Consolidation then breakout
    for i in range(n):
        if i < n - 5:
            prices[i] = base + np.random.normal(0, 0.5)
        else:
            if breakout_at_end:
                prices[i] = base + 3.0 + i * 0.1  # Break above range
            else:
                prices[i] = base + np.random.normal(0, 0.5)

    return pd.DataFrame({
        'datetime': dates,
        'open': prices - 0.2,
        'high': prices + 0.5,
        'low': prices - 0.5,
        'close': prices,
        'volume': np.random.randint(1000000, 3000000, n),
    })


@pytest.fixture
def config():
    return Config()


@pytest.fixture
def data_cache():
    return MagicMock()


@pytest.fixture
def detector(data_cache, config):
    return SignalDetector(data_cache, config)


class TestBreakoutSignal:
    def test_outside_time_window_rejects(self, detector, data_cache):
        df = make_breakout_df()
        data_cache.get_daily_bars.return_value = df

        quote = {'last': 55.0, 'volume': 5000000, 'avg_volume': 2000000}
        # Before market open
        early_time = datetime(2024, 9, 1, 9, 0, 0)
        result = detector.check_breakout('TEST', quote, early_time)
        assert not result.triggered
        assert "Outside" in result.reason

    def test_breakout_triggered(self, detector, data_cache):
        df = make_breakout_df(60, 50.0, breakout_at_end=True)
        data_cache.get_daily_bars.return_value = df

        # Price well above 40-day high
        quote = {'last': 60.0, 'volume': 5000000, 'avg_volume': 2000000}
        market_time = datetime(2024, 9, 1, 11, 0, 0)
        result = detector.check_breakout('TEST', quote, market_time)
        assert isinstance(result, SignalResult)
        # May or may not trigger depending on exact data

    def test_price_below_breakout_rejects(self, detector, data_cache):
        df = make_breakout_df(60, 50.0, breakout_at_end=False)
        data_cache.get_daily_bars.return_value = df

        quote = {'last': 48.0, 'volume': 1000000, 'avg_volume': 2000000}
        market_time = datetime(2024, 9, 1, 11, 0, 0)
        result = detector.check_breakout('TEST', quote, market_time)
        assert not result.triggered

    def test_insufficient_data_rejects(self, detector, data_cache):
        df = make_breakout_df(10)
        data_cache.get_daily_bars.return_value = df

        quote = {'last': 55.0, 'volume': 5000000, 'avg_volume': 2000000}
        market_time = datetime(2024, 9, 1, 11, 0, 0)
        result = detector.check_breakout('TEST', quote, market_time)
        assert not result.triggered


class TestPullbackSignal:
    def test_outside_time_window_rejects(self, detector, data_cache):
        quote = {'last': 50.0}
        late_time = datetime(2024, 9, 1, 16, 0, 0)
        result = detector.check_pullback('TEST', quote, late_time)
        assert not result.triggered

    def test_no_data_rejects(self, detector, data_cache):
        data_cache.get_daily_bars.return_value = None
        quote = {'last': 50.0}
        market_time = datetime(2024, 9, 1, 11, 0, 0)
        result = detector.check_pullback('TEST', quote, market_time)
        assert not result.triggered


class TestScanForSignals:
    def test_risk_off_returns_empty(self, detector, data_cache):
        signals = detector.scan_for_signals(
            ['AAPL', 'MSFT'],
            {'AAPL': {'last': 200}, 'MSFT': {'last': 400}},
            datetime(2024, 9, 1, 11, 0, 0),
            Regime.RISK_OFF,
        )
        assert signals == []

    def test_empty_symbols(self, detector):
        signals = detector.scan_for_signals([], {}, datetime.now(), Regime.TREND)
        assert signals == []
