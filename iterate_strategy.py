"""Iterate on strategy: run backtest 2016-2020 with detailed diagnostics."""

import json
import logging
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

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
)
from core.config import Config
from data.us_universe import USMarketUniverseProvider


def run_and_analyze(spec_path: str, start: str = "2016-01-01", end: str = "2020-12-31"):
    start_time = time.time()
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

    benchmark = "SPY"
    if benchmark not in symbols:
        symbols.append(benchmark)

    bt_config = BacktestConfig(
        start_date=start,
        end_date=end,
        starting_capital=100_000.0,
        symbols=symbols,
        strategy_type="rule_based",
        strategy_spec_path=spec_path,
        auto_us_universe=is_all_us,
        use_computed_scanners=False,
        benchmark=benchmark,
        slippage_pct=0.05,
    )

    # Fetch data
    logger.info("Loading data for %d symbols...", len(symbols))
    provider = HistoricalDataProvider()

    def progress_cb(completed, total, sym):
        if completed % 500 == 0 or completed == total:
            logger.info("Data: %d/%d symbols", completed, total)

    data = provider.fetch_universe(symbols, start, end, progress_callback=progress_cb)
    provider.close()
    logger.info("Loaded data for %d symbols", len(data))

    # Run backtest
    data_cache = BacktestDataCache(data)
    strategy_spec = load_strategy_spec(bt_config)
    config = Config()
    engine = RuleBasedBacktestEngine(config, bt_config, data_cache, strategy_spec)

    last_report = [time.time()]
    def on_progress(cur, tot, dt):
        now = time.time()
        if now - last_report[0] >= 30 or cur == tot:
            elapsed = now - start_time
            pct = cur / max(tot, 1) * 100
            logger.info("Progress: %d/%d (%.1f%%) - %s - %.0fs", cur, tot, pct, dt, elapsed)
            last_report[0] = now

    engine.on_progress = on_progress
    logger.info("Running backtest: %s to %s", start, end)
    results = engine.run()
    elapsed = time.time() - start_time

    if "error" in results:
        logger.error("ERROR: %s", results["error"])
        return results

    # === SUMMARY ===
    print("\n" + "=" * 70)
    print(f"BACKTEST RESULTS ({elapsed:.0f}s)")
    print("=" * 70)
    for key in ["total_trades", "total_return_pct", "cagr", "sharpe", "sortino",
                 "max_drawdown", "win_rate", "profit_factor", "avg_r",
                 "expectancy_r", "final_equity", "starting_capital"]:
        val = results.get(key)
        if val is not None:
            if isinstance(val, float):
                print(f"  {key:25s}: {val:>12.4f}")
            else:
                print(f"  {key:25s}: {val}")

    # === TRADE ANALYSIS ===
    trades = results.get("trades", [])
    if trades:
        print(f"\n--- TRADE ANALYSIS ({len(trades)} trades) ---")

        # Exit reason breakdown
        exit_reasons = Counter(t.get("exit_reason", "UNKNOWN") for t in trades)
        print("\nExit Reasons:")
        for reason, count in exit_reasons.most_common():
            subset = [t for t in trades if t.get("exit_reason") == reason]
            avg_r = sum(t.get("pnl_r", 0) for t in subset) / len(subset)
            total_pnl = sum(t.get("pnl_dollars", 0) for t in subset)
            win_count = sum(1 for t in subset if t.get("pnl_r", 0) > 0)
            wr = win_count / len(subset) * 100 if subset else 0
            print(f"  {reason:20s}: {count:5d} trades, avg_r={avg_r:+.3f}, "
                  f"total_pnl=${total_pnl:>12,.0f}, win_rate={wr:.1f}%")

        # Yearly breakdown
        print("\nYearly Breakdown:")
        by_year = defaultdict(list)
        for t in trades:
            entry = t.get("entry_date")
            if entry:
                yr = entry.year if hasattr(entry, "year") else int(str(entry)[:4])
                by_year[yr].append(t)
        for yr in sorted(by_year.keys()):
            yr_trades = by_year[yr]
            yr_pnl = sum(t.get("pnl_dollars", 0) for t in yr_trades)
            yr_wins = sum(1 for t in yr_trades if t.get("pnl_r", 0) > 0)
            yr_wr = yr_wins / len(yr_trades) * 100 if yr_trades else 0
            yr_avg_r = sum(t.get("pnl_r", 0) for t in yr_trades) / len(yr_trades)
            print(f"  {yr}: {len(yr_trades):4d} trades, pnl=${yr_pnl:>12,.0f}, "
                  f"win_rate={yr_wr:.1f}%, avg_r={yr_avg_r:+.3f}")

        # Holding period analysis
        holding_days = []
        for t in trades:
            ed, xd = t.get("entry_date"), t.get("exit_date")
            if ed and xd:
                try:
                    delta = (xd - ed).days
                    holding_days.append(delta)
                except:
                    pass
        if holding_days:
            print(f"\nHolding Period: avg={sum(holding_days)/len(holding_days):.1f}d, "
                  f"med={sorted(holding_days)[len(holding_days)//2]}d, "
                  f"min={min(holding_days)}d, max={max(holding_days)}d")

        # Top 10 winners and losers
        sorted_by_r = sorted(trades, key=lambda t: t.get("pnl_r", 0))
        print("\nTop 10 Losers:")
        for t in sorted_by_r[:10]:
            print(f"  {t.get('symbol','?'):6s} entry={t.get('entry_price',0):8.2f} "
                  f"exit={t.get('exit_price',0):8.2f} pnl_r={t.get('pnl_r',0):+.2f} "
                  f"${t.get('pnl_dollars',0):>10,.0f} reason={t.get('exit_reason','?')}")
        print("\nTop 10 Winners:")
        for t in sorted_by_r[-10:]:
            print(f"  {t.get('symbol','?'):6s} entry={t.get('entry_price',0):8.2f} "
                  f"exit={t.get('exit_price',0):8.2f} pnl_r={t.get('pnl_r',0):+.2f} "
                  f"${t.get('pnl_dollars',0):>10,.0f} reason={t.get('exit_reason','?')}")

        # R-distribution
        r_values = [t.get("pnl_r", 0) for t in trades]
        big_winners = sum(1 for r in r_values if r >= 3)
        small_winners = sum(1 for r in r_values if 0 < r < 3)
        small_losers = sum(1 for r in r_values if -1 <= r < 0)
        big_losers = sum(1 for r in r_values if r < -1)
        print(f"\nR-Distribution: big_win(3R+)={big_winners}, "
              f"small_win(0-3R)={small_winners}, "
              f"small_loss(-1R to 0)={small_losers}, "
              f"big_loss(<-1R)={big_losers}")

    # === REGIME STATS ===
    regime_stats = results.get("regime_stats")
    if regime_stats:
        print("\nRegime Stats:")
        for regime, stats in regime_stats.items():
            print(f"  {regime}: count={stats.get('count',0)}, "
                  f"win_rate={stats.get('win_rate',0):.2f}, "
                  f"avg_r={stats.get('avg_r',0):.3f}")

    # === EXIT STATS ===
    exit_stats = results.get("exit_stats")
    if exit_stats:
        print("\nExit Stats (from engine):")
        for reason, stats in exit_stats.items():
            print(f"  {reason}: count={stats.get('count',0)}, "
                  f"total_r={stats.get('total_r',0):.2f}, "
                  f"total_pnl=${stats.get('total_pnl',0):,.0f}")

    # Save full results
    out_path = Path("backtest_runs") / f"iterate_{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}.json"
    out_path.parent.mkdir(exist_ok=True)
    summary = {
        "timestamp": datetime.utcnow().isoformat(),
        "strategy": raw_spec.get("name"),
        "start_date": start,
        "end_date": end,
        "elapsed_seconds": round(elapsed, 1),
        "results": {
            k: results.get(k) for k in [
                "total_trades", "total_return_pct", "cagr", "sharpe", "sortino",
                "max_drawdown", "win_rate", "profit_factor", "avg_r",
                "expectancy_r", "final_equity",
            ]
        },
        "strategy_spec": raw_spec,
    }
    out_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    logger.info("Saved to %s", out_path)

    print("=" * 70)
    return results


if __name__ == "__main__":
    run_and_analyze("strategy_specs/working_strategy.json")
