"""Batch backtest runner — tests multiple strategies and compares results."""

import json
import logging
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from core.config import load_config
from backtest.config_overrides import BacktestConfig
from backtest.data_provider import HistoricalDataProvider
from backtest.data_adapter import BacktestDataCache
from backtest.rule_based_engine import (
    RuleBasedBacktestEngine,
    RuleBasedStrategySpec,
    referenced_price_symbols,
    strategy_spec_uses_auto_us_universe,
    trade_symbols_from_strategy_spec,
)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("batch_backtest")
logger.setLevel(logging.INFO)

# ── Configuration ──
START_DATE = "2024-02-27"
END_DATE = "2026-02-27"
STARTING_CAPITAL = 100_000.0
BENCHMARK = "SPY"
SLIPPAGE_PCT = 0.05


def load_strategies_from_dir(directory: str) -> List[dict]:
    """Load all strategy JSON files from a directory."""
    specs = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            specs.append(raw)
        except Exception as e:
            logger.warning(f"Skipping {path.name}: {e}")
    return specs


def run_single_backtest(
    spec_dict: dict,
    config,
    provider: HistoricalDataProvider,
    data_cache_store: dict,
) -> dict:
    """Run a single backtest and return results dict."""
    name = spec_dict.get("name", "Unnamed")
    logger.info(f"{'='*60}")
    logger.info(f"Running: {name}")
    logger.info(f"{'='*60}")

    try:
        spec = RuleBasedStrategySpec.from_dict(spec_dict)
    except Exception as e:
        logger.error(f"Invalid spec '{name}': {e}")
        return {"name": name, "error": str(e)}

    bt_config = BacktestConfig(
        start_date=START_DATE,
        end_date=END_DATE,
        starting_capital=STARTING_CAPITAL,
        symbols=list(spec.symbols),
        strategy_type="rule_based",
        strategy_spec=spec_dict,
        strategy_spec_path="",
        benchmark=BENCHMARK,
        slippage_pct=SLIPPAGE_PCT,
        auto_us_universe=False,
        use_computed_scanners=False,
        use_scanner_history=False,
        scanner_names=[],
        scanner_history_dir="",
        scanner_carry_forward_days=0,
    )

    # Determine all symbols needed
    trade_syms = trade_symbols_from_strategy_spec(spec)
    ref_syms = referenced_price_symbols(spec)
    all_symbols = sorted(set(trade_syms + ref_syms + [BENCHMARK]))

    # Check if we need intraday data
    is_intraday = spec.backtest_timeframe != "1d"

    # Create a cache key for data reuse
    cache_key = tuple(sorted(all_symbols))
    intraday_cache_key = (cache_key, spec.backtest_timeframe if is_intraday else "1d")

    # Fetch or reuse daily data
    if cache_key not in data_cache_store:
        logger.info(f"Fetching daily data for {len(all_symbols)} symbols...")
        data = provider.fetch_universe(
            all_symbols, START_DATE, END_DATE,
            progress_callback=lambda c, t, s: None,
        )
        if not data:
            return {"name": name, "error": "No daily data fetched"}
        data_cache_store[cache_key] = data
        logger.info(f"Cached daily data for {len(data)} symbols")
    else:
        data = data_cache_store[cache_key]
        logger.info(f"Reusing cached daily data ({len(data)} symbols)")

    # Fetch intraday if needed
    intraday_data = {}
    if is_intraday:
        if intraday_cache_key not in data_cache_store:
            logger.info(f"Fetching {spec.backtest_timeframe} intraday data...")
            intraday_data = provider.fetch_intraday_universe(
                all_symbols, START_DATE, END_DATE,
                interval=spec.backtest_timeframe,
                progress_callback=lambda c, t, s: None,
            )
            if not intraday_data:
                logger.warning("No intraday data fetched, falling back to daily")
            else:
                data_cache_store[intraday_cache_key] = intraday_data
                logger.info(f"Cached intraday data for {len(intraday_data)} symbols")
        else:
            intraday_data = data_cache_store[intraday_cache_key]

    # Run backtest
    data_cache = BacktestDataCache(data, intraday_data=intraday_data)
    engine = RuleBasedBacktestEngine(config, bt_config, data_cache, spec)

    t0 = time.time()
    results = engine.run()
    elapsed = time.time() - t0

    results["name"] = name
    results["elapsed_sec"] = round(elapsed, 1)

    if "error" not in results:
        logger.info(
            f"  Trades: {results.get('total_trades', 0)} | "
            f"Return: {results.get('total_return_pct', 0):+.1f}% | "
            f"CAGR: {results.get('cagr', 0):.1f}% | "
            f"Sharpe: {results.get('sharpe', 0):.2f} | "
            f"Max DD: {results.get('max_drawdown', 0):.1f}% | "
            f"Win Rate: {results.get('win_rate', 0):.1f}% | "
            f"Time: {elapsed:.1f}s"
        )
    else:
        logger.error(f"  Error: {results['error']}")

    return results


def print_comparison_table(all_results: List[dict]):
    """Print a formatted comparison of all strategy results."""
    print("\n" + "=" * 120)
    print("STRATEGY COMPARISON TABLE")
    print("=" * 120)

    headers = ["Strategy", "Trades", "Return%", "CAGR%", "Sharpe", "Sortino",
               "MaxDD%", "WinRate%", "PF", "AvgR", "Time"]
    widths = [40, 7, 9, 8, 7, 7, 7, 8, 6, 7, 6]

    header_line = ""
    for h, w in zip(headers, widths):
        header_line += f"{h:>{w}} "
    print(header_line)
    print("-" * 120)

    # Sort by CAGR descending
    sorted_results = sorted(
        all_results,
        key=lambda r: r.get("cagr", -999) if "error" not in r else -999,
        reverse=True,
    )

    for r in sorted_results:
        name = r.get("name", "?")[:40]
        if "error" in r:
            print(f"{name:>40} {'ERROR: ' + r['error'][:70]}")
            continue

        row = (
            f"{name:>40} "
            f"{r.get('total_trades', 0):>7} "
            f"{r.get('total_return_pct', 0):>+8.1f}% "
            f"{r.get('cagr', 0):>7.1f}% "
            f"{r.get('sharpe', 0):>7.2f} "
            f"{r.get('sortino', 0):>7.2f} "
            f"{r.get('max_drawdown', 0):>6.1f}% "
            f"{r.get('win_rate', 0):>7.1f}% "
            f"{r.get('profit_factor', 0):>6.2f} "
            f"{r.get('avg_r', 0):>+6.2f} "
            f"{r.get('elapsed_sec', 0):>5.1f}s"
        )
        print(row)

    print("=" * 120)

    # Highlight best
    winners = [r for r in sorted_results if "error" not in r and r.get("cagr", 0) >= 100]
    if winners:
        print(f"\n*** {len(winners)} STRATEGIES WITH 100%+ CAGR ***")
        for w in winners:
            print(f"  - {w['name']}: CAGR={w['cagr']:.1f}%, Return={w['total_return_pct']:+.1f}%, Sharpe={w['sharpe']:.2f}")
    else:
        best = sorted_results[0] if sorted_results and "error" not in sorted_results[0] else None
        if best:
            print(f"\nBest so far: {best['name']} with CAGR={best.get('cagr', 0):.1f}%")
            print("No strategy hit 100%+ CAGR yet. Will iterate...")


def main():
    config = load_config("config.yaml")
    provider = HistoricalDataProvider(
        data_source=config.data.backtest_data_source,
        polygon_api_key=config.data.polygon_api_key,
        default_history_years=config.data.default_history_years,
    )
    data_cache_store: Dict = {}

    # Load strategies from the batch directory
    strategy_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("strategy_specs/batch_test")
    if not strategy_dir.exists():
        logger.error(f"Strategy directory {strategy_dir} not found!")
        sys.exit(1)

    strategies = load_strategies_from_dir(str(strategy_dir))
    if not strategies:
        logger.error("No strategy files found!")
        sys.exit(1)

    logger.info(f"Loaded {len(strategies)} strategies from {strategy_dir}")
    logger.info(f"Backtest period: {START_DATE} to {END_DATE}")
    logger.info(f"Starting capital: ${STARTING_CAPITAL:,.0f}")

    all_results = []
    for spec_dict in strategies:
        try:
            result = run_single_backtest(spec_dict, config, provider, data_cache_store)
            all_results.append(result)
        except Exception as e:
            logger.error(f"Fatal error on {spec_dict.get('name', '?')}: {e}")
            all_results.append({"name": spec_dict.get("name", "?"), "error": str(e)})

    provider.close()
    print_comparison_table(all_results)

    # Save results
    results_path = Path("batch_results.json")
    safe_results = []
    for r in all_results:
        safe = {k: v for k, v in r.items() if k not in ("equity_curve", "trades")}
        safe_results.append(safe)
    results_path.write_text(json.dumps(safe_results, indent=2, default=str), encoding="utf-8")
    logger.info(f"Results saved to {results_path}")


if __name__ == "__main__":
    main()
