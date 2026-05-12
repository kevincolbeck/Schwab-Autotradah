"""Entry signal detection: breakout and pullback/reclaim."""

import logging
from datetime import datetime, time as dtime
from typing import Dict, List, Optional, Tuple

import pandas as pd

from core.constants import Regime
from core.config import Config
from data.market_data import MarketDataCache
from strategy.indicators import (
    sma, breakout_level, lowest_low, volume_pace, highest_high,
)

logger = logging.getLogger(__name__)


class SignalResult:
    """Describes an entry signal for a symbol."""
    def __init__(
        self, symbol: str, signal_type: str, triggered: bool,
        entry_price: float = 0.0, stop_price: float = 0.0,
        breakout_level_val: float = 0.0, reason: str = "",
        details: Optional[dict] = None,
    ):
        self.symbol = symbol
        self.signal_type = signal_type  # 'BREAKOUT' or 'PULLBACK'
        self.triggered = triggered
        self.entry_price = entry_price
        self.stop_price = stop_price
        self.breakout_level = breakout_level_val
        self.reason = reason
        self.details = details or {}


class SignalDetector:
    """Detects breakout and pullback entry signals."""

    def __init__(self, data_cache: MarketDataCache, config: Config):
        self.data = data_cache
        self.config = config

    def check_breakout(
        self, symbol: str, quote: dict, current_time: datetime,
    ) -> SignalResult:
        """Check for intraday breakout entry signal.

        Requirements:
        - Within trigger time window
        - Price >= breakout_level * (1 + entry_buffer)
        - Volume pace >= min_rel_vol_pace
        """
        cfg = self.config.breakout

        # Time window check
        et_time = current_time.time()
        window_start = dtime(cfg.trigger_start_hour, cfg.trigger_start_minute)
        window_end = dtime(cfg.trigger_end_hour, cfg.trigger_end_minute)
        if not (window_start <= et_time <= window_end):
            return SignalResult(symbol, 'BREAKOUT', False, reason="Outside trigger window")

        # Get daily bars for breakout level calculation
        df = self.data.get_daily_bars(symbol)
        if df is None or len(df) < cfg.lookback:
            return SignalResult(symbol, 'BREAKOUT', False, reason="Insufficient data")

        # Compute breakout level (highest high over lookback period)
        bl = highest_high(df['high'], cfg.lookback).iloc[-1]
        if pd.isna(bl):
            return SignalResult(symbol, 'BREAKOUT', False, reason="Cannot compute breakout level")

        entry_trigger = bl * (1 + cfg.entry_buffer_pct / 100)
        last_price = quote.get('last', 0)

        if last_price < entry_trigger:
            return SignalResult(
                symbol, 'BREAKOUT', False,
                breakout_level_val=bl,
                reason=f"Price {last_price:.2f} < trigger {entry_trigger:.2f}",
            )

        # Volume pace check
        today_volume = quote.get('volume', 0)
        avg_vol = quote.get('avg_volume', 0)
        if avg_vol <= 0:
            # Try from daily bars
            if len(df) >= 20:
                avg_vol = df['volume'].tail(20).mean()

        # Calculate minutes since open
        market_open = current_time.replace(hour=9, minute=30, second=0, microsecond=0)
        minutes_elapsed = max(1, (current_time - market_open).total_seconds() / 60)

        vol_pace = volume_pace(today_volume, avg_vol, minutes_elapsed)
        if vol_pace < cfg.min_rel_vol_pace:
            return SignalResult(
                symbol, 'BREAKOUT', False,
                breakout_level_val=bl,
                reason=f"Vol pace {vol_pace:.2f} < {cfg.min_rel_vol_pace}",
            )

        # Compute stop price
        base_low = lowest_low(df['low'], cfg.lookback).iloc[-1]
        stop_buffer = self.config.stop.stop_buffer_pct / 100
        stop_price = base_low * (1 - stop_buffer)

        # Entry price (stop-limit)
        entry_price = entry_trigger
        limit_price = entry_price * (1 + cfg.limit_offset_pct / 100)

        return SignalResult(
            symbol, 'BREAKOUT', True,
            entry_price=entry_price,
            stop_price=stop_price,
            breakout_level_val=bl,
            reason=f"Breakout! Price={last_price:.2f} > {entry_trigger:.2f}, VolPace={vol_pace:.2f}",
            details={
                'trigger_price': entry_trigger,
                'limit_price': limit_price,
                'breakout_level': bl,
                'base_low': base_low,
                'volume_pace': vol_pace,
                'last_price': last_price,
            },
        )

    def check_pullback(
        self, symbol: str, quote: dict, current_time: datetime,
    ) -> SignalResult:
        """Check for pullback/reclaim entry signal (CHOP regime).

        Symbol must have pulled back to SMA20 or SMA50 and then reclaimed.
        """
        cfg = self.config.pullback

        # Time window check (same as breakout)
        et_time = current_time.time()
        bcfg = self.config.breakout
        window_start = dtime(bcfg.trigger_start_hour, bcfg.trigger_start_minute)
        window_end = dtime(bcfg.trigger_end_hour, bcfg.trigger_end_minute)
        if not (window_start <= et_time <= window_end):
            return SignalResult(symbol, 'PULLBACK', False, reason="Outside trigger window")

        df = self.data.get_daily_bars(symbol)
        if df is None or len(df) < 50:
            return SignalResult(symbol, 'PULLBACK', False, reason="Insufficient data")

        close = df['close']
        high = df['high']
        low = df['low']
        last_price = quote.get('last', 0)

        sma20 = sma(close, 20)
        sma50 = sma(close, 50)

        # Check if price recently pulled back to SMA20 or SMA50
        # "Recently" = in last 5 bars, low touched or went below SMA
        pullback_detected = False
        pullback_level = None
        pullback_sma_name = None

        for lookback_bar in range(1, 6):
            idx = -lookback_bar
            if cfg.use_sma20 and pd.notna(sma20.iloc[idx]):
                if low.iloc[idx] <= sma20.iloc[idx] * 1.002:  # Within 0.2%
                    pullback_detected = True
                    pullback_level = sma20.iloc[-1]
                    pullback_sma_name = "SMA20"
                    break
            if cfg.use_sma50 and pd.notna(sma50.iloc[idx]):
                if low.iloc[idx] <= sma50.iloc[idx] * 1.002:
                    pullback_detected = True
                    pullback_level = sma50.iloc[-1]
                    pullback_sma_name = "SMA50"
                    break

        if not pullback_detected:
            return SignalResult(symbol, 'PULLBACK', False, reason="No recent pullback to SMA20/SMA50")

        # Check reclaim: current price must be above the SMA
        if last_price <= pullback_level:
            return SignalResult(
                symbol, 'PULLBACK', False,
                reason=f"Price {last_price:.2f} still below {pullback_sma_name} {pullback_level:.2f}",
            )

        # Entry above reclaim candle high or close
        if cfg.entry_above_reclaim_close:
            entry_price = close.iloc[-1]  # Previous close as entry reference
        else:
            entry_price = high.iloc[-1]  # Previous high

        # Trigger: last_price must be above entry
        if last_price < entry_price:
            return SignalResult(
                symbol, 'PULLBACK', False,
                reason=f"Price {last_price:.2f} < reclaim entry {entry_price:.2f}",
            )

        # Stop below pullback swing low
        recent_lows = low.tail(10)
        swing_low = recent_lows.min()
        stop_buffer = cfg.stop_buffer_pct / 100
        stop_price = swing_low * (1 - stop_buffer)

        return SignalResult(
            symbol, 'PULLBACK', True,
            entry_price=last_price,
            stop_price=stop_price,
            reason=f"Pullback reclaim at {pullback_sma_name}! Price={last_price:.2f}",
            details={
                'pullback_sma': pullback_sma_name,
                'pullback_level': pullback_level,
                'swing_low': swing_low,
                'last_price': last_price,
            },
        )

    def scan_for_signals(
        self, symbols: List[str], quotes: Dict[str, dict],
        current_time: datetime, regime: Regime,
    ) -> List[SignalResult]:
        """Scan all symbols for entry signals based on current regime."""
        signals = []

        for symbol in symbols:
            quote = quotes.get(symbol)
            if not quote:
                continue

            if regime == Regime.RISK_OFF:
                continue  # No entries in risk-off

            if regime == Regime.TREND or (regime == Regime.CHOP and self.config.regime.chop_breakouts_enabled):
                result = self.check_breakout(symbol, quote, current_time)
                if result.triggered:
                    signals.append(result)
                    continue

            if regime == Regime.CHOP:
                result = self.check_pullback(symbol, quote, current_time)
                if result.triggered:
                    signals.append(result)

        return signals
