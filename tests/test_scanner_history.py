"""Tests for scanner history loading and date-based symbol lookup."""

from pathlib import Path

from strategy.scanner_history import ScannerHistory


def _write_csv(path: Path, rows: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_loads_union_for_matching_scanners(tmp_path):
    _write_csv(
        tmp_path / "qullamaggie 1 month_2020-01-02.csv",
        ["symbol", "AAPL", "MSFT"],
    )
    _write_csv(
        tmp_path / "2020-01-02_qullamaggie 3 month.csv",
        ["ticker", "NVDA"],
    )
    _write_csv(
        tmp_path / "2020-01-02_other scanner.csv",
        ["symbol", "SHOULD_NOT_LOAD"],
    )

    history = ScannerHistory.from_directory(
        str(tmp_path),
        ["qullamaggie 1 month", "qullamaggie 3 month"],
    )

    symbols = history.get_symbols_for_date("2020-01-02")
    assert symbols == ["AAPL", "MSFT", "NVDA"]


def test_carry_forward_uses_previous_snapshot(tmp_path):
    _write_csv(
        tmp_path / "2020-01-02" / "test scanner.csv",
        ["symbol", "TSLA"],
    )

    history = ScannerHistory.from_directory(
        str(tmp_path),
        ["test scanner"],
    )

    assert history.get_symbols_for_date("2020-01-03", carry_forward_days=0) == []
    assert history.get_symbols_for_date("2020-01-03", carry_forward_days=1) == ["TSLA"]
    assert history.get_symbols_for_date("2020-01-05", carry_forward_days=1) == []
