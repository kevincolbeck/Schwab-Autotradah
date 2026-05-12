"""Compute backtest performance statistics."""

import math

import numpy as np
import pandas as pd

from backtest.config_overrides import BacktestConfig
from backtest.portfolio import Portfolio


def compute_statistics(portfolio: Portfolio, bt_config: BacktestConfig) -> dict:
    """Compute comprehensive backtest statistics."""
    trades = portfolio.closed_trades
    equity_curve = portfolio.equity_curve

    if not equity_curve:
        return {"error": "No equity data"}

    total_trades = len(trades)
    if total_trades == 0:
        return {
            "total_trades": 0,
            "equity_curve": equity_curve,
            "final_equity": portfolio.cash,
            "starting_capital": bt_config.starting_capital,
        }

    winners = [t for t in trades if t["pnl_dollars"] > 0]
    losers = [t for t in trades if t["pnl_dollars"] <= 0]

    win_rate = len(winners) / total_trades
    avg_win = np.mean([t["pnl_r"] for t in winners]) if winners else 0
    avg_loss = np.mean([t["pnl_r"] for t in losers]) if losers else 0
    avg_r = np.mean([t["pnl_r"] for t in trades])

    gross_profit = sum(t["pnl_dollars"] for t in winners) if winners else 0
    gross_loss = abs(sum(t["pnl_dollars"] for t in losers)) if losers else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    expectancy_r = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)

    # Equity curve stats
    equities = pd.Series([ep.equity for ep in equity_curve])
    dates = pd.Series([ep.date for ep in equity_curve])

    final_equity = equities.iloc[-1]
    starting_capital = bt_config.starting_capital
    total_return_pct = (final_equity - starting_capital) / starting_capital * 100

    years = (dates.iloc[-1] - dates.iloc[0]).days / 365.25
    if years > 0 and final_equity > 0:
        cagr = (final_equity / starting_capital) ** (1 / years) - 1
    else:
        cagr = 0

    drawdowns = pd.Series([ep.drawdown_pct for ep in equity_curve])
    max_drawdown = drawdowns.max()

    daily_returns = equities.pct_change().dropna()
    if len(daily_returns) > 1 and daily_returns.std() > 0:
        sharpe = (daily_returns.mean() / daily_returns.std()) * math.sqrt(252)
    else:
        sharpe = 0

    downside_returns = daily_returns[daily_returns < 0]
    if len(downside_returns) > 0 and downside_returns.std() > 0:
        sortino = (daily_returns.mean() / downside_returns.std()) * math.sqrt(252)
    else:
        sortino = 0

    calmar = (cagr / (max_drawdown / 100)) if max_drawdown > 0 else 0

    r_results = [t["pnl_r"] for t in trades]
    max_consec_losses = _max_consecutive(r_results, lambda x: x <= 0)
    max_consec_wins = _max_consecutive(r_results, lambda x: x > 0)

    # By regime
    regime_stats = {}
    for regime in ["TREND", "CHOP", "RISK_OFF"]:
        regime_trades = [t for t in trades if t["regime_at_entry"] == regime]
        if regime_trades:
            regime_winners = [t for t in regime_trades if t["pnl_dollars"] > 0]
            regime_stats[regime] = {
                "count": len(regime_trades),
                "win_rate": len(regime_winners) / len(regime_trades),
                "avg_r": float(np.mean([t["pnl_r"] for t in regime_trades])),
                "total_pnl": sum(t["pnl_dollars"] for t in regime_trades),
            }

    # By exit reason
    exit_stats = {}
    for t in trades:
        reason = t["exit_reason"]
        if reason not in exit_stats:
            exit_stats[reason] = {"count": 0, "total_r": 0, "total_pnl": 0}
        exit_stats[reason]["count"] += 1
        exit_stats[reason]["total_r"] += t["pnl_r"]
        exit_stats[reason]["total_pnl"] += t["pnl_dollars"]

    holding_days = [(t["exit_date"] - t["entry_date"]).days for t in trades]
    avg_hold = float(np.mean(holding_days)) if holding_days else 0

    return {
        "starting_capital": starting_capital,
        "final_equity": float(final_equity),
        "total_return_pct": total_return_pct,
        "cagr": cagr * 100,
        "max_drawdown": float(max_drawdown),
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "total_trades": total_trades,
        "win_rate": win_rate * 100,
        "avg_win_r": float(avg_win),
        "avg_loss_r": float(avg_loss),
        "avg_r": float(avg_r),
        "profit_factor": profit_factor,
        "expectancy_r": expectancy_r,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "max_consec_wins": max_consec_wins,
        "max_consec_losses": max_consec_losses,
        "avg_holding_days": avg_hold,
        "regime_stats": regime_stats,
        "exit_stats": exit_stats,
        "equity_curve": equity_curve,
        "trades": trades,
    }


def _max_consecutive(values: list, condition) -> int:
    max_count = 0
    current = 0
    for v in values:
        if condition(v):
            current += 1
            max_count = max(max_count, current)
        else:
            current = 0
    return max_count
