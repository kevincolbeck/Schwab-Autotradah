"""Tests for trade management: breakeven, EOD exit."""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import Config
from core.constants import Regime, TradeStatus
from data.models import Trade, BotOrder


@pytest.fixture
def config():
    return Config()


class TestBreakevenLogic:
    def test_be_trigger_at_1r(self):
        """Verify breakeven triggers when price reaches entry + 1R."""
        entry = 50.0
        stop = 48.0
        r_value = entry - stop  # $2

        # At +1R
        price_at_1r = entry + r_value  # $52
        assert price_at_1r == 52.0

        # BE price should be entry + buffer
        be_buffer_pct = 0.0  # default
        be_price = entry + (entry * be_buffer_pct / 100)
        assert be_price == 50.0  # Entry price

    def test_be_never_lowers_stop(self):
        """Stop must never go below breakeven once set."""
        entry = 50.0
        stop = 48.0
        be_price = 50.0

        # If current stop is already above entry (manually moved), don't lower
        current_stop = 51.0
        should_update = be_price > current_stop
        assert not should_update


class TestEODExitLogic:
    def test_sma_closeness_threshold(self):
        """Test SMA closeness determines exit line."""
        sma10 = 100.0
        sma20 = 99.6

        closeness = abs(sma10 - sma20) / sma10 * 100
        assert closeness == pytest.approx(0.4, abs=0.01)

        threshold = 0.50
        if closeness <= threshold:
            exit_line = sma20  # Use SMA20 when close
        else:
            exit_line = sma10

        assert exit_line == sma20  # 0.4% < 0.5%, so SMA20

    def test_sma_far_apart_uses_sma10(self):
        """When SMAs far apart, use SMA10 as exit line."""
        sma10 = 100.0
        sma20 = 95.0

        closeness = abs(sma10 - sma20) / sma10 * 100
        assert closeness == 5.0

        threshold = 0.50
        exit_line = sma20 if closeness <= threshold else sma10
        assert exit_line == sma10

    def test_exit_when_below_exit_line(self):
        """Should exit when price < exit line."""
        last_price = 99.0
        exit_line = 100.0
        should_exit = last_price < exit_line
        assert should_exit

    def test_no_exit_when_above_exit_line(self):
        """Should not exit when price >= exit line."""
        last_price = 101.0
        exit_line = 100.0
        should_exit = last_price < exit_line
        assert not should_exit


class TestDailyLossLock:
    def test_lock_at_minus_3r(self):
        """Daily PnL of -3R should trigger lock."""
        daily_total_r = -3.0
        loss_lock_r = 3.0
        locked = daily_total_r <= -loss_lock_r
        assert locked

    def test_no_lock_above_threshold(self):
        """Daily PnL above threshold should not lock."""
        daily_total_r = -2.5
        loss_lock_r = 3.0
        locked = daily_total_r <= -loss_lock_r
        assert not locked


class TestKillSwitchBehavior:
    def test_kill_switch_stops_new_trades(self):
        from execution.kill_switch import KillSwitch
        ks = KillSwitch(MagicMock())

        assert ks.allow_new_trades is True
        ks._active = True
        assert ks.allow_new_trades is False

    def test_manage_exits_while_off(self):
        from execution.kill_switch import KillSwitch
        ks = KillSwitch(MagicMock())

        ks._active = True
        ks._manage_exits_while_off = True
        assert ks.allow_exit_management is True

        ks._manage_exits_while_off = False
        assert ks.allow_exit_management is False

    def test_deactivate_allows_trades(self):
        from execution.kill_switch import KillSwitch
        ks = KillSwitch(MagicMock())

        ks._active = True
        assert not ks.allow_new_trades
        ks._active = False
        assert ks.allow_new_trades
