"""Tests for AI proposal parsing helpers."""

from gui.chat_controller import parse_proposed_changes, parse_proposed_strategy_spec


def test_parse_combined_changes_and_strategy_spec():
    text = """
    Here is a proposal.
    ```json
    {
      "proposed_changes": {
        "sizing.risk_per_trade_pct": 0.75
      },
      "strategy_spec": {
        "name": "AAPL Mean Reversion",
        "symbols": ["AAPL"],
        "entry_rule": "close < sma_20 * 0.98",
        "exit_rule": "close >= sma_20"
      }
    }
    ```
    """
    changes = parse_proposed_changes(text)
    spec = parse_proposed_strategy_spec(text)

    assert changes == {"sizing.risk_per_trade_pct": 0.75}
    assert spec is not None
    assert spec["name"] == "AAPL Mean Reversion"


def test_parse_changes_fallback_still_works_without_strategy_spec():
    text = """
    ```json
    {"proposed_changes": {"breakout.lookback": 55}}
    ```
    """
    changes = parse_proposed_changes(text)
    spec = parse_proposed_strategy_spec(text)

    assert changes == {"breakout.lookback": 55}
    assert spec is None


def test_parse_inline_json_without_fence():
    text = (
        "Use this payload now: "
        '{"proposed_changes":{"sizing.risk_per_trade_pct":0.75},'
        '"strategy_spec":{"name":"X","symbols":["ALL_US"],'
        '"entry_rule_long":"close>open","exit_rule":"close<open"}}'
    )
    changes = parse_proposed_changes(text)
    spec = parse_proposed_strategy_spec(text)

    assert changes == {"sizing.risk_per_trade_pct": 0.75}
    assert spec is not None
    assert spec["symbols"] == ["ALL_US"]


def test_parse_direct_strategy_spec_object_without_wrapper():
    text = """
    ```json
    {
      "name": "Direct Spec",
      "symbols": ["AAPL"],
      "entry_rule_long": "close > open",
      "entry_rule_short": "",
      "exit_rule": "close < open"
    }
    ```
    """
    changes = parse_proposed_changes(text)
    spec = parse_proposed_strategy_spec(text)

    assert changes is None
    assert spec is not None
    assert spec["name"] == "Direct Spec"
