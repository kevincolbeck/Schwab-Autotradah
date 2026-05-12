"""Market regime detection: TREND / CHOP / RISK_OFF."""

import logging
from datetime import datetime
from typing import Optional, Tuple

import pandas as pd

from core.constants import Regime
from core.config import Config
from data.market_data import MarketDataCache
from strategy.indicators import sma, sma_declining, sma_rising, consecutive_true
from data.models import RegimeState, Trade, TradeStatus

logger = logging.getLogger(__name__)


class RegimeDetector:
    """Determines market regime based on benchmark SMAs and trade hit rate."""

    def __init__(self, data_cache: MarketDataCache, config: Config, db_session_factory):
        self.data = data_cache
        self.config = config
        self.db_session_factory = db_session_factory
        self._current_regime = Regime.TREND
        self._last_regime_change: Optional[datetime] = None
        self._cooldown_remaining = 0

    @property
    def current_regime(self) -> Regime:
        return self._current_regime

    def evaluate(self) -> Tuple[Regime, dict]:
        """Evaluate current regime. Returns (regime, details_dict)."""
        cfg = self.config.regime
        benchmark = cfg.benchmark

        df = self.data.get_daily_bars(benchmark)
        if df is None or len(df) < cfg.sma_long + cfg.decline_consecutive_days + 5:
            logger.warning(f"Insufficient benchmark data for regime detection")
            return self._current_regime, {'error': 'insufficient_data'}

        close = df['close']
        sma_short = sma(close, cfg.sma_short)
        sma_long_series = sma(close, cfg.sma_long)

        latest_close = close.iloc[-1]
        latest_sma10 = sma_short.iloc[-1]
        latest_sma20 = sma_long_series.iloc[-1]

        # Compute declining/rising streaks
        short_declining = sma_declining(sma_short)
        long_declining = sma_declining(sma_long_series)
        both_declining = short_declining & long_declining
        decline_streak = consecutive_true(both_declining, 1)

        short_rising = sma_rising(sma_short)
        long_rising = sma_rising(sma_long_series)
        both_rising = short_rising & long_rising
        rise_streak = consecutive_true(both_rising, 1)

        # Count consecutive declining days
        decline_count = 0
        for i in range(len(both_declining) - 1, -1, -1):
            if both_declining.iloc[i]:
                decline_count += 1
            else:
                break

        rise_count = 0
        for i in range(len(both_rising) - 1, -1, -1):
            if both_rising.iloc[i]:
                rise_count += 1
            else:
                break

        details = {
            'benchmark': benchmark,
            'close': latest_close,
            'sma10': latest_sma10,
            'sma20': latest_sma20,
            'decline_count': decline_count,
            'rise_count': rise_count,
            'hit_rate': None,
            'cooldown_remaining': self._cooldown_remaining,
        }

        # --- Check cooldown ---
        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            details['cooldown_remaining'] = self._cooldown_remaining
            return self._current_regime, details

        new_regime = self._current_regime

        # --- RISK_OFF check ---
        is_risk_off_trigger = decline_count >= cfg.decline_consecutive_days
        if cfg.require_close_above_sma20:
            is_risk_off_trigger = is_risk_off_trigger and (latest_close < latest_sma20)

        if is_risk_off_trigger and self._current_regime != Regime.RISK_OFF:
            new_regime = Regime.RISK_OFF
            logger.warning(f"RISK_OFF triggered: SMA10+SMA20 declining {decline_count} days")

        # --- RISK_OFF exit check ---
        if self._current_regime == Regime.RISK_OFF:
            is_recovery = rise_count >= cfg.rise_consecutive_days
            if cfg.require_close_above_sma20:
                is_recovery = is_recovery and (latest_close > latest_sma20)
            if is_recovery:
                new_regime = self._determine_trend_or_chop()
                logger.info(f"Exiting RISK_OFF: SMA10+SMA20 rising {rise_count} days -> {new_regime.value}")
            else:
                new_regime = Regime.RISK_OFF

        # --- TREND vs CHOP (when not RISK_OFF) ---
        if new_regime != Regime.RISK_OFF and self._current_regime != Regime.RISK_OFF:
            new_regime = self._determine_trend_or_chop()

        details['hit_rate'] = self._compute_hit_rate()

        # Apply regime change with cooldown
        if new_regime != self._current_regime:
            logger.info(f"Regime change: {self._current_regime.value} -> {new_regime.value}")
            self._current_regime = new_regime
            self._last_regime_change = datetime.utcnow()
            self._cooldown_remaining = cfg.cooldown_days

        return self._current_regime, details

    def _determine_trend_or_chop(self) -> Regime:
        """Use rolling hit rate to determine TREND vs CHOP."""
        hit_rate = self._compute_hit_rate()
        if hit_rate is not None and hit_rate >= self.config.regime.trend_hit_rate_threshold:
            return Regime.TREND
        return Regime.CHOP

    def _compute_hit_rate(self) -> Optional[float]:
        """Compute hit rate from recent closed trades: % that hit +1R before -1R."""
        session = self.db_session_factory()
        try:
            window = self.config.regime.hit_rate_window
            closed_trades = session.query(Trade).filter(
                Trade.status.in_([
                    TradeStatus.CLOSED_STOP.name,
                    TradeStatus.CLOSED_EOD.name,
                    TradeStatus.CLOSED_REGIME.name,
                    TradeStatus.CLOSED_MANUAL.name,
                ]),
                Trade.pnl_r_multiple.isnot(None),
            ).order_by(Trade.exit_time.desc()).limit(window).all()

            if len(closed_trades) < 5:
                return None  # Not enough data

            winners = sum(1 for t in closed_trades if t.pnl_r_multiple and t.pnl_r_multiple >= 1.0)
            return winners / len(closed_trades)
        except Exception as e:
            logger.error(f"Error computing hit rate: {e}")
            return None
        finally:
            session.close()

    def save_state(self, details: dict):
        """Persist current regime state to database."""
        session = self.db_session_factory()
        try:
            state = RegimeState(
                regime=self._current_regime.value,
                benchmark=details.get('benchmark'),
                sma10_value=details.get('sma10'),
                sma20_value=details.get('sma20'),
                sma10_declining_days=details.get('decline_count', 0),
                sma20_declining_days=details.get('decline_count', 0),
                sma10_rising_days=details.get('rise_count', 0),
                sma20_rising_days=details.get('rise_count', 0),
                benchmark_close=details.get('close'),
                hit_rate=details.get('hit_rate'),
                cooldown_remaining=details.get('cooldown_remaining', 0),
            )
            session.add(state)
            session.commit()
        except Exception as e:
            logger.error(f"Error saving regime state: {e}")
            session.rollback()
        finally:
            session.close()
