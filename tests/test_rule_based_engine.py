"""Tests for generic rule-based backtest engine."""

from pathlib import Path

import numpy as np
import pandas as pd

from backtest.config_overrides import BacktestConfig
from backtest.data_adapter import BacktestDataCache
from backtest.rule_based_engine import (
    RuleBasedBacktestEngine,
    RuleBasedStrategySpec,
    _evaluate_custom_formula,
    _evaluate_rule_scalar,
    _evaluate_rule_series,
    _sorted_indicators,
    load_strategy_spec,
    referenced_price_symbols,
    save_strategy_spec,
    symbols_from_strategy_spec,
    strategy_spec_uses_auto_us_universe,
    trade_symbols_from_strategy_spec,
)
from core.config import Config


def _make_price_data(symbol: str, start: str = "2024-01-01", periods: int = 200, amplitude: float = 8.0) -> pd.DataFrame:
    idx = pd.date_range(start, periods=periods, freq="B")
    base = 100 + np.sin(np.linspace(0, 18, periods)) * amplitude
    close = pd.Series(base, index=idx)
    high = close + 1.0
    low = close - 1.0
    open_ = close.shift(1).fillna(close.iloc[0])
    volume = pd.Series(2_000_000, index=idx)
    return pd.DataFrame(
        {
            "datetime": idx,
            "open": open_.values,
            "high": high.values,
            "low": low.values,
            "close": close.values,
            "volume": volume.values,
        }
    )


def test_rule_based_engine_runs_mean_reversion_spec():
    data = {
        "AAPL": _make_price_data("AAPL"),
        "SPY": _make_price_data("SPY"),
    }
    cache = BacktestDataCache(data)
    bt = BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-10-01",
        strategy_type="rule_based",
        symbols=["AAPL"],
        benchmark="SPY",
    )
    spec = RuleBasedStrategySpec.from_dict(
        {
            "name": "MR AAPL",
            "symbols": ["AAPL"],
            "indicators": [{"name": "sma_20", "type": "sma", "source": "close", "length": 20}],
            "entry_rule": "close < sma_20 * 0.98",
            "exit_rule": "close >= sma_20",
            "position_size_pct": 25,
            "max_positions": 1,
            "max_holding_days": 15,
            "stop_loss_pct": 5,
        }
    )

    engine = RuleBasedBacktestEngine(Config(), bt, cache, spec)
    results = engine.run()

    assert "error" not in results
    assert "equity_curve" in results
    assert results["total_trades"] > 0


def test_save_and_load_strategy_spec(tmp_path):
    raw_spec = {
        "name": "Test Strategy",
        "symbols": ["AAPL"],
        "entry_rule": "close > open",
        "exit_rule": "close < open",
    }
    spec_path = tmp_path / "strategy.json"
    save_strategy_spec(raw_spec, str(spec_path))

    bt = BacktestConfig(
        strategy_type="rule_based",
        strategy_spec_path=str(spec_path),
    )
    loaded = load_strategy_spec(bt)
    assert loaded.name == "Test Strategy"
    assert loaded.symbols == ["AAPL"]
    assert symbols_from_strategy_spec(raw_spec) == ["AAPL"]


def test_strategy_spec_allows_null_optional_numeric_fields():
    raw_spec = {
        "name": "Null Optionals",
        "symbols": ["AAPL"],
        "entry_rule": "close > open",
        "exit_rule": "close < open",
        "position_size_pct": None,
        "stop_loss_pct": None,
        "take_profit_pct": None,
        "max_holding_days": None,
    }
    spec = RuleBasedStrategySpec.from_dict(raw_spec)
    assert spec.position_size_pct == 10.0
    assert spec.stop_loss_pct == 0.0
    assert spec.take_profit_pct == 0.0
    assert spec.max_holding_days == 0


def test_symbols_from_strategy_spec_handles_none_symbols():
    assert symbols_from_strategy_spec({"symbols": None}) == []


def test_symbols_from_strategy_spec_filters_all_us_tokens():
    raw = {"symbols": ["ALL_US", "AAPL", "__AUTO_US__", "MSFT"]}
    assert symbols_from_strategy_spec(raw) == ["AAPL", "MSFT"]


def test_strategy_spec_auto_us_detection():
    raw = {"symbols": ["ALL US SYMBOLS"]}
    assert strategy_spec_uses_auto_us_universe(raw)
    spec = RuleBasedStrategySpec.from_dict(
        {
            "name": "Auto US",
            "symbols": ["ALL_US"],
            "entry_rule_long": "close > 0",
            "exit_rule": "False",
        }
    )
    assert strategy_spec_uses_auto_us_universe(spec)
    assert trade_symbols_from_strategy_spec(spec) == []


def test_rule_expression_supports_abs_function():
    frame = _make_price_data("AAPL", periods=40)
    frame["sma_5"] = frame["close"].rolling(5, min_periods=5).mean()
    signal = _evaluate_rule_series("abs(close - sma_5) >= 0", frame)
    assert signal.dtype == bool
    assert len(signal) == len(frame)


def test_rule_expression_supports_bracket_lookback_syntax():
    frame = _make_price_data("AAPL", periods=60)
    frame["spy_close"] = frame["close"] * 0.98
    signal = _evaluate_rule_series(
        "close / spy_close > (close[20] / spy_close[20])",
        frame,
    )
    assert signal.dtype == bool
    assert len(signal) == len(frame)


def test_rule_expression_bracket_lookback_handles_short_history():
    frame = _make_price_data("AAPL", periods=10)
    signal = _evaluate_rule_series("close > close[20]", frame)
    assert signal.dtype == bool
    assert len(signal) == len(frame)
    assert not signal.any()


def test_strategy_spec_parses_percentage_and_string_numbers():
    raw_spec = {
        "name": "String Inputs",
        "symbols": ["AAPL"],
        "entry_rule": "close > open",
        "exit_rule": "close < open",
        "entry_price_field": "open",
        "use_intraday_vwap_stop": "true",
        "intraday_interval": "5m",
        "backtest_timeframe": "5m",
        "position_size_mode": "risk_pct",
        "risk_per_trade_pct": "0.75%",
        "position_size_pct": "25%",
        "max_positions": "2",
        "stop_loss_pct": "5%",
        "take_profit_pct": "10.5",
        "max_holding_days": "15",
    }
    spec = RuleBasedStrategySpec.from_dict(raw_spec)
    assert spec.position_size_pct == 25.0
    assert spec.entry_price_field == "open"
    assert spec.use_intraday_vwap_stop is True
    assert spec.intraday_interval == "5m"
    assert spec.backtest_timeframe == "5m"
    assert spec.position_size_mode == "risk_pct"
    assert spec.risk_per_trade_pct == 0.75
    assert spec.max_positions == 2
    assert spec.stop_loss_pct == 5.0
    assert spec.take_profit_pct == 10.5
    assert spec.max_holding_days == 15


def test_referenced_price_symbols_extracts_symbol_prefixes():
    spec = RuleBasedStrategySpec.from_dict(
        {
            "name": "Cross Symbol",
            "symbols": ["AAPL"],
            "entry_rule": "qqq_open > qqq_close & close > open",
            "exit_rule": "spy_close < spy_open",
        }
    )
    refs = referenced_price_symbols(spec)
    assert refs == ["QQQ", "SPY"]


def test_rule_engine_supports_prefixed_external_symbol_columns():
    data = {
        "AAPL": _make_price_data("AAPL"),
        "QQQ": _make_price_data("QQQ"),
        "SPY": _make_price_data("SPY"),
    }
    cache = BacktestDataCache(data)
    bt = BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-10-01",
        strategy_type="rule_based",
        symbols=["AAPL", "QQQ", "SPY"],
        benchmark="SPY",
    )
    spec = RuleBasedStrategySpec.from_dict(
        {
            "name": "Uses qqq_open",
            "symbols": ["AAPL"],
            "entry_rule": "qqq_open > 0 & close > 0",
            "exit_rule": "qqq_close > 0",
            "position_size_pct": 20,
            "max_positions": 1,
        }
    )
    engine = RuleBasedBacktestEngine(Config(), bt, cache, spec)
    results = engine.run()
    assert "error" not in results


def test_rule_engine_supports_indicators_from_reference_symbol_source():
    data = {
        "AAPL": _make_price_data("AAPL"),
        "SPY": _make_price_data("SPY"),
    }
    cache = BacktestDataCache(data)
    bt = BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-10-01",
        strategy_type="rule_based",
        symbols=["AAPL"],
        benchmark="SPY",
    )
    spec = RuleBasedStrategySpec.from_dict(
        {
            "name": "Reference-source indicator",
            "symbols": ["AAPL"],
            "indicators": [
                {"name": "spy_sma_20", "type": "sma", "source": "spy_close", "length": 20}
            ],
            "entry_rule_long": "spy_sma_20 > 0",
            "exit_rule": "False",
            "position_size_pct": 10,
            "max_positions": 1,
        }
    )
    results = RuleBasedBacktestEngine(Config(), bt, cache, spec).run()
    assert "error" not in results


def test_rule_engine_supports_short_entries_and_position_side_exit():
    data = {
        "AAPL": _make_price_data("AAPL"),
        "SPY": _make_price_data("SPY"),
    }
    cache = BacktestDataCache(data)
    bt = BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-10-01",
        strategy_type="rule_based",
        symbols=["AAPL"],
        benchmark="SPY",
    )
    spec = RuleBasedStrategySpec.from_dict(
        {
            "name": "Short Test",
            "symbols": ["AAPL"],
            "entry_rule_long": "",
            "entry_rule_short": "close > 0",
            "exit_rule": "position_side == 'SHORT'",
            "position_size_pct": 10,
            "max_positions": 1,
            "stop_loss_pct": 2,
            "max_holding_days": 0,
        }
    )
    engine = RuleBasedBacktestEngine(Config(), bt, cache, spec)
    results = engine.run()
    assert "error" not in results
    assert results["total_trades"] > 0
    assert all(t.get("side") == "SHORT" for t in results.get("trades", []))


def test_rule_engine_exit_rule_supports_entry_price_variable():
    data = {
        "AAPL": _make_price_data("AAPL"),
        "SPY": _make_price_data("SPY"),
    }
    cache = BacktestDataCache(data)
    bt = BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-10-01",
        strategy_type="rule_based",
        symbols=["AAPL"],
        benchmark="SPY",
    )
    spec = RuleBasedStrategySpec.from_dict(
        {
            "name": "Exit uses entry_price",
            "symbols": ["AAPL"],
            "entry_rule_long": "close > 0",
            "exit_rule": "close >= entry_price * 1.001",
            "position_size_pct": 10,
            "max_positions": 1,
            "stop_loss_pct": 0,
            "take_profit_pct": 0,
            "max_holding_days": 0,
        }
    )
    results = RuleBasedBacktestEngine(Config(), bt, cache, spec).run()
    assert "error" not in results
    assert results["total_trades"] > 0


def test_evaluate_rule_scalar_supports_position_side_variable():
    row = pd.Series({"close": 100.0, "open": 99.0})
    assert _evaluate_rule_scalar("position_side == 'LONG' & close > open", row, "LONG")
    assert not _evaluate_rule_scalar("position_side == 'LONG' & close > open", row, "SHORT")


def test_vwap_proxy_indicator_supported():
    data = {
        "AAPL": _make_price_data("AAPL"),
        "SPY": _make_price_data("SPY"),
    }
    cache = BacktestDataCache(data)
    bt = BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-03-31",
        strategy_type="rule_based",
        symbols=["AAPL"],
        benchmark="SPY",
    )
    spec = RuleBasedStrategySpec.from_dict(
        {
            "name": "VWAP Proxy",
            "symbols": ["AAPL"],
            "indicators": [{"name": "vwap_proxy", "type": "vwap_proxy"}],
            "entry_rule_long": "close > vwap_proxy",
            "exit_rule": "position_side == 'LONG' & close < vwap_proxy",
        }
    )
    results = RuleBasedBacktestEngine(Config(), bt, cache, spec).run()
    assert "error" not in results


def test_intraday_vwap_stop_exits_position():
    daily = _make_price_data("AAPL", start="2024-01-01", periods=5)
    intraday = pd.DataFrame(
        {
            "datetime": pd.to_datetime(
                [
                    "2024-01-02 09:35:00",
                    "2024-01-02 09:40:00",
                    "2024-01-02 09:45:00",
                ]
            ),
            "open": [100.0, 99.5, 99.0],
            "high": [100.2, 99.7, 99.2],
            "low": [99.8, 99.2, 98.8],
            "close": [100.0, 99.0, 98.5],
            "volume": [1000, 1500, 2000],
        }
    )
    cache = BacktestDataCache({"AAPL": daily, "SPY": daily.copy()}, intraday_data={"AAPL": intraday})
    bt = BacktestConfig(
        start_date="2024-01-02",
        end_date="2024-01-03",
        strategy_type="rule_based",
        symbols=["AAPL"],
        benchmark="SPY",
    )
    spec = RuleBasedStrategySpec.from_dict(
        {
            "name": "Intraday VWAP Stop",
            "symbols": ["AAPL"],
            "entry_rule_long": "close > 0",
            "exit_rule": "False",
            "entry_price_field": "open",
            "use_intraday_vwap_stop": True,
            "intraday_interval": "5m",
            "position_size_mode": "notional_pct",
            "position_size_pct": 10,
            "max_positions": 1,
            "stop_loss_pct": 0,
        }
    )
    results = RuleBasedBacktestEngine(Config(), bt, cache, spec).run()
    assert "error" not in results
    assert results["total_trades"] >= 1
    assert any(t["exit_reason"] == "INTRADAY_VWAP_STOP" for t in results.get("trades", []))


def test_intraday_backtest_timeframe_runs_true_bar_loop():
    daily = _make_price_data("AAPL", start="2024-01-01", periods=5)
    intraday = pd.DataFrame(
        {
            "datetime": pd.to_datetime(
                [
                    "2024-01-02 09:35:00",
                    "2024-01-02 09:40:00",
                    "2024-01-02 09:45:00",
                    "2024-01-02 09:50:00",
                ]
            ),
            "open": [100.0, 101.2, 100.5, 100.4],
            "high": [101.5, 101.4, 100.9, 100.6],
            "low": [99.9, 100.2, 100.1, 100.0],
            "close": [101.2, 100.4, 100.6, 100.2],
            "volume": [2000, 2200, 1800, 1600],
        }
    )
    cache = BacktestDataCache(
        {"AAPL": daily, "SPY": daily.copy()},
        intraday_data={"AAPL": intraday, "SPY": intraday.copy()},
    )
    bt = BacktestConfig(
        start_date="2024-01-02",
        end_date="2024-01-03",
        strategy_type="rule_based",
        symbols=["AAPL"],
        benchmark="SPY",
    )
    spec = RuleBasedStrategySpec.from_dict(
        {
            "name": "True Intraday Loop",
            "symbols": ["AAPL"],
            "entry_rule_long": "close > open",
            "exit_rule": "close < open",
            "entry_price_field": "close",
            "backtest_timeframe": "5m",
            "position_size_mode": "notional_pct",
            "position_size_pct": 10,
            "max_positions": 1,
            "stop_loss_pct": 0,
            "take_profit_pct": 0,
        }
    )
    results = RuleBasedBacktestEngine(Config(), bt, cache, spec).run()
    assert "error" not in results
    assert results["total_trades"] >= 1
    assert any(t["exit_reason"] == "RULE_EXIT" for t in results.get("trades", []))


def test_strategy_spec_parses_false_string_for_intraday_flag():
    spec = RuleBasedStrategySpec.from_dict(
        {
            "name": "Flag Parse",
            "symbols": ["AAPL"],
            "entry_rule_long": "close > 0",
            "exit_rule": "False",
            "use_intraday_vwap_stop": "false",
        }
    )
    assert spec.use_intraday_vwap_stop is False


def test_rule_engine_trades_resolved_symbols_for_all_us_spec():
    data = {
        "AAPL": _make_price_data("AAPL"),
        "MSFT": _make_price_data("MSFT"),
        "SPY": _make_price_data("SPY"),
    }
    cache = BacktestDataCache(data)
    bt = BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-03-31",
        strategy_type="rule_based",
        symbols=["AAPL", "MSFT", "SPY"],  # Simulates worker-resolved ALL_US universe subset
        benchmark="SPY",
    )
    spec = RuleBasedStrategySpec.from_dict(
        {
            "name": "All US resolved",
            "symbols": ["ALL_US"],
            "entry_rule_long": "close > open",
            "exit_rule": "close < open",
            "max_positions": 2,
        }
    )
    results = RuleBasedBacktestEngine(Config(), bt, cache, spec).run()
    assert "error" not in results


def test_custom_indicator_adr_formula():
    """Custom formula: ADR percentage = avg(high - low) over 20 bars / close * 100."""
    frame = _make_price_data("AAPL", periods=60)
    result = _evaluate_custom_formula(
        "(high[0:20] - low[0:20]).mean() / close * 100", frame
    )
    assert isinstance(result, pd.Series)
    assert len(result) == len(frame)
    # First 19 bars should be NaN (insufficient rolling window), then valid
    assert result.iloc[25] > 0


def test_custom_indicator_relative_strength_formula():
    """Custom formula: relative strength vs benchmark using shift."""
    frame = _make_price_data("AAPL", periods=120)
    frame["spy_close"] = frame["close"] * 1.02
    result = _evaluate_custom_formula(
        "(close / close[60]) / (spy_close / spy_close[60])", frame
    )
    assert isinstance(result, pd.Series)
    assert len(result) == len(frame)


def test_external_indicator_creates_column():
    """External indicator type copies reference column to named indicator."""
    data = {
        "AAPL": _make_price_data("AAPL"),
        "SPY": _make_price_data("SPY"),
    }
    cache = BacktestDataCache(data)
    bt = BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-06-01",
        strategy_type="rule_based",
        symbols=["AAPL"],
        benchmark="SPY",
    )
    spec = RuleBasedStrategySpec.from_dict(
        {
            "name": "External Indicator Test",
            "symbols": ["AAPL"],
            "indicators": [
                {"name": "spy_close", "type": "external", "symbol": "SPY", "field": "close"},
                {"name": "spy_sma_20", "type": "sma", "source": "spy_close", "length": 20},
            ],
            "entry_rule_long": "close > 0 & spy_close > spy_sma_20",
            "exit_rule": "False",
            "position_size_pct": 10,
            "max_positions": 1,
        }
    )
    results = RuleBasedBacktestEngine(Config(), bt, cache, spec).run()
    assert "error" not in results
    assert results["total_trades"] > 0


def test_external_and_custom_indicators_end_to_end():
    """Full strategy with external + custom indicators (mirrors working_strategy.json)."""
    data = {
        "AAPL": _make_price_data("AAPL", periods=250),
        "SPY": _make_price_data("SPY", periods=250, amplitude=4.0),  # weaker SPY so relative_strength > 1
    }
    cache = BacktestDataCache(data)
    bt = BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-12-01",
        strategy_type="rule_based",
        symbols=["AAPL"],
        benchmark="SPY",
    )
    spec = RuleBasedStrategySpec.from_dict(
        {
            "name": "Full Custom+External Test",
            "symbols": ["AAPL"],
            "indicators": [
                {"name": "sma_50", "type": "sma", "source": "close", "length": 50},
                {"name": "sma_200", "type": "sma", "source": "close", "length": 200},
                {"name": "sma_20", "type": "sma", "source": "close", "length": 20},
                {"name": "rsi_14", "type": "rsi", "source": "close", "length": 14},
                {"name": "spy_close", "type": "external", "symbol": "SPY", "field": "close"},
                {"name": "spy_sma_20", "type": "sma", "source": "spy_close", "length": 20},
                {
                    "name": "adr_20",
                    "type": "custom",
                    "formula": "(high[0:20] - low[0:20]).mean() / close * 100",
                },
                {
                    "name": "relative_strength",
                    "type": "custom",
                    "formula": "(close / close[60]) / (spy_close / spy_close[60])",
                },
            ],
            "entry_rule_long": (
                "close > sma_50 & close > sma_200 & "
                "close <= sma_20 * 1.02 & close >= sma_20 * 0.98 & "
                "rsi_14 > 40 & relative_strength > 1.0 & "
                "volume > 1000000 & close > 10 & adr_20 >= 0.5"
            ),
            "exit_rule": "close < sma_20",
            "entry_price_field": "open",
            "position_size_mode": "risk_pct",
            "risk_per_trade_pct": 0.75,
            "max_positions": 1,
            "stop_loss_pct": 4.0,
        }
    )
    results = RuleBasedBacktestEngine(Config(), bt, cache, spec).run()
    assert "error" not in results


def test_sorted_indicators_dependency_order():
    """Externals should come first, then standard, then custom."""
    indicators = [
        {"name": "sma_20", "type": "sma", "source": "close", "length": 20},
        {"name": "adr_20", "type": "custom", "formula": "(high[0:20] - low[0:20]).mean() / close * 100"},
        {"name": "spy_close", "type": "external", "symbol": "SPY", "field": "close"},
        {"name": "rsi_14", "type": "rsi", "source": "close", "length": 14},
    ]
    sorted_inds = _sorted_indicators(indicators)
    types = [ind.get("type") for ind in sorted_inds]
    assert types == ["external", "sma", "rsi", "custom"]


def test_referenced_price_symbols_includes_external_indicators():
    """referenced_price_symbols should include symbols from external indicator defs."""
    spec = RuleBasedStrategySpec.from_dict(
        {
            "name": "External refs",
            "symbols": ["AAPL"],
            "indicators": [
                {"name": "spy_close", "type": "external", "symbol": "SPY", "field": "close"},
            ],
            "entry_rule_long": "close > 0",
            "exit_rule": "False",
        }
    )
    refs = referenced_price_symbols(spec)
    assert "SPY" in refs
