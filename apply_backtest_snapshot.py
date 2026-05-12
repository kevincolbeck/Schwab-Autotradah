"""Apply an immutable backtest run snapshot to live/backtest config.

Usage:
  python apply_backtest_snapshot.py --run-id 20260222T021530Z_ab12cd34ef56
  python apply_backtest_snapshot.py --snapshot backtest_runs/20260222T021530Z_ab12cd34ef56.json
"""

import argparse
import json
from pathlib import Path
from typing import Dict

from backtest.rule_based_engine import save_strategy_spec
from core.config import load_config, save_config


_REPLAYABLE_CONFIG_SECTIONS = (
    "universe",
    "eligibility",
    "trend_quality",
    "contraction",
    "regime",
    "breakout",
    "pullback",
    "stop",
    "sizing",
    "eod_exit",
    "safety",
    "strategy_runtime",
)


def _apply_section(section_obj, values: Dict[str, object]) -> None:
    for key, value in values.items():
        if hasattr(section_obj, key):
            setattr(section_obj, key, value)


def _resolve_snapshot_path(run_id: str, snapshot_path: str) -> Path:
    if snapshot_path:
        return Path(snapshot_path)
    return Path("backtest_runs") / f"{run_id}.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply a backtest run snapshot to config.yaml and strategy spec.",
    )
    parser.add_argument("--run-id", default="", help="Run ID written by backtester (optional if --snapshot set).")
    parser.add_argument("--snapshot", default="", help="Path to a backtest snapshot JSON.")
    parser.add_argument("--config", default="config.yaml", help="Config path to update.")
    args = parser.parse_args()

    if not args.run_id and not args.snapshot:
        parser.error("Provide --run-id or --snapshot")

    snapshot_path = _resolve_snapshot_path(args.run_id, args.snapshot)
    if not snapshot_path.exists():
        raise FileNotFoundError(f"Snapshot not found: {snapshot_path}")

    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(snapshot, dict):
        raise ValueError("Snapshot JSON must be an object")

    config = load_config(args.config)

    runtime_snapshot = snapshot.get("runtime_config") or {}
    if isinstance(runtime_snapshot, dict):
        for section_name in _REPLAYABLE_CONFIG_SECTIONS:
            section_values = runtime_snapshot.get(section_name)
            if isinstance(section_values, dict) and hasattr(config, section_name):
                _apply_section(getattr(config, section_name), section_values)

    strategy = snapshot.get("strategy") or {}
    full_spec = strategy.get("full_spec")
    if isinstance(full_spec, dict):
        out_spec_path_text = str(strategy.get("frozen_spec_path") or "").strip()
        if not out_spec_path_text:
            out_spec_path_text = config.strategy_runtime.active_strategy_spec_path
        if not out_spec_path_text:
            out_spec_path_text = "strategy_specs/active_strategy.json"

        out_spec_path = Path(out_spec_path_text)
        save_strategy_spec(full_spec, str(out_spec_path))
        config.strategy_runtime.active_backtest_strategy = "rule_based"
        config.strategy_runtime.active_live_strategy = "rule_based"
        config.strategy_runtime.active_strategy_spec_path = str(out_spec_path).replace("\\", "/")

    save_config(config, args.config)

    print(f"Applied snapshot: {snapshot_path}")
    print(f"Updated config: {args.config}")
    if isinstance(full_spec, dict):
        print(f"Live/backtest strategy now points to: {config.strategy_runtime.active_strategy_spec_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
