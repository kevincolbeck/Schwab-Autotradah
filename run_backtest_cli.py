"""CLI backtest runner for the current working_strategy.json."""

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

from backtest.config_overrides import BacktestConfig
from backtest.data_adapter import BacktestDataCache
from backtest.data_provider import HistoricalDataProvider
from backtest.rule_based_engine import (
    RuleBasedBacktestEngine,
    load_strategy_spec,
    referenced_price_symbols,
    strategy_spec_uses_auto_us_universe,
)
from core.config import Config
from data.us_universe import USMarketUniverseProvider


def main():
    start_time = time.time()

    # Load strategy spec
    spec_path = "strategy_specs/working_strategy.json"
    raw_spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    logger.info("Strategy: %s", raw_spec.get("name", "Unknown"))

    # Build symbol list
    symbols_raw = raw_spec.get("symbols", [])
    is_all_us = any(
        s.strip().upper() in {"ALL_US", "*", "ALL", "ALL_US_SYMBOLS"}
        for s in symbols_raw
    )

    if is_all_us:
        logger.info("Resolving ALL_US universe...")
        us_provider = USMarketUniverseProvider()
        symbols = us_provider.get_symbols()
        logger.info("Resolved %d US symbols", len(symbols))
    else:
        symbols = [s.strip().upper() for s in symbols_raw if s.strip()]

    # Add benchmark + reference symbols
    benchmark = "SPY"
    if benchmark not in symbols:
        symbols.append(benchmark)

    # Backtest config
    bt_config = BacktestConfig(
        start_date="2016-01-01",
        end_date="2026-02-22",
        starting_capital=100_000.0,
        symbols=symbols,
        strategy_type="rule_based",
        strategy_spec_path=spec_path,
        auto_us_universe=is_all_us,
        use_computed_scanners=False,
        benchmark=benchmark,
        slippage_pct=0.05,
    )

    # Phase 1: Fetch data (uses cache)
    logger.info("Loading historical data for %d symbols...", len(symbols))
    provider = HistoricalDataProvider()

    def progress_cb(completed, total, sym):
        if completed % 500 == 0 or completed == total:
            logger.info("Data load: %d/%d symbols", completed, total)

    data = provider.fetch_universe(
        symbols,
        bt_config.start_date,
        bt_config.end_date,
        progress_callback=progress_cb,
    )
    provider.close()
    logger.info("Loaded data for %d symbols", len(data))

    # Phase 2: Build cache and load strategy
    data_cache = BacktestDataCache(data)
    strategy_spec = load_strategy_spec(bt_config)
    config = Config()

    # Phase 3: Run backtest
    engine = RuleBasedBacktestEngine(config, bt_config, data_cache, strategy_spec)

    last_report = [time.time()]

    def on_progress(cur, tot, dt):
        now = time.time()
        if now - last_report[0] >= 30 or cur == tot:
            elapsed = now - start_time
            pct = cur / max(tot, 1) * 100
            logger.info(
                "Backtest progress: %d/%d days (%.1f%%) - %s - elapsed %.0fs",
                cur, tot, pct, dt, elapsed,
            )
            last_report[0] = now

    engine.on_progress = on_progress

    logger.info("Running backtest: %s to %s", bt_config.start_date, bt_config.end_date)
    results = engine.run()

    # Print results
    elapsed = time.time() - start_time
    logger.info("=" * 60)
    logger.info("BACKTEST COMPLETE (%.1f seconds)", elapsed)
    logger.info("=" * 60)

    if "error" in results:
        logger.error("Error: %s", results["error"])
        return

    for key in [
        "total_trades", "total_return_pct", "cagr", "sharpe", "sortino",
        "max_drawdown", "win_rate", "profit_factor", "avg_r", "expectancy_r",
        "final_equity",
    ]:
        val = results.get(key)
        if val is not None:
            if isinstance(val, float):
                logger.info("  %-20s: %.4f", key, val)
            else:
                logger.info("  %-20s: %s", key, val)

    # Save results summary
    out_path = Path("backtest_runs") / f"cli_run_{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}.json"
    out_path.parent.mkdir(exist_ok=True)
    summary = {
        "timestamp": datetime.utcnow().isoformat(),
        "strategy": raw_spec.get("name"),
        "start_date": bt_config.start_date,
        "end_date": bt_config.end_date,
        "symbols_count": len(symbols),
        "elapsed_seconds": round(elapsed, 1),
        "results": {
            k: results.get(k)
            for k in [
                "total_trades", "total_return_pct", "cagr", "sharpe", "sortino",
                "max_drawdown", "win_rate", "profit_factor", "avg_r",
                "expectancy_r", "final_equity",
            ]
        },
        "strategy_spec": raw_spec,
    }
    out_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    logger.info("Results saved to %s", out_path)


if __name__ == "__main__":
    main()
