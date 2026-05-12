"""Batch optimizer for ICT-style intraday strategies on 5m data."""

from __future__ import annotations

import argparse
import itertools
import json
import random
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List

from backtest.config_overrides import BacktestConfig
from backtest.data_adapter import BacktestDataCache
from backtest.data_provider import HistoricalDataProvider
from backtest.rule_based_engine import RuleBasedBacktestEngine, RuleBasedStrategySpec
from core.config import load_config, Config


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Optimize ICT-style 5m intraday strategy.")
    parser.add_argument("--start-date", default="2024-03-02")
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument("--symbols", default="TQQQ,SOXL,TECL")
    parser.add_argument("--benchmark", default="SPY")
    parser.add_argument("--max-runs", type=int, default=30)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--slippage-pct", type=float, default=0.05)
    parser.add_argument("--starting-capital", type=float, default=100000.0)
    return parser.parse_args()


def _build_spec(symbols: List[str], params: Dict[str, float]) -> Dict:
    vol_mult = params["vol_mult"]
    pullback_band = params["pullback_band"]
    consolid_max = params["consolid_max"]
    atr_stop_mult = params["atr_stop_mult"]
    min_stop_pct = params["min_stop_pct"]
    risk_pct = params["risk_pct"]
    max_pos = int(params["max_pos"])
    ext_threshold = params["ext_threshold"]
    min_atr_ratio = params["min_atr_ratio"]
    min_dollar_vol_5m = int(params["min_dollar_vol_5m"])
    tp1_r = params["tp1_r"]
    tp2_r = params["tp2_r"]
    tp1_sell = params["tp1_sell"]
    tp2_sell = params["tp2_sell"]

    indicators = [
        {"type": "external", "symbol": "SPY", "field": "close", "name": "spy_close"},
        {"type": "ema", "length": 21, "source": "close", "name": "ema_21"},
        {"type": "ema", "length": 96, "source": "close", "name": "ema_96"},
        {"type": "ema", "length": 390, "source": "close", "name": "ema_390"},
        {"type": "ema", "length": 78, "source": "spy_close", "name": "spy_ema_78"},
        {"type": "ema", "length": 390, "source": "spy_close", "name": "spy_ema_390"},
        {"type": "atr", "length": 20, "name": "atr_20"},
        {"type": "sma", "length": 20, "source": "volume", "name": "vol_sma_20"},
        {"type": "rolling_max", "length": 12, "source": "high", "name": "range_high_12"},
        {"type": "rolling_min", "length": 12, "source": "low", "name": "range_low_12"},
        {"type": "rolling_max", "length": 78, "source": "high", "name": "day_high_78"},
        {"type": "rolling_min", "length": 78, "source": "low", "name": "day_low_78"},
        {"type": "lag", "source": "day_high_78", "periods": 1, "name": "prev_day_high"},
        {"type": "lag", "source": "day_low_78", "periods": 1, "name": "prev_day_low"},
        {
            "type": "custom",
            "name": "minutes_from_open",
            "formula": "(datetime.dt.hour * 60 + datetime.dt.minute) - 570",
        },
        {"type": "custom", "name": "dollar_vol_5m", "formula": "close * volume"},
        {"type": "custom", "name": "range_pct_12", "formula": "(range_high_12 - range_low_12) / close"},
        {"type": "custom", "name": "fvg_bull", "formula": "(low > high[2]) & ((low - high[2]) / atr_20 >= 0.15)"},
        {"type": "custom", "name": "ob_reclaim", "formula": "(close[1] < open[1]) & (low <= high[1]) & (close >= open[1])"},
        {
            "type": "custom",
            "name": "momentum_score",
            "formula": "(close / ema_96) * (volume / vol_sma_20)",
        },
        {
            "type": "custom",
            "name": "stop_dist",
            "formula": f"np.maximum(atr_20 * {atr_stop_mult:.4f}, close * {min_stop_pct:.4f})",
        },
    ]

    entry_rule_long = (
        "close >= 5"
        " & minutes_from_open >= 15"
        " & minutes_from_open <= 360"
        f" & dollar_vol_5m >= {min_dollar_vol_5m}"
        " & ema_21 > ema_96 & ema_96 > ema_390"
        " & spy_ema_78 > spy_ema_390"
        f" & atr_20 / close >= {min_atr_ratio:.6f}"
        f" & volume >= vol_sma_20 * {vol_mult:.4f}"
        " & ("
        f"(close > prev_day_high & close > range_high_12 & range_pct_12 <= {consolid_max:.6f} & fvg_bull)"
        " | "
        f"(ob_reclaim & abs(close - ema_21) / close <= {pullback_band:.6f} & close > session_vwap & close > prev_day_low)"
        ")"
    )

    exit_rule = (
        "position_side == 'LONG' & ("
        "close < session_vwap"
        f" | (abs(ema_21 - ema_96) / ema_96 > {ext_threshold:.6f} & close < ema_21)"
        f" | (abs(ema_21 - ema_96) / ema_96 <= {ext_threshold:.6f} & close < ema_96)"
        " | minutes_from_open >= 385"
        ")"
    )

    spec = {
        "name": "ICT Intraday Dual Entry Optimized",
        "description": "ICT-style dual entry strategy optimized on 5m bars.",
        "symbols": symbols,
        "indicators": indicators,
        "entry_rule_long": entry_rule_long,
        "entry_rule_short": "",
        "exit_rule": exit_rule,
        "entry_price_field": "close",
        "backtest_timeframe": "5m",
        "intraday_interval": "5m",
        "use_intraday_vwap_stop": False,
        "position_size_mode": "risk_pct",
        "risk_per_trade_pct": risk_pct,
        "position_size_pct": 20.0,
        "max_positions": max_pos,
        "max_positions_trend": max_pos,
        "max_positions_chop": max_pos,
        "stop_loss_pct": 0.0,
        "stop_loss_field": "stop_dist",
        "take_profit_pct": 0.0,
        "take_profit_rules": [
            {
                "trigger_r": tp1_r,
                "action": "sell_partial",
                "sell_pct_of_position": tp1_sell,
                "move_stop_to_pct": 0.0,
            },
            {
                "trigger_r": tp2_r,
                "action": "sell_partial",
                "sell_pct_of_position": tp2_sell,
                "move_stop_to_pct": 0.5,
            },
        ],
        "exit_rule_after_tp": False,
        "max_holding_days": 0,
        "ranking_field": "momentum_score",
        "regime_trend_threshold": 0.0,
        "daily_loss_lock_r": 0.0,
        "max_total_open_risk_r": 0.0,
    }
    return spec


def _sample_param_sets(max_runs: int, seed: int) -> List[Dict[str, float]]:
    grid = {
        "risk_pct": [0.75, 1.0, 1.25, 1.5, 2.0, 2.5],
        "vol_mult": [1.2, 1.35, 1.5, 1.65],
        "pullback_band": [0.0025, 0.0035, 0.0045, 0.006],
        "consolid_max": [0.008, 0.012, 0.016],
        "atr_stop_mult": [0.8, 1.0, 1.2, 1.5],
        "min_stop_pct": [0.003, 0.004, 0.005, 0.007],
        "max_pos": [1, 2],
        "ext_threshold": [0.015, 0.02, 0.03],
        "min_atr_ratio": [0.0018, 0.0022, 0.0028, 0.0035],
        "min_dollar_vol_5m": [200000, 300000, 500000, 700000],
        "tp1_r": [0.8, 1.0, 1.2],
        "tp2_r": [1.8, 2.0, 2.5, 3.0],
        "tp1_sell": [15.0, 20.0, 25.0, 35.0],
        "tp2_sell": [15.0, 20.0, 25.0, 35.0],
    }

    keys = list(grid.keys())
    all_combos = [dict(zip(keys, vals)) for vals in itertools.product(*[grid[k] for k in keys])]
    rng = random.Random(seed)
    rng.shuffle(all_combos)
    return all_combos[:max_runs]


def main():
    args = _parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if args.benchmark.upper() not in symbols:
        symbols_with_benchmark = symbols + [args.benchmark.upper()]
    else:
        symbols_with_benchmark = list(symbols)

    config = load_config("config.yaml")
    provider = HistoricalDataProvider(
        data_source=config.data.backtest_data_source,
        polygon_api_key=config.data.polygon_api_key,
        default_history_years=config.data.default_history_years,
    )

    print(f"[INFO] Loading daily data for {len(symbols_with_benchmark)} symbols...")
    daily_data = provider.fetch_universe(
        symbols_with_benchmark,
        args.start_date,
        args.end_date,
    )
    print(f"[INFO] Daily loaded: {len(daily_data)} symbols")

    print("[INFO] Loading 5m intraday data...")
    intraday_data = provider.fetch_intraday_universe(
        symbols_with_benchmark,
        args.start_date,
        args.end_date,
        interval="5m",
    )
    provider.close()
    print(f"[INFO] Intraday loaded: {len(intraday_data)} symbols")

    if len(intraday_data) == 0:
        raise SystemExit("No intraday data fetched; cannot run optimizer.")

    bt_config = BacktestConfig(
        start_date=args.start_date,
        end_date=args.end_date,
        starting_capital=args.starting_capital,
        symbols=symbols_with_benchmark,
        strategy_type="rule_based",
        auto_us_universe=False,
        benchmark=args.benchmark.upper(),
        slippage_pct=args.slippage_pct,
    )
    base_config = Config()

    candidates = _sample_param_sets(args.max_runs, args.seed)
    results = []

    started = datetime.utcnow()
    for idx, params in enumerate(candidates, start=1):
        spec_dict = _build_spec(symbols, params)
        spec = RuleBasedStrategySpec.from_dict(spec_dict)
        cache = BacktestDataCache(daily_data, intraday_data=intraday_data)
        engine = RuleBasedBacktestEngine(base_config, bt_config, cache, spec)
        run_result = engine.run()
        entry = {
            "run_index": idx,
            "params": params,
            "error": run_result.get("error"),
            "total_trades": run_result.get("total_trades"),
            "cagr": run_result.get("cagr"),
            "total_return_pct": run_result.get("total_return_pct"),
            "max_drawdown": run_result.get("max_drawdown"),
            "sharpe": run_result.get("sharpe"),
            "win_rate": run_result.get("win_rate"),
            "profit_factor": run_result.get("profit_factor"),
            "final_equity": run_result.get("final_equity"),
        }
        results.append(entry)

        if entry["error"]:
            print(f"[{idx:03d}/{len(candidates):03d}] ERROR: {entry['error']}")
            continue

        cagr = float(entry["cagr"] or 0.0)
        trades = int(entry["total_trades"] or 0)
        dd = float(entry["max_drawdown"] or 0.0)
        print(
            f"[{idx:03d}/{len(candidates):03d}] CAGR={cagr:8.2f}% "
            f"Trades={trades:5d} DD={dd:6.2f}%"
        )

    scored = [r for r in results if not r.get("error") and r.get("cagr") is not None]
    scored.sort(key=lambda r: float(r["cagr"]), reverse=True)

    out_dir = Path("backtest_runs")
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"intraday_opt_{stamp}.json"
    out_payload = {
        "timestamp_utc": datetime.utcnow().isoformat(),
        "started_utc": started.isoformat(),
        "start_date": args.start_date,
        "end_date": args.end_date,
        "symbols": symbols,
        "benchmark": args.benchmark.upper(),
        "max_runs": args.max_runs,
        "results": results,
        "top10": scored[:10],
    }
    out_path.write_text(json.dumps(out_payload, indent=2), encoding="utf-8")

    if not scored:
        print("[DONE] No successful runs.")
        print(f"[DONE] Saved: {out_path}")
        return

    best = scored[0]
    best_spec = _build_spec(symbols, best["params"])
    best_spec_path = Path("strategy_specs/ict_daytrading_5m_best.json")
    best_spec_path.write_text(json.dumps(best_spec, indent=2), encoding="utf-8")

    print("[DONE] Optimization complete.")
    print(f"[DONE] Best CAGR: {float(best['cagr']):.2f}%")
    print(f"[DONE] Best trades: {int(best['total_trades'] or 0)}")
    print(f"[DONE] Best drawdown: {float(best['max_drawdown'] or 0.0):.2f}%")
    print(f"[DONE] Best spec: {best_spec_path}")
    print(f"[DONE] Full results: {out_path}")


if __name__ == "__main__":
    main()
