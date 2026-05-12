"""Position sizing with risk-per-trade and safety constraints."""

import logging
import math
from typing import Optional, Tuple

from core.constants import Regime
from core.config import Config
from data.models import Trade, TradeStatus

logger = logging.getLogger(__name__)


class PositionSizer:
    """Calculates position size based on risk parameters and constraints."""

    def __init__(self, config: Config, db_session_factory):
        self.config = config
        self.db_session_factory = db_session_factory

    def calculate(
        self,
        entry_price: float,
        stop_price: float,
        equity: float,
        regime: Regime,
        risk_per_trade_pct: Optional[float] = None,
    ) -> Tuple[int, dict]:
        """Calculate position size in shares.

        Returns (shares, details_dict). Returns (0, details) if sizing fails.
        """
        cfg = self.config.sizing

        if risk_per_trade_pct is None:
            risk_per_trade_pct = cfg.risk_per_trade_pct

        # Validate bounds
        risk_per_trade_pct = max(cfg.risk_min_pct, min(cfg.risk_max_pct, risk_per_trade_pct))

        # Regime size multiplier
        if regime == Regime.CHOP:
            size_mult = self.config.regime.chop_size_multiplier
        elif regime == Regime.RISK_OFF:
            return 0, {'reason': 'RISK_OFF regime, no new positions'}
        else:
            size_mult = 1.0

        # Per-share risk
        per_share_risk = entry_price - stop_price
        if per_share_risk <= 0:
            return 0, {'reason': f'Invalid risk: entry={entry_price:.2f} stop={stop_price:.2f}'}

        # Risk dollars
        risk_dollars = equity * (risk_per_trade_pct / 100) * size_mult

        # Shares
        shares = math.floor(risk_dollars / per_share_risk)
        if shares <= 0:
            return 0, {'reason': f'Shares=0: risk_dollars={risk_dollars:.2f} per_share_risk={per_share_risk:.2f}'}

        details = {
            'risk_per_trade_pct': risk_per_trade_pct,
            'size_multiplier': size_mult,
            'risk_dollars': risk_dollars,
            'per_share_risk': per_share_risk,
            'raw_shares': shares,
            'entry_price': entry_price,
            'stop_price': stop_price,
            'equity': equity,
        }

        # --- Constraint checks ---

        # Max position % of equity
        position_value = shares * entry_price
        max_position_value = equity * (cfg.max_position_pct_equity / 100)
        if position_value > max_position_value:
            shares = math.floor(max_position_value / entry_price)
            details['constrained_by'] = 'max_position_pct_equity'

        # Max positions check
        max_positions = cfg.max_positions_trend if regime == Regime.TREND else cfg.max_positions_chop
        current_positions = self._count_open_positions()
        if current_positions >= max_positions:
            return 0, {**details, 'reason': f'Max positions reached: {current_positions}/{max_positions}'}

        # Max total open risk (in R)
        current_risk_r = self._total_open_risk_r()
        new_risk_r = (shares * per_share_risk) / (equity * (risk_per_trade_pct / 100)) if risk_dollars > 0 else 0
        if current_risk_r + new_risk_r > cfg.max_total_open_risk_r:
            # Reduce shares to fit within limit
            remaining_r = max(0, cfg.max_total_open_risk_r - current_risk_r)
            max_risk_dollars = remaining_r * (equity * (risk_per_trade_pct / 100))
            shares = math.floor(max_risk_dollars / per_share_risk)
            details['constrained_by'] = 'max_total_open_risk_r'
            if shares <= 0:
                return 0, {**details, 'reason': f'Max total risk reached: {current_risk_r:.1f}R / {cfg.max_total_open_risk_r}R'}

        details['final_shares'] = shares
        details['position_value'] = shares * entry_price
        details['risk_r'] = (shares * per_share_risk) / risk_dollars if risk_dollars > 0 else 0

        return shares, details

    def _count_open_positions(self) -> int:
        """Count currently open positions."""
        session = self.db_session_factory()
        try:
            count = session.query(Trade).filter(
                Trade.status.in_([TradeStatus.OPEN.name, TradeStatus.BREAKEVEN.name])
            ).count()
            return count
        except Exception:
            return 0
        finally:
            session.close()

    def _total_open_risk_r(self) -> float:
        """Calculate total open risk in R-multiples."""
        session = self.db_session_factory()
        try:
            trades = session.query(Trade).filter(
                Trade.status.in_([TradeStatus.OPEN.name, TradeStatus.BREAKEVEN.name])
            ).all()
            total_r = 0.0
            for t in trades:
                if t.r_value and t.r_value > 0 and t.shares:
                    trade_risk = t.shares * t.r_value
                    if t.r_value > 0:
                        total_r += trade_risk / (t.r_value * t.shares) if t.shares > 0 else 0
                    else:
                        total_r += 1.0  # Assume 1R if unknown
            return total_r
        except Exception:
            return 0.0
        finally:
            session.close()
