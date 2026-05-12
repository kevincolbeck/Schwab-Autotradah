"""Tests for position sizing logic."""

import pytest
from unittest.mock import MagicMock

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import Config
from core.constants import Regime
from strategy.sizing import PositionSizer


@pytest.fixture
def config():
    return Config()


@pytest.fixture
def db_session_factory():
    session = MagicMock()
    factory = MagicMock(return_value=session)
    # Default: no open positions
    session.query.return_value.filter.return_value.count.return_value = 0
    session.query.return_value.filter.return_value.all.return_value = []
    return factory


@pytest.fixture
def sizer(config, db_session_factory):
    return PositionSizer(config, db_session_factory)


class TestPositionSizing:
    def test_basic_sizing(self, sizer):
        # $100k equity, 0.5% risk, entry $50, stop $48
        shares, details = sizer.calculate(
            entry_price=50.0, stop_price=48.0,
            equity=100_000.0, regime=Regime.TREND,
            risk_per_trade_pct=0.5,
        )
        # risk = $500, per_share_risk = $2, shares = 250
        assert shares == 250
        assert details['risk_dollars'] == 500.0
        assert details['per_share_risk'] == 2.0

    def test_chop_regime_reduces_size(self, sizer):
        shares_trend, _ = sizer.calculate(50.0, 48.0, 100_000.0, Regime.TREND, 0.5)
        shares_chop, _ = sizer.calculate(50.0, 48.0, 100_000.0, Regime.CHOP, 0.5)
        assert shares_chop < shares_trend
        assert shares_chop == 125  # 0.5 multiplier

    def test_risk_off_returns_zero(self, sizer):
        shares, details = sizer.calculate(50.0, 48.0, 100_000.0, Regime.RISK_OFF, 0.5)
        assert shares == 0

    def test_invalid_risk_returns_zero(self, sizer):
        # Stop above entry
        shares, details = sizer.calculate(48.0, 50.0, 100_000.0, Regime.TREND, 0.5)
        assert shares == 0

    def test_max_position_pct_constraint(self, sizer):
        # With tiny stop distance, position would be huge
        sizer.config.sizing.max_position_pct_equity = 20.0
        shares, details = sizer.calculate(
            entry_price=100.0, stop_price=99.90,
            equity=100_000.0, regime=Regime.TREND,
            risk_per_trade_pct=0.5,
        )
        # Max position = 20% of $100k = $20k / $100 = 200 shares
        assert shares <= 200

    def test_max_positions_reached(self, sizer, db_session_factory):
        session = db_session_factory()
        session.query.return_value.filter.return_value.count.return_value = 8  # At max
        shares, details = sizer.calculate(50.0, 48.0, 100_000.0, Regime.TREND, 0.5)
        assert shares == 0
        assert "Max positions" in details.get('reason', '')

    def test_risk_clamped_to_bounds(self, sizer):
        # Risk too high
        shares, details = sizer.calculate(50.0, 48.0, 100_000.0, Regime.TREND, 5.0)
        assert details['risk_per_trade_pct'] == 2.0  # Clamped

        # Risk too low
        shares, details = sizer.calculate(50.0, 48.0, 100_000.0, Regime.TREND, 0.01)
        assert details['risk_per_trade_pct'] == 0.05  # Clamped
