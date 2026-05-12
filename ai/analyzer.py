"""Deep trade analysis that feeds data to the AI strategist."""

import json
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


def analyze_trades(trades: List[dict]) -> dict:
    """Produce detailed trade analysis for AI consumption.

    Returns a structured dict with patterns, statistics, and observations
    that the AI can reason about.
    """
    if not trades:
        return {"total_trades": 0, "observations": ["No trades to analyze."]}

    winners = [t for t in trades if t['pnl_dollars'] > 0]
    losers = [t for t in trades if t['pnl_dollars'] <= 0]

    result = {
        "total_trades": len(trades),
        "winners": len(winners),
        "losers": len(losers),
        "win_rate": len(winners) / len(trades),
    }

    # --- P&L distribution ---
    pnls = [t['pnl_dollars'] for t in trades]
    result["pnl_stats"] = {
        "total": sum(pnls),
        "mean": sum(pnls) / len(pnls),
        "median": sorted(pnls)[len(pnls) // 2],
        "best": max(pnls),
        "worst": min(pnls),
    }

    if winners:
        win_pnls = [t['pnl_dollars'] for t in winners]
        result["winner_stats"] = {
            "avg_pnl": sum(win_pnls) / len(win_pnls),
            "avg_r": _mean([t.get('pnl_r_multiple', 0) or 0 for t in winners]),
            "avg_hold_hours": _avg_hold(winners),
        }
    if losers:
        loss_pnls = [t['pnl_dollars'] for t in losers]
        result["loser_stats"] = {
            "avg_pnl": sum(loss_pnls) / len(loss_pnls),
            "avg_r": _mean([t.get('pnl_r_multiple', 0) or 0 for t in losers]),
            "avg_hold_hours": _avg_hold(losers),
        }

    # --- By symbol ---
    by_symbol = {}
    for t in trades:
        sym = t['symbol']
        if sym not in by_symbol:
            by_symbol[sym] = {"trades": 0, "wins": 0, "pnl": 0, "r_total": 0}
        by_symbol[sym]["trades"] += 1
        by_symbol[sym]["pnl"] += t['pnl_dollars']
        by_symbol[sym]["r_total"] += t.get('pnl_r_multiple', 0) or 0
        if t['pnl_dollars'] > 0:
            by_symbol[sym]["wins"] += 1

    for sym, info in by_symbol.items():
        info["win_rate"] = info["wins"] / info["trades"] if info["trades"] else 0

    # Sort by P&L
    result["by_symbol"] = dict(sorted(by_symbol.items(), key=lambda x: x[1]["pnl"], reverse=True))

    # Identify consistently losing symbols
    result["weak_symbols"] = [
        sym for sym, info in by_symbol.items()
        if info["trades"] >= 2 and info["win_rate"] < 0.3
    ]
    result["strong_symbols"] = [
        sym for sym, info in by_symbol.items()
        if info["trades"] >= 2 and info["win_rate"] > 0.7
    ]

    # --- By exit reason ---
    by_reason = {}
    for t in trades:
        reason = t.get('exit_reason', 'UNKNOWN') or 'UNKNOWN'
        if reason not in by_reason:
            by_reason[reason] = {"trades": 0, "pnl": 0, "wins": 0}
        by_reason[reason]["trades"] += 1
        by_reason[reason]["pnl"] += t['pnl_dollars']
        if t['pnl_dollars'] > 0:
            by_reason[reason]["wins"] += 1
    result["by_exit_reason"] = by_reason

    # --- Hold time analysis ---
    hold_hours_win = []
    hold_hours_loss = []
    for t in trades:
        h = _hold_hours(t)
        if h is None:
            continue
        if t['pnl_dollars'] > 0:
            hold_hours_win.append(h)
        else:
            hold_hours_loss.append(h)

    result["hold_time"] = {
        "avg_winner_hours": _mean(hold_hours_win) if hold_hours_win else 0,
        "avg_loser_hours": _mean(hold_hours_loss) if hold_hours_loss else 0,
    }

    # --- Streak analysis ---
    streaks = _compute_streaks(trades)
    result["streaks"] = streaks

    # --- Recent performance trend ---
    if len(trades) >= 10:
        recent = trades[:len(trades) // 2]  # first half (most recent, trades are desc)
        older = trades[len(trades) // 2:]
        recent_wr = sum(1 for t in recent if t['pnl_dollars'] > 0) / len(recent)
        older_wr = sum(1 for t in older if t['pnl_dollars'] > 0) / len(older)
        result["trend"] = {
            "recent_win_rate": recent_wr,
            "older_win_rate": older_wr,
            "improving": recent_wr > older_wr,
        }

    # --- Generate observations ---
    observations = _generate_observations(result)
    result["observations"] = observations

    return result


def _mean(vals):
    return sum(vals) / len(vals) if vals else 0


def _hold_hours(t):
    entry = t.get('entry_time')
    exit_ = t.get('exit_time')
    if entry and exit_:
        return (exit_ - entry).total_seconds() / 3600
    return None


def _avg_hold(trades):
    hours = [_hold_hours(t) for t in trades]
    hours = [h for h in hours if h is not None]
    return _mean(hours)


def _compute_streaks(trades):
    max_win = max_loss = cur_win = cur_loss = 0
    for t in reversed(trades):
        if t['pnl_dollars'] > 0:
            cur_win += 1
            cur_loss = 0
        else:
            cur_loss += 1
            cur_win = 0
        max_win = max(max_win, cur_win)
        max_loss = max(max_loss, cur_loss)
    return {"max_win_streak": max_win, "max_loss_streak": max_loss}


def _generate_observations(analysis: dict) -> List[str]:
    """Generate human-readable observations from trade analysis."""
    obs = []

    wr = analysis.get("win_rate", 0)
    if wr < 0.4:
        obs.append(f"Low win rate ({wr:.1%}). Consider tightening entry criteria.")
    elif wr > 0.65:
        obs.append(f"High win rate ({wr:.1%}). Could potentially increase position sizing.")

    if "winner_stats" in analysis and "loser_stats" in analysis:
        avg_win = analysis["winner_stats"]["avg_pnl"]
        avg_loss = abs(analysis["loser_stats"]["avg_pnl"])
        if avg_loss > 0:
            rr = avg_win / avg_loss
            if rr < 1.0:
                obs.append(f"Risk/reward ratio is poor ({rr:.2f}:1). Winners are smaller than losers.")
            elif rr > 2.0:
                obs.append(f"Strong risk/reward ({rr:.2f}:1). Winners significantly larger than losers.")

    if "hold_time" in analysis:
        ht = analysis["hold_time"]
        if ht["avg_loser_hours"] > 0 and ht["avg_winner_hours"] > 0:
            if ht["avg_loser_hours"] > ht["avg_winner_hours"] * 1.5:
                obs.append("Losers are held much longer than winners. Consider tightening max hold days.")

    if analysis.get("weak_symbols"):
        obs.append(f"Consistently weak symbols: {', '.join(analysis['weak_symbols'])}. Consider removing.")

    if analysis.get("strong_symbols"):
        obs.append(f"Consistently strong symbols: {', '.join(analysis['strong_symbols'])}.")

    by_reason = analysis.get("by_exit_reason", {})
    for reason, info in by_reason.items():
        if reason == "MAX_HOLD" and info["trades"] >= 3 and info["pnl"] < 0:
            obs.append(f"MAX_HOLD exits are net negative (${info['pnl']:,.2f}). "
                       "These trades ran out of time - consider adjusting hold period or exit rules.")

    if "trend" in analysis:
        if analysis["trend"]["improving"]:
            obs.append("Recent performance is improving compared to older trades.")
        else:
            obs.append("Recent performance is declining. Strategy may need adjustment.")

    streaks = analysis.get("streaks", {})
    if streaks.get("max_loss_streak", 0) >= 5:
        obs.append(f"Had a {streaks['max_loss_streak']}-trade losing streak. "
                   "Consider adding a cooldown or regime filter.")

    return obs


def build_ai_prompt_data(trades: List[dict], daily_pnl: List[dict],
                         current_spec: dict, metrics: dict) -> dict:
    """Build the complete data package for AI analysis."""
    trade_analysis = analyze_trades(trades)

    return {
        "trade_analysis": trade_analysis,
        "performance_metrics": metrics,
        "current_strategy": {
            "name": current_spec.get("name", ""),
            "entry_rule_long": current_spec.get("entry_rule_long", ""),
            "exit_rule": current_spec.get("exit_rule", ""),
            "position_size_pct": current_spec.get("position_size_pct", 0),
            "max_positions": current_spec.get("max_positions", 0),
            "max_holding_days": current_spec.get("max_holding_days", 0),
            "stop_loss_pct": current_spec.get("stop_loss_pct", 0),
            "symbols": current_spec.get("symbols", []),
            "indicators": current_spec.get("indicators", []),
        },
        "daily_pnl_recent": daily_pnl[-30:] if daily_pnl else [],
    }
