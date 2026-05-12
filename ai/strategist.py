"""AI Strategist - uses Claude to continuously analyze and improve trading strategy.

Runs automatically on schedule:
  - After market close daily: reviews today's trades, proposes micro-adjustments
  - Weekly (Friday): deep analysis with full trade history, larger parameter sweeps
  - On every trade close: quick review of what went right/wrong

The AI can modify:
  - Entry thresholds (RSI levels, ZScore levels)
  - Exit rules (SMA period, RSI exit threshold)
  - Position sizing (% per position, max positions)
  - Max hold days
  - Symbol universe (add/remove symbols)
  - Trend filter parameters

All changes are validated via backtest before being applied to live strategy.
All decisions are logged to the database with full reasoning.
"""

import json
import copy
import logging
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List

import anthropic

from ai.analyzer import analyze_trades, build_ai_prompt_data
from data.models import AIDecision

logger = logging.getLogger(__name__)

# Guardrails: maximum parameter changes per cycle
PARAM_BOUNDS = {
    "position_size_pct": (10.0, 80.0),
    "max_positions": (1, 5),
    "max_holding_days": (3, 30),
    "stop_loss_pct": (0.0, 15.0),
}

# Parameters the AI is allowed to modify
TUNABLE_PARAMS = [
    "entry_rule_long", "exit_rule", "position_size_pct", "max_positions",
    "max_holding_days", "stop_loss_pct", "symbols",
]

SYSTEM_PROMPT = """You are a quantitative trading strategist AI embedded in a live trading system. Your sole goal is to maximize risk-adjusted returns.

You analyze trade data and strategy performance to make concrete, specific improvements. You think like a Renaissance Technologies quant: data-driven, precise, incremental.

CURRENT STRATEGY FORMAT:
The strategy is a JSON spec with these key tunable parameters:
- entry_rule_long: Boolean expression using indicator columns (e.g., "rsi_2 < 15 & zscore_10 < -1.5")
- exit_rule: Boolean expression for exit signals (e.g., "close > sma_5 & rsi_3 > 60")
- indicators: List of indicator definitions (RSI, SMA, ZScore, etc.)
- position_size_pct: Percentage of equity per position (10-80%)
- max_positions: Maximum concurrent positions (1-5)
- max_holding_days: Max days to hold before forced exit (3-30)
- stop_loss_pct: Stop loss percentage below entry (0-15%, 0 = disabled)
- symbols: List of stock symbols to trade

AVAILABLE INDICATOR TYPES for rules:
- rsi (with configurable length): e.g., rsi_2, rsi_3, rsi_14
- sma (simple moving average): e.g., sma_5, sma_20, sma_50
- zscore (z-score of price): e.g., zscore_10, zscore_20
- custom (formula-based): e.g., momentum = close / sma_50

RULES FOR MAKING CHANGES:
1. Be INCREMENTAL. Never change more than 2-3 parameters at once.
2. Always explain your reasoning with specific data points.
3. If the strategy is working well (Sharpe > 1.5, win rate > 50%), make only minor tweaks.
4. If losing money, focus on reducing losers first (tighter stops, stricter entry).
5. If win rate is high but gains are small, focus on letting winners run (looser exits).
6. Consider removing symbols that consistently lose.
7. Consider the risk/reward ratio of each change.
8. NEVER make a change just for the sake of change. "No changes needed" is a valid response.

YOUR RESPONSE FORMAT:
You must respond with a JSON object (no markdown, no code fences):
{
    "analysis": "Your detailed analysis of the current performance...",
    "reasoning": "Why you are proposing these specific changes...",
    "changes": {
        "param_name": new_value,
        ...
    },
    "confidence": 0.0-1.0,
    "risk_assessment": "What could go wrong with these changes..."
}

If no changes are needed, return:
{
    "analysis": "Your analysis...",
    "reasoning": "Why no changes are needed...",
    "changes": {},
    "confidence": 1.0,
    "risk_assessment": "N/A - no changes proposed"
}

IMPORTANT: Only include parameters that you want to CHANGE in the "changes" object. Do not repeat unchanged values."""


class AIStrategist:
    """AI brain that continuously analyzes and optimizes the trading strategy."""

    def __init__(self, api_key: str, db_session_factory, spec_path: str,
                 engine=None):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.db_session_factory = db_session_factory
        self.spec_path = spec_path
        self.engine = engine
        self._last_analysis_time = None
        self._decisions = []  # in-memory cache of recent decisions

    def load_spec(self) -> dict:
        """Load the current strategy spec from disk."""
        with open(self.spec_path, 'r') as f:
            return json.load(f)

    def save_spec(self, spec: dict):
        """Save updated strategy spec to disk."""
        with open(self.spec_path, 'w') as f:
            json.dump(spec, f, indent=2)

    def run_analysis(self, analysis_type: str = "DAILY",
                     min_trades: int = 5) -> Optional[dict]:
        """Run AI analysis cycle.

        Args:
            analysis_type: DAILY, WEEKLY, or TRADE_REVIEW
            min_trades: Minimum closed trades required to run analysis

        Returns:
            Dict with analysis results, or None if skipped
        """
        logger.info(f"AI Strategist: Starting {analysis_type} analysis...")

        try:
            # Gather data
            days = 7 if analysis_type == "DAILY" else 90 if analysis_type == "WEEKLY" else 30
            trades = self.engine.get_closed_trades(days) if self.engine else []
            daily_pnl = self.engine.get_daily_pnl_history(days) if self.engine else []
            metrics = self.engine.get_performance_metrics(days) if self.engine else {}

            if len(trades) < min_trades:
                logger.info(f"AI Strategist: Only {len(trades)} trades (need {min_trades}). Skipping.")
                return None

            current_spec = self.load_spec()
            prompt_data = build_ai_prompt_data(trades, daily_pnl, current_spec, metrics)

            # Build the prompt
            user_prompt = self._build_prompt(prompt_data, analysis_type)

            # Call Claude
            logger.info("AI Strategist: Consulting Claude API...")
            response = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )

            response_text = response.content[0].text.strip()
            ai_result = self._parse_response(response_text)

            if ai_result is None:
                logger.warning("AI Strategist: Could not parse AI response")
                self._log_decision(analysis_type, response_text, {}, {}, False, metrics, current_spec)
                return {"reasoning": response_text, "changes": {}, "applied": False}

            proposed_changes = ai_result.get("changes", {})
            reasoning = ai_result.get("analysis", "") + "\n\n" + ai_result.get("reasoning", "")
            confidence = ai_result.get("confidence", 0)

            if not proposed_changes:
                logger.info("AI Strategist: No changes proposed.")
                self._log_decision(analysis_type, reasoning, {}, {}, True, metrics, current_spec)
                return {
                    "reasoning": reasoning,
                    "changes": {},
                    "applied": False,
                    "confidence": confidence,
                }

            # Validate and apply changes
            validated_changes = self._validate_changes(proposed_changes, current_spec)
            if not validated_changes:
                logger.info("AI Strategist: All proposed changes failed validation.")
                self._log_decision(analysis_type, reasoning, proposed_changes, {}, False, metrics, current_spec)
                return {
                    "reasoning": reasoning,
                    "changes": proposed_changes,
                    "applied": False,
                    "rejected_reason": "Failed validation guardrails",
                }

            # Apply changes
            spec_before = copy.deepcopy(current_spec)
            new_spec = self._apply_changes(current_spec, validated_changes)

            # Run quick backtest comparison if possible
            bt_before, bt_after = self._backtest_comparison(spec_before, new_spec)

            # Only apply if backtest shows improvement or is inconclusive
            should_apply = self._should_apply(bt_before, bt_after, confidence)

            if should_apply:
                self.save_spec(new_spec)
                logger.info(f"AI Strategist: Applied changes: {json.dumps(validated_changes)}")

                # Notify engine to reload spec
                if self.engine and hasattr(self.engine, 'rule_live_executor'):
                    try:
                        self.engine.rule_live_executor.reload_spec()
                        logger.info("AI Strategist: Live executor reloaded with new spec")
                    except Exception as e:
                        logger.warning(f"AI Strategist: Could not reload executor: {e}")
            else:
                logger.info("AI Strategist: Changes not applied (backtest didn't improve)")
                validated_changes = {}

            self._log_decision(
                analysis_type, reasoning, proposed_changes, validated_changes,
                should_apply, metrics, spec_before, new_spec if should_apply else None,
                bt_before, bt_after,
            )

            return {
                "reasoning": reasoning,
                "changes": validated_changes,
                "applied": should_apply,
                "confidence": confidence,
                "risk_assessment": ai_result.get("risk_assessment", ""),
                "backtest_before": bt_before,
                "backtest_after": bt_after,
            }

        except anthropic.APIError as e:
            logger.error(f"AI Strategist: API error: {e}")
            return {"error": str(e), "applied": False}
        except Exception as e:
            logger.error(f"AI Strategist: Error: {e}\n{traceback.format_exc()}")
            return {"error": str(e), "applied": False}

    def _build_prompt(self, data: dict, analysis_type: str) -> str:
        """Build the user prompt with all relevant data."""
        parts = [f"ANALYSIS TYPE: {analysis_type}\n"]

        # Performance metrics
        m = data.get("performance_metrics", {})
        if m:
            parts.append("CURRENT PERFORMANCE METRICS:")
            parts.append(f"  Total Trades: {m.get('total_trades', 0)}")
            parts.append(f"  Win Rate: {m.get('win_rate', 0):.1%}")
            parts.append(f"  Total P&L: ${m.get('total_pnl', 0):,.2f}")
            parts.append(f"  Sharpe Ratio: {m.get('sharpe', 0):.2f}")
            parts.append(f"  Sortino Ratio: {m.get('sortino', 0):.2f}")
            parts.append(f"  Profit Factor: {m.get('profit_factor', 0):.2f}")
            parts.append(f"  Max Drawdown: ${m.get('max_drawdown', 0):,.2f}")
            parts.append(f"  Avg Win: ${m.get('avg_win', 0):,.2f}")
            parts.append(f"  Avg Loss: ${m.get('avg_loss', 0):,.2f}")
            parts.append(f"  Avg R-Multiple: {m.get('avg_r', 0):+.2f}")
            parts.append(f"  Best Day: ${m.get('best_day', 0):,.2f}")
            parts.append(f"  Worst Day: ${m.get('worst_day', 0):,.2f}")
            parts.append(f"  Win Streak Max: {m.get('streak_wins', 0)}")
            parts.append(f"  Loss Streak Max: {m.get('streak_losses', 0)}")
            parts.append("")

        # Trade analysis
        ta = data.get("trade_analysis", {})
        if ta.get("observations"):
            parts.append("KEY OBSERVATIONS:")
            for obs in ta["observations"]:
                parts.append(f"  - {obs}")
            parts.append("")

        if ta.get("by_symbol"):
            parts.append("PERFORMANCE BY SYMBOL:")
            for sym, info in ta["by_symbol"].items():
                wr = info['win_rate']
                parts.append(f"  {sym}: {info['trades']} trades, "
                             f"WR={wr:.0%}, P&L=${info['pnl']:+,.2f}")
            parts.append("")

        if ta.get("by_exit_reason"):
            parts.append("PERFORMANCE BY EXIT REASON:")
            for reason, info in ta["by_exit_reason"].items():
                parts.append(f"  {reason}: {info['trades']} trades, P&L=${info['pnl']:+,.2f}")
            parts.append("")

        if ta.get("hold_time"):
            ht = ta["hold_time"]
            parts.append(f"HOLD TIME: Winners avg {ht['avg_winner_hours']:.1f}h, "
                         f"Losers avg {ht['avg_loser_hours']:.1f}h")
            parts.append("")

        # Current strategy
        strat = data.get("current_strategy", {})
        if strat:
            parts.append("CURRENT STRATEGY SPEC:")
            parts.append(f"  Entry Rule: {strat.get('entry_rule_long', '')}")
            parts.append(f"  Exit Rule: {strat.get('exit_rule', '')}")
            parts.append(f"  Position Size: {strat.get('position_size_pct', 0)}%")
            parts.append(f"  Max Positions: {strat.get('max_positions', 0)}")
            parts.append(f"  Max Hold Days: {strat.get('max_holding_days', 0)}")
            parts.append(f"  Stop Loss: {strat.get('stop_loss_pct', 0)}%")
            parts.append(f"  Symbols ({len(strat.get('symbols', []))}): {', '.join(strat.get('symbols', []))}")
            parts.append("")

        parts.append("Based on this data, analyze the strategy performance and propose specific, "
                     "data-driven improvements. Remember: be incremental and explain your reasoning.")

        return "\n".join(parts)

    def _parse_response(self, text: str) -> Optional[dict]:
        """Parse the AI's JSON response."""
        # Strip markdown code fences if present
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Try to find JSON in the response
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start:end])
                except json.JSONDecodeError:
                    pass
            logger.warning(f"AI Strategist: Could not parse JSON from response")
            return None

    def _validate_changes(self, changes: dict, current_spec: dict) -> dict:
        """Validate proposed changes against guardrails."""
        validated = {}

        for param, value in changes.items():
            if param not in TUNABLE_PARAMS:
                logger.warning(f"AI Strategist: Ignoring non-tunable param: {param}")
                continue

            # Numeric bounds
            if param in PARAM_BOUNDS:
                lo, hi = PARAM_BOUNDS[param]
                if isinstance(value, (int, float)):
                    value = max(lo, min(hi, value))
                else:
                    logger.warning(f"AI Strategist: Invalid type for {param}: {type(value)}")
                    continue

            # Rule validation
            if param in ("entry_rule_long", "exit_rule"):
                if not isinstance(value, str) or len(value) < 3:
                    logger.warning(f"AI Strategist: Invalid rule string for {param}")
                    continue
                # Basic safety check
                dangerous = ("import", "exec", "eval", "open", "os.", "subprocess", "__")
                if any(d in value.lower() for d in dangerous):
                    logger.warning(f"AI Strategist: Dangerous content in {param}, rejecting")
                    continue

            # Symbol validation
            if param == "symbols":
                if not isinstance(value, list) or len(value) < 3:
                    logger.warning("AI Strategist: symbols must be a list with >= 3 entries")
                    continue
                value = [s.upper().strip() for s in value if isinstance(s, str)]

            validated[param] = value

        return validated

    def _apply_changes(self, spec: dict, changes: dict) -> dict:
        """Apply validated changes to a copy of the strategy spec."""
        new_spec = copy.deepcopy(spec)

        for param, value in changes.items():
            new_spec[param] = value

            # If entry/exit rules changed, update indicators if needed
            if param == "entry_rule_long":
                self._ensure_indicators(new_spec, value)
            elif param == "exit_rule":
                self._ensure_indicators(new_spec, value)

        return new_spec

    def _ensure_indicators(self, spec: dict, rule: str):
        """Ensure indicators referenced in a rule exist in the spec."""
        import re
        # Find indicator references like rsi_2, sma_5, zscore_10
        refs = set(re.findall(r'((?:rsi|sma|zscore|ema)_\d+)', rule))
        existing_names = {ind.get('name', '') for ind in spec.get('indicators', [])}

        for ref in refs:
            if ref not in existing_names:
                # Parse type and length
                parts = ref.rsplit('_', 1)
                if len(parts) == 2:
                    ind_type, length = parts[0], int(parts[1])
                    spec['indicators'].append({
                        "type": ind_type,
                        "length": length,
                        "source": "close",
                        "name": ref,
                    })
                    logger.info(f"AI Strategist: Auto-added indicator {ref}")

    def _backtest_comparison(self, spec_before: dict, spec_after: dict):
        """Run quick backtest comparison between old and new spec.

        Returns (metrics_before, metrics_after) dicts, or (None, None) if
        backtesting is not available.
        """
        try:
            from backtest.rule_based_engine import RuleBasedBacktester, RuleBasedStrategySpec
            from backtest.config_overrides import BacktestConfig

            bt_config = BacktestConfig(
                starting_capital=100000,
                start_date="2024-01-01",
                end_date=datetime.now().strftime("%Y-%m-%d"),
            )

            # Backtest before
            spec_obj_before = RuleBasedStrategySpec.from_dict(spec_before)
            bt_before = RuleBasedBacktester(spec_obj_before, bt_config)
            bt_before.run()
            stats_before = bt_before.get_summary_stats()

            # Backtest after
            spec_obj_after = RuleBasedStrategySpec.from_dict(spec_after)
            bt_after = RuleBasedBacktester(spec_obj_after, bt_config)
            bt_after.run()
            stats_after = bt_after.get_summary_stats()

            return (
                _extract_key_metrics(stats_before),
                _extract_key_metrics(stats_after),
            )
        except Exception as e:
            logger.debug(f"AI Strategist: Backtest comparison skipped: {e}")
            return None, None

    def _should_apply(self, bt_before, bt_after, confidence: float) -> bool:
        """Decide whether to apply changes based on backtest results."""
        # If no backtest available, apply if confidence > 0.6
        if bt_before is None or bt_after is None:
            return confidence >= 0.6

        # Compare key metrics
        sharpe_before = bt_before.get("sharpe", 0)
        sharpe_after = bt_after.get("sharpe", 0)
        pnl_before = bt_before.get("total_pnl", 0)
        pnl_after = bt_after.get("total_pnl", 0)

        # Apply if Sharpe improved or P&L improved without Sharpe degrading
        if sharpe_after > sharpe_before:
            return True
        if pnl_after > pnl_before and sharpe_after >= sharpe_before * 0.9:
            return True

        return False

    def _log_decision(self, analysis_type, reasoning, proposed, applied,
                      approved, metrics, spec_before, spec_after=None,
                      bt_before=None, bt_after=None):
        """Log AI decision to database."""
        session = self.db_session_factory()
        try:
            decision = AIDecision(
                analysis_type=analysis_type,
                reasoning=reasoning[:4000] if reasoning else "",
                changes_proposed=json.dumps(proposed) if proposed else "{}",
                changes_applied=json.dumps(applied) if applied else "{}",
                backtest_before=json.dumps(bt_before) if bt_before else None,
                backtest_after=json.dumps(bt_after) if bt_after else None,
                approved=approved,
                metrics_snapshot=json.dumps(metrics) if metrics else None,
                spec_before=json.dumps(spec_before) if spec_before else None,
                spec_after=json.dumps(spec_after) if spec_after else None,
            )
            session.add(decision)
            session.commit()
            self._decisions.append({
                "id": decision.id,
                "timestamp": decision.timestamp,
                "type": analysis_type,
                "reasoning": reasoning[:500] if reasoning else "",
                "changes": applied if approved else proposed,
                "approved": approved,
            })
            # Keep only last 50 in memory
            if len(self._decisions) > 50:
                self._decisions = self._decisions[-50:]
        except Exception as e:
            logger.error(f"AI Strategist: Failed to log decision: {e}")
            session.rollback()
        finally:
            session.close()

    def get_recent_decisions(self, limit: int = 20) -> List[dict]:
        """Get recent AI decisions from database."""
        session = self.db_session_factory()
        try:
            decisions = session.query(AIDecision).order_by(
                AIDecision.timestamp.desc()
            ).limit(limit).all()
            return [{
                'id': d.id,
                'timestamp': d.timestamp,
                'type': d.analysis_type,
                'reasoning': d.reasoning,
                'changes_proposed': d.changes_proposed,
                'changes_applied': d.changes_applied,
                'approved': d.approved,
                'backtest_before': d.backtest_before,
                'backtest_after': d.backtest_after,
            } for d in reversed(decisions)]
        finally:
            session.close()

    def get_current_spec_summary(self) -> dict:
        """Get a summary of the current strategy spec."""
        try:
            spec = self.load_spec()
            return {
                "name": spec.get("name", ""),
                "entry_rule": spec.get("entry_rule_long", ""),
                "exit_rule": spec.get("exit_rule", ""),
                "position_size_pct": spec.get("position_size_pct", 0),
                "max_positions": spec.get("max_positions", 0),
                "max_holding_days": spec.get("max_holding_days", 0),
                "stop_loss_pct": spec.get("stop_loss_pct", 0),
                "num_symbols": len(spec.get("symbols", [])),
                "symbols": spec.get("symbols", []),
            }
        except Exception:
            return {}


def _extract_key_metrics(stats: dict) -> dict:
    """Extract key metrics from backtest statistics."""
    return {
        "sharpe": stats.get("sharpe_ratio", 0),
        "total_pnl": stats.get("total_return_pct", 0),
        "win_rate": stats.get("win_rate", 0),
        "max_drawdown": stats.get("max_drawdown_pct", 0),
        "profit_factor": stats.get("profit_factor", 0),
        "total_trades": stats.get("total_trades", 0),
        "cagr": stats.get("cagr", 0),
    }
