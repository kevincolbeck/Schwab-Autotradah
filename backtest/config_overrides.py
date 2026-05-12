"""Backtest-specific configuration dataclass."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from strategy.scanner_history import ScannerHistory


@dataclass
class BacktestConfig:
    """Parameters specific to the backtest simulation."""
    # Date range
    start_date: str = "2016-01-01"
    end_date: str = "2026-02-20"

    # Starting capital
    starting_capital: float = 100_000.0

    # Universe
    symbols: List[str] = field(default_factory=lambda: [
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD",
        "CRM", "AVGO", "NFLX", "ADBE", "COST", "LLY", "UNH",
    ])
    strategy_type: str = "momentum_breakout"  # momentum_breakout | rule_based
    strategy_spec: Optional[Dict] = None
    strategy_spec_path: str = ""
    auto_us_universe: bool = True
    auto_universe_max_symbols: int = 0  # 0 = all
    use_computed_scanners: bool = False
    use_scanner_history: bool = False
    scanner_names: List[str] = field(default_factory=lambda: [
        "qullamaggie 1 month",
        "qullamaggie 3 month",
        "qullamaggie 6 month",
        "test scanner",
    ])
    scanner_history_dir: str = "scanner_history"
    scanner_carry_forward_days: int = 1
    scanner_history: Optional["ScannerHistory"] = None
    benchmark: str = "SPY"

    # Simulation approximations
    slippage_pct: float = 0.05
    commission_per_share: float = 0.0

    # Volume filter: daily_volume >= avg_volume_20d * min_daily_vol_ratio
    min_daily_vol_ratio: float = 1.0

    # On same bar: assume price hits high (breakeven) before low (stop)
    breakeven_before_stop_on_same_bar: bool = True

    # No spread data in daily bars
    skip_spread_check: bool = True
