"""CLI backtest runner for 15-minute intraday strategy.

Uses yfinance for data (free, no rate limits, max ~60 days of 15m data).
For longer history, upgrade to Polygon Starter ($29/mo) for unlimited API calls.
"""

import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from backtest.config_overrides import BacktestConfig
from backtest.data_adapter import BacktestDataCache
from backtest.data_provider import HistoricalDataProvider
from backtest.rule_based_engine import (
    RuleBasedBacktestEngine,
    load_strategy_spec,
)
from core.config import Config


def main():
    start_time = time.time()

    spec_path = "strategy_specs/intraday_15m_meanrev.json"
    raw_spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    logger.info("Strategy: %s", raw_spec.get("name", "Unknown"))

    timeframe = raw_spec.get("backtest_timeframe", "15m")
    symbols = [s.strip().upper() for s in raw_spec.get("symbols", []) if s.strip()]

    ref_symbols = set()
    for ind in raw_spec.get("indicators", []):
        if ind.get("type") == "external":
            ref_symbols.add(ind["symbol"].upper())
    benchmark = "SPY"
    ref_symbols.add(benchmark)
    all_symbols = sorted(set(symbols) | ref_symbols)

    # yfinance 15m data: max ~60 days
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=58)).strftime("%Y-%m-%d")

    logger.info("Timeframe: %s", timeframe)
    logger.info("Symbols: %d trade + %d reference = %s", len(symbols), len(ref_symbols), all_symbols)
    logger.info("Date range: %s to %s (~60 days, yfinance limit for 15m)", start_date, end_date)

    bt_config = BacktestConfig(
        start_date=start_date,
        end_date=end_date,
        starting_capital=100_000.0,
        symbols=all_symbols,
        strategy_type="rule_based",
        strategy_spec_path=spec_path,
        benchmark=benchmark,
        slippage_pct=0.05,
    )

    # Use yfinance only — no rate limits
    provider = HistoricalDataProvider(data_source="yfinance")

    logger.info("Fetching %s data for %d symbols via yfinance...", timeframe, len(all_symbols))
    intraday_data = {}
    for sym in all_symbols:
        try:
            df = provider.fetch_intraday_symbol(
                sym, start_date=start_date, end_date=end_date, interval=timeframe
            )
            if not df.empty:
                intraday_data[sym] = df
                logger.info("  %s: %d bars", sym, len(df))
            else:
                logger.warning("  %s: no data", sym)
        except Exception as e:
            logger.error("  %s failed: %s", sym, e)
    provider.close()
    logger.info("Loaded %d symbols, total %d bars",
                len(intraday_data), sum(len(df) for df in intraday_data.values()))

    if not intraday_data:
        logger.error("No data fetched!")
        sys.exit(1)

    data_cache = BacktestDataCache(full_data={}, intraday_data=intraday_data)
    strategy_spec = load_strategy_spec(bt_config)
    config = Config()

    engine = RuleBasedBacktestEngine(config, bt_config, data_cache, strategy_spec)

    def on_progress(cur, tot, dt):
        if cur == tot or cur % 10 == 0:
            logger.info("Backtest: %d/%d days - %s", cur, tot, dt)

    engine.on_progress = on_progress

    logger.info("=" * 60)
    logger.info("Running %s backtest: %s to %s", timeframe, start_date, end_date)
    logger.info("=" * 60)
    results = engine.run()

    elapsed = time.time() - start_time
    logger.info("=" * 60)
    logger.info("INTRADAY BACKTEST COMPLETE (%.1f seconds)", elapsed)
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

    out_path = Path("backtest_runs") / f"intraday_15m_{datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
    out_path.parent.mkdir(exist_ok=True)
    summary = {
        "timestamp": datetime.now().isoformat(),
        "strategy": raw_spec.get("name"),
        "timeframe": timeframe,
        "start_date": start_date,
        "end_date": end_date,
        "symbols_count": len(symbols),
        "intraday_bars_loaded": sum(len(df) for df in intraday_data.values()),
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
