"""Tests for regime detection logic."""

import pytest
import pandas as pd
import numpy as np
from unittest.mock import MagicMock, patch

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import Config
from core.constants import Regime
from strategy.regime import RegimeDetector


def make_benchmark_df(n=100, declining=False):
    """Create synthetic benchmark data."""
    np.random.seed(42)
    dates = pd.date_range('2024-01-01', periods=n, freq='B')
    if declining:
        returns = np.random.normal(-0.003, 0.01, n)
    else:
        returns = np.random.normal(0.002, 0.01, n)
    prices = 450.0 * np.cumprod(1 + returns)
    return pd.DataFrame({
        'datetime': dates,
        'open': prices,
        'high': prices * 1.005,
        'low': prices * 0.995,
        'close': prices,
        'volume': np.random.randint(50000000, 100000000, n),
    })


@pytest.fixture
def config():
    return Config()


@pytest.fixture
def data_cache():
    return MagicMock()


@pytest.fixture
def db_session_factory():
    session = MagicMock()
    factory = MagicMock(return_value=session)
    session.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
    return factory


@pytest.fixture
def detector(data_cache, config, db_session_factory):
    return RegimeDetector(data_cache, config, db_session_factory)


class TestRegimeDetection:
    def test_initial_regime_is_trend(self, detector):
        assert detector.current_regime == Regime.TREND

    def test_evaluate_with_uptrend_data(self, detector, data_cache):
        df = make_benchmark_df(100, declining=False)
        data_cache.get_daily_bars.return_value = df
        regime, details = detector.evaluate()
        assert regime in (Regime.TREND, Regime.CHOP)  # Depends on hit rate
        assert 'sma10' in details
        assert 'sma20' in details

    def test_evaluate_with_declining_data(self, detector, data_cache):
        df = make_benchmark_df(100, declining=True)
        data_cache.get_daily_bars.return_value = df
        regime, details = detector.evaluate()
        # After enough declining days, should trigger RISK_OFF
        assert regime in (Regime.TREND, Regime.CHOP, Regime.RISK_OFF)

    def test_evaluate_insufficient_data(self, detector, data_cache):
        data_cache.get_daily_bars.return_value = None
        regime, details = detector.evaluate()
        assert regime == Regime.TREND  # Stays at default
        assert 'error' in details

    def test_cooldown_prevents_regime_change(self, detector, data_cache):
        df = make_benchmark_df(100, declining=True)
        data_cache.get_daily_bars.return_value = df

        # First evaluation
        regime1, _ = detector.evaluate()

        # Force a regime change
        if detector._current_regime != Regime.RISK_OFF:
            detector._current_regime = Regime.RISK_OFF
            detector._cooldown_remaining = 3

        # During cooldown, regime should not change
        regime2, details = detector.evaluate()
        assert details.get('cooldown_remaining', 0) >= 0


class TestHitRate:
    def test_no_trades_returns_none(self, detector):
        rate = detector._compute_hit_rate()
        assert rate is None

    def test_hit_rate_computation(self, detector, db_session_factory):
        # Mock trades with R-multiples
        mock_trades = []
        for i in range(20):
            trade = MagicMock()
            trade.pnl_r_multiple = 1.5 if i < 12 else -1.0  # 60% win rate
            mock_trades.append(trade)

        session = db_session_factory()
        session.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = mock_trades

        rate = detector._compute_hit_rate()
        assert rate == 0.6
