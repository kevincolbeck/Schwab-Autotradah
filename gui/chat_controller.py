"""Controller for AI chat: API calls, response parsing, config manipulation."""

import copy
import dataclasses
import json
import logging
import re
from typing import Dict, List, Optional

from PySide6.QtCore import QThread, Signal

from core.config import Config

logger = logging.getLogger(__name__)


def get_tunable_params(config: Config) -> Dict[str, object]:
    """Walk Config dataclass fields and return {dotted.path: current_value}
    for all int/float/bool fields."""
    result = {}
    for section_name, section_val in dataclasses.asdict(config).items():
        if isinstance(section_val, dict):
            for field_name, field_val in section_val.items():
                if isinstance(field_val, (int, float, bool)):
                    result[f"{section_name}.{field_name}"] = field_val
    return result


def apply_changes_to_config(config: Config, changes: Dict[str, object]) -> Config:
    """Apply {dotted.path: new_value} to a deep copy of config. Returns new Config."""
    new_config = copy.deepcopy(config)
    for path, value in changes.items():
        parts = path.split(".")
        if len(parts) != 2:
            logger.warning(f"Invalid config path: {path}")
            continue
        section, field_name = parts
        if hasattr(new_config, section):
            sub = getattr(new_config, section)
            if hasattr(sub, field_name):
                current = getattr(sub, field_name)
                if isinstance(current, bool):
                    value = bool(value)
                elif isinstance(current, int):
                    value = int(value)
                elif isinstance(current, float):
                    value = float(value)
                setattr(sub, field_name, value)
            else:
                logger.warning(f"Unknown field: {section}.{field_name}")
        else:
            logger.warning(f"Unknown config section: {section}")
    return new_config


def build_system_prompt(config: Config, results: Optional[dict], bt_summary: str) -> str:
    """Build the system prompt that gives Claude full strategy context."""
    params = get_tunable_params(config)
    params_text = "\n".join(f"  {k}: {v}" for k, v in sorted(params.items()))

    results_text = ""
    if results and "error" not in results:
        results_text = f"""
## Most Recent Backtest Results
- Total Return: {results.get('total_return_pct', 0):.1f}%
- CAGR: {results.get('cagr', 0):.2f}%
- Max Drawdown: {results.get('max_drawdown', 0):.1f}%
- Sharpe: {results.get('sharpe', 0):.2f}
- Sortino: {results.get('sortino', 0):.2f}
- Calmar: {results.get('calmar', 0):.2f}
- Total Trades: {results.get('total_trades', 0)}
- Win Rate: {results.get('win_rate', 0):.1f}%
- Avg Win (R): {results.get('avg_win_r', 0):+.2f}
- Avg Loss (R): {results.get('avg_loss_r', 0):+.2f}
- Profit Factor: {results.get('profit_factor', 0):.2f}
- Expectancy (R): {results.get('expectancy_r', 0):+.3f}
- Max Consec Losses: {results.get('max_consec_losses', 0)}
- Avg Holding Days: {results.get('avg_holding_days', 0):.1f}
"""
        for regime, stats in results.get("regime_stats", {}).items():
            results_text += (
                f"\n### {regime} Regime\n"
                f"  Trades: {stats['count']}, Win Rate: {stats['win_rate']:.1%}, "
                f"Avg R: {stats['avg_r']:+.2f}, Total PnL: ${stats['total_pnl']:,.2f}\n"
            )
        for reason, stats in results.get("exit_stats", {}).items():
            results_text += (
                f"\n### Exit: {reason}\n"
                f"  Count: {stats['count']}, Total R: {stats['total_r']:+.2f}, "
                f"Total PnL: ${stats['total_pnl']:,.2f}\n"
            )

    return f"""You are an expert quantitative trading strategy advisor embedded in a trading backtesting system.

## Strategy Overview
Default strategy is momentum breakout for US equities. It:
1. Filters stocks by eligibility (price, volume, US equity)
2. Checks trend quality (above SMA50, SMA200, positive slope)
3. Requires volatility contraction (ATR percentile, range ratio, tight closes)
4. Enters on breakout above N-day high with volume confirmation
5. Sets stop-loss at N-day low minus buffer
6. Manages positions: breakeven stop at +1R, EOD exit if close < SMA10/20
7. Uses a regime detector (TREND/CHOP/RISK_OFF) to scale position sizes and halt trading
8. Position sizing: risk% of equity per trade, adjusted for regime

## Backtest Setup
{bt_summary}

## Current Parameter Values
{params_text}

{results_text}

## Your Role
- Analyze the user's questions about strategy performance
- You can suggest either:
  1) parameter updates to existing strategy settings
  2) a full strategy rewrite using a rule-based strategy spec
- When suggesting changes, ALWAYS include a JSON block in your response with this exact format:
```json
{{
  "proposed_changes": {{"section.field": value, ...}},
  "strategy_spec": {{
    "name": "Strategy Name",
    "description": "optional",
    "symbols": ["AAPL"],
    "indicators": [
      {{"name": "sma_20", "type": "sma", "source": "close", "length": 20}},
      {{"name": "rsi_14", "type": "rsi", "source": "close", "length": 14}}
    ],
    "entry_rule_long": "close < sma_20 & rsi_14 < 30",
    "entry_rule_short": "close > sma_20 & rsi_14 > 70",
    "exit_rule": "(position_side == 'LONG' & close >= sma_20) | (position_side == 'SHORT' & close <= sma_20)",
    "entry_price_field": "open",
    "backtest_timeframe": "1d",
    "use_intraday_vwap_stop": true,
    "intraday_interval": "5m",
    "position_size_mode": "risk_pct",
    "risk_per_trade_pct": 0.75,
    "position_size_pct": 20,
    "max_positions": 1,
    "stop_loss_pct": 4,
    "take_profit_pct": 8,
    "max_holding_days": 15
  }}
}}
```
- Use dotted notation matching the parameter names above (e.g., "sizing.risk_per_trade_pct", "breakout.lookback")
- Only propose numeric/boolean parameters that appear in the parameter list above
- `strategy_spec` is optional. Include it only when user asks for a new strategy.
- To run across the full US universe, set `symbols` to `["ALL_US"]`.
- For direction-aware strategies, use `entry_rule_long` and `entry_rule_short`.
- In `exit_rule`, `position_side` is available and equals `'LONG'` or `'SHORT'`.
- Rule expressions can reference other symbols as `<symbol>_<field>` (e.g., `qqq_open`, `spy_close`).
- Supported indicators include `sma`, `ema`, `rsi`, `zscore`, `atr`, `stddev`, `vwap_proxy`.
- For open-based entries, set `entry_price_field` to `open`.
- For intraday VWAP stop behavior, set `use_intraday_vwap_stop: true` and choose `intraday_interval`.
- For true intraday strategy simulation, set `backtest_timeframe` to an intraday interval (e.g., `5m`).
- Explain your reasoning for each proposed change
- Be specific: say "increase risk from 0.50% to 0.75%" not "increase risk a bit"
- You can propose multiple parameter changes at once
- If the user just asks a question without wanting changes, respond normally without a JSON block
- Consider trade-offs: higher risk means larger drawdowns, tighter stops mean more whipsaws, etc.
- Reference the backtest results when available to support your analysis

IMPORTANT: The JSON block must be valid JSON. The key "proposed_changes" must be present."""


class ClaudeWorker(QThread):
    """Runs the Anthropic API call in a background thread."""
    response_ready = Signal(str)
    error = Signal(str)

    def __init__(self, api_key: str, messages: list, system_prompt: str):
        super().__init__()
        self.api_key = api_key
        self.messages = messages
        self.system_prompt = system_prompt

    def run(self):
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=self.api_key)
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=2048,
                system=self.system_prompt,
                messages=self.messages,
            )
            text = response.content[0].text
            self.response_ready.emit(text)
        except Exception as e:
            logger.error(f"Claude API error: {e}", exc_info=True)
            self.error.emit(str(e))


def parse_proposed_changes(response_text: str) -> Optional[Dict[str, object]]:
    """Extract the proposed_changes JSON from Claude's response text."""
    payload = _extract_payload(response_text)
    if payload and "proposed_changes" in payload and isinstance(payload["proposed_changes"], dict):
        return payload["proposed_changes"]

    # Backward-compatible fallback
    # Try fenced code block first
    pattern = r"```json\s*(\{.*?\})\s*```"
    match = re.search(pattern, response_text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            if "proposed_changes" in data and isinstance(data["proposed_changes"], dict):
                return data["proposed_changes"]
        except json.JSONDecodeError:
            pass

    # Fallback: look for inline JSON
    pattern2 = r'\{"proposed_changes"\s*:\s*\{[^}]+\}\s*\}'
    match2 = re.search(pattern2, response_text)
    if match2:
        try:
            data = json.loads(match2.group())
            if "proposed_changes" in data:
                return data["proposed_changes"]
        except json.JSONDecodeError:
            pass

    return None


def parse_proposed_strategy_spec(response_text: str) -> Optional[Dict[str, object]]:
    """Extract optional strategy_spec JSON from Claude response."""
    payload = _extract_payload(response_text)
    if payload:
        if isinstance(payload.get("strategy_spec"), dict):
            return payload["strategy_spec"]
        if _looks_like_strategy_spec(payload):
            return payload
    for obj in _iter_json_objects(response_text):
        if isinstance(obj, dict) and _looks_like_strategy_spec(obj):
            return obj
    return None


def _extract_payload(response_text: str) -> Optional[Dict[str, object]]:
    """Extract top-level JSON payload from Claude response text."""
    # Prefer fenced JSON blocks when present.
    for block in _extract_fenced_json_blocks(response_text):
        try:
            data = json.loads(block)
            if isinstance(data, dict) and (
                "proposed_changes" in data or "strategy_spec" in data
            ):
                return data
        except json.JSONDecodeError:
            continue
    # Fallback: scan for inline JSON objects in free text.
    for obj in _iter_json_objects(response_text):
        if isinstance(obj, dict) and ("proposed_changes" in obj or "strategy_spec" in obj):
            return obj
    return None


def strip_json_block(response_text: str) -> str:
    """Remove the JSON code block from display text (shown as ProposalCard instead)."""
    cleaned = re.sub(r"```json\s*\{.*?\}\s*```", "", response_text, flags=re.DOTALL)
    return cleaned.strip()


def _extract_fenced_json_blocks(response_text: str) -> List[str]:
    pattern = r"```json\s*(\{.*?\})\s*```"
    return [m.group(1) for m in re.finditer(pattern, response_text, re.DOTALL)]


def _iter_json_objects(text: str):
    decoder = json.JSONDecoder()
    index = 0
    while True:
        brace = text.find("{", index)
        if brace < 0:
            break
        try:
            obj, offset = decoder.raw_decode(text[brace:])
            yield obj
            index = brace + max(offset, 1)
        except json.JSONDecodeError:
            index = brace + 1


def _looks_like_strategy_spec(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if "symbols" not in value:
        return False
    has_entry = any(k in value for k in ("entry_rule", "entry_rule_long", "entry_rule_short"))
    has_exit = "exit_rule" in value
    return has_entry and has_exit
