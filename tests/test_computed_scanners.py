"""Tests for ThinkScript-translated computed scanner rules."""

import numpy as np
import pandas as pd

from strategy.computed_scanners import (
    SCANNER_1M,
    SCANNER_3M,
    SCANNER_6M,
    SCANNER_TEST,
    matched_scanners,
    selected_scanner_mask,
)


def _make_df(n: int = 160, start: float = 50.0, end: float = 120.0, volume: float = 700_000):
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    close = pd.Series(np.geomspace(start, end, n), index=idx)
    high = close * 1.06
    low = close * 0.94
    vol = pd.Series(volume, index=idx)
    return pd.DataFrame(
        {
            "datetime": idx,
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": vol,
        }
    )


def test_1m_3m_6m_scanners_pass_when_thresholds_met():
    df = _make_df(start=20, end=240)
    out = matched_scanners(
        df,
        [SCANNER_1M, SCANNER_3M, SCANNER_6M],
    )
    assert SCANNER_1M in out
    assert SCANNER_3M in out
    assert SCANNER_6M in out


def test_scanner_fails_when_dollar_volume_too_low():
    df = _make_df(volume=25_000)  # Too low for 20M daily dollar vol
    out = matched_scanners(df, [SCANNER_1M, SCANNER_3M, SCANNER_6M])
    assert out == set()


def test_test_scanner_contracting_logic():
    df = _make_df(n=140, start=8, end=220, volume=800_000)

    # Force a valid "contracting" final bar:
    # today's high lower than prior 10-day high, and today's low higher than prior 10-day low
    prior = df.index[-11:-1]
    df.loc[prior, "high"] = np.linspace(230, 210, len(prior))
    df.loc[prior, "low"] = np.linspace(140, 170, len(prior))
    today = df.index[-1]
    df.loc[today, "high"] = 205
    df.loc[today, "low"] = 175
    df.loc[today, "close"] = 190
    df.loc[today, "volume"] = 900_000

    out = matched_scanners(df, [SCANNER_TEST])
    assert SCANNER_TEST in out


def test_selected_scanner_mask_returns_history_not_just_latest():
    df = _make_df(n=160, start=20, end=240)
    mask = selected_scanner_mask(df, [SCANNER_1M])

    assert mask.dtype == bool
    assert bool(mask.iloc[-1])
    assert not bool(mask.iloc[10])
    assert int(mask.sum()) > 0
