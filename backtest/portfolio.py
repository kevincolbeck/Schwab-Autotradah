"""Track portfolio state: cash, positions, equity curve."""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class BacktestPosition:
    """An open position in the backtester."""
    symbol: str
    shares: int
    entry_price: float
    entry_date: datetime
    initial_stop: float
    current_stop: float
    r_value: float
    side: str = "LONG"  # LONG | SHORT
    breakeven_set: bool = False
    regime_at_entry: str = "TREND"
    signal_type: str = "BREAKOUT"
    triggered_tp_rules: List[int] = field(default_factory=list)


@dataclass
class EquityPoint:
    """A single point on the equity curve."""
    date: datetime
    equity: float
    cash: float
    positions_value: float
    drawdown_pct: float
    regime: str


class Portfolio:
    """Manages cash, open positions, and equity history."""

    def __init__(self, starting_capital: float):
        self.starting_capital = starting_capital
        self.cash = starting_capital
        self.positions: Dict[str, BacktestPosition] = {}
        self.equity_curve: List[EquityPoint] = []
        self._peak_equity = starting_capital
        self._closed_trades: List[dict] = []

    @property
    def open_position_count(self) -> int:
        return len(self.positions)

    def total_equity(self, current_prices: Dict[str, float]) -> float:
        positions_value = 0.0
        for pos in self.positions.values():
            current_price = current_prices.get(pos.symbol, pos.entry_price)
            if pos.side == "SHORT":
                positions_value -= pos.shares * current_price
            else:
                positions_value += pos.shares * current_price
        return self.cash + positions_value

    def open_position(self, pos: BacktestPosition):
        position_value = pos.shares * pos.entry_price
        if pos.side == "SHORT":
            # Short sale proceeds are credited to cash; MTM liability is reflected in equity.
            self.cash += position_value
        else:
            self.cash -= position_value
        self.positions[pos.symbol] = pos

    def close_position(
        self, symbol: str, exit_price: float, exit_date: datetime, exit_reason: str,
    ) -> Optional[dict]:
        if symbol not in self.positions:
            return None
        pos = self.positions.pop(symbol)
        close_value = pos.shares * exit_price
        if pos.side == "SHORT":
            # Buying to cover reduces cash.
            self.cash -= close_value
            pnl_dollars = (pos.entry_price - exit_price) * pos.shares
        else:
            self.cash += close_value
            pnl_dollars = (exit_price - pos.entry_price) * pos.shares

        risk_dollars = pos.r_value * pos.shares
        pnl_r = pnl_dollars / risk_dollars if risk_dollars > 0 else 0

        result = {
            "symbol": symbol,
            "side": pos.side,
            "entry_date": pos.entry_date,
            "exit_date": exit_date,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "shares": pos.shares,
            "pnl_dollars": pnl_dollars,
            "pnl_r": pnl_r,
            "r_value": pos.r_value,
            "initial_stop": pos.initial_stop,
            "exit_reason": exit_reason,
            "regime_at_entry": pos.regime_at_entry,
            "signal_type": pos.signal_type,
            "breakeven_was_set": pos.breakeven_set,
        }
        self._closed_trades.append(result)
        return result

    def reduce_position(
        self, symbol: str, sell_pct: float, exit_price: float,
        exit_date: datetime, exit_reason: str,
    ) -> Optional[dict]:
        """Sell a percentage of an open position (partial exit).

        Returns a closed-trade record for the sold portion. The remaining
        position stays open with reduced share count.
        """
        if symbol not in self.positions:
            return None
        pos = self.positions[symbol]
        sell_shares = max(1, int(pos.shares * sell_pct / 100.0))
        if sell_shares >= pos.shares:
            return self.close_position(symbol, exit_price, exit_date, exit_reason)

        close_value = sell_shares * exit_price
        if pos.side == "SHORT":
            self.cash -= close_value
            pnl_dollars = (pos.entry_price - exit_price) * sell_shares
        else:
            self.cash += close_value
            pnl_dollars = (exit_price - pos.entry_price) * sell_shares

        risk_dollars = pos.r_value * sell_shares
        pnl_r = pnl_dollars / risk_dollars if risk_dollars > 0 else 0

        result = {
            "symbol": symbol,
            "side": pos.side,
            "entry_date": pos.entry_date,
            "exit_date": exit_date,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "shares": sell_shares,
            "pnl_dollars": pnl_dollars,
            "pnl_r": pnl_r,
            "r_value": pos.r_value,
            "initial_stop": pos.initial_stop,
            "exit_reason": exit_reason,
            "regime_at_entry": pos.regime_at_entry,
            "signal_type": pos.signal_type,
            "breakeven_was_set": pos.breakeven_set,
        }
        self._closed_trades.append(result)
        pos.shares -= sell_shares
        return result

    def record_equity(self, date: datetime, current_prices: Dict[str, float], regime: str):
        eq = self.total_equity(current_prices)
        self._peak_equity = max(self._peak_equity, eq)
        dd = (self._peak_equity - eq) / self._peak_equity * 100 if self._peak_equity > 0 else 0
        pos_value = 0.0
        for p in self.positions.values():
            px = current_prices.get(p.symbol, p.entry_price)
            pos_value += (-p.shares * px) if p.side == "SHORT" else (p.shares * px)
        self.equity_curve.append(EquityPoint(
            date=date, equity=eq, cash=self.cash,
            positions_value=pos_value, drawdown_pct=dd, regime=regime,
        ))

    def total_open_risk_r(self) -> float:
        return float(len(self.positions))

    @property
    def closed_trades(self) -> List[dict]:
        return self._closed_trades
