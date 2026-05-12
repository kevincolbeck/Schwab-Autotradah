"""Simulate order fills using daily OHLCV bars.

Translates the live bot's intraday logic to daily-bar approximations:
- Breakout entry: daily high > breakout_level -> fill at trigger + slippage
- Volume check: daily_volume > avg_volume * threshold
- Stop-loss: daily low <= stop_price -> fill at stop_price
- Breakeven: daily high >= entry + 1R -> move stop to entry
- EOD exit: check close vs SMA10/SMA20
"""

import logging
from dataclasses import dataclass

import pandas as pd

from backtest.config_overrides import BacktestConfig
from backtest.portfolio import BacktestPosition

logger = logging.getLogger(__name__)


@dataclass
class FillResult:
    """Result of an order simulation check."""
    filled: bool
    fill_price: float = 0.0
    reason: str = ""


def check_breakout_entry(
    today_bar: pd.Series,
    breakout_level: float,
    entry_buffer_pct: float,
    avg_volume_20d: float,
    bt_config: BacktestConfig,
) -> FillResult:
    """Check if today's bar triggers a breakout entry."""
    trigger = breakout_level * (1 + entry_buffer_pct / 100)

    if today_bar["high"] < trigger:
        return FillResult(False, reason="High did not reach trigger")

    if avg_volume_20d > 0:
        vol_ratio = today_bar["volume"] / avg_volume_20d
        if vol_ratio < bt_config.min_daily_vol_ratio:
            return FillResult(False, reason=f"Vol ratio {vol_ratio:.2f} < {bt_config.min_daily_vol_ratio}")

    fill_price = trigger * (1 + bt_config.slippage_pct / 100)
    fill_price = min(fill_price, today_bar["high"])

    return FillResult(True, fill_price, f"Breakout fill at {fill_price:.2f}")


def check_stop_loss(
    today_bar: pd.Series,
    position: BacktestPosition,
    bt_config: BacktestConfig,
) -> FillResult:
    """Check if the stop-loss was hit during today's bar.

    If breakeven_before_stop_on_same_bar is True, checks if the high
    reached +1R before checking if the low hit the stop. This models
    the optimistic assumption that price went up before going down.
    """
    stop_price = position.current_stop

    # Breakeven check first (if enabled and not already set)
    if bt_config.breakeven_before_stop_on_same_bar and not position.breakeven_set:
        be_trigger = position.entry_price + position.r_value
        if today_bar["high"] >= be_trigger:
            position.breakeven_set = True
            position.current_stop = position.entry_price
            stop_price = position.entry_price

    if today_bar["low"] <= stop_price:
        fill_price = stop_price * (1 - bt_config.slippage_pct / 100)
        fill_price = max(fill_price, today_bar["low"])
        return FillResult(True, fill_price, f"Stop hit at {fill_price:.2f}")

    return FillResult(False)


def check_breakeven_only(
    today_bar: pd.Series,
    position: BacktestPosition,
) -> bool:
    """Check and set breakeven stop if +1R reached. Returns True if just set."""
    if position.breakeven_set:
        return False

    be_trigger = position.entry_price + position.r_value
    if today_bar["high"] >= be_trigger:
        position.breakeven_set = True
        position.current_stop = position.entry_price
        return True
    return False


def check_eod_exit(
    today_close: float,
    sma10_val: float,
    sma20_val: float,
    sma_closeness_threshold: float,
) -> FillResult:
    """Check if close is below the exit SMA line.

    Same logic as TradeManager._check_eod_exit():
    - If SMA10 and SMA20 are within closeness_threshold% of each other,
      use SMA20; otherwise use SMA10.
    """
    if sma10_val <= 0:
        return FillResult(False)

    closeness = abs(sma10_val - sma20_val) / sma10_val * 100
    if closeness <= sma_closeness_threshold:
        exit_line = sma20_val
    else:
        exit_line = sma10_val

    if today_close < exit_line:
        return FillResult(True, today_close, f"EOD exit: close {today_close:.2f} < {exit_line:.2f}")

    return FillResult(False)
