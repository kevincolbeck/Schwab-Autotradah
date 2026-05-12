"""Configuration loader with defaults and validation."""

import os
import yaml
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SchwabConfig:
    client_id: str = ""
    client_secret: str = ""
    redirect_url: str = "https://127.0.0.1:8182/callback"
    token_file: str = "token.json"
    account_hash: str = ""


@dataclass
class UniverseConfig:
    source: str = "watchlists"  # watchlists | scanner_files | computed_scanners
    watchlist_names: list = field(default_factory=lambda: [
        "LEADERS_1M", "LEADERS_3M", "LEADERS_6M"
    ])
    scanner_names: list = field(default_factory=lambda: [
        "qullamaggie 1 month",
        "qullamaggie 3 month",
        "qullamaggie 6 month",
        "test scanner",
    ])
    # Computed scanner thresholds (translated from ThinkScript)
    scanner_min_adr_pct: float = 4.5
    scanner_min_dollar_vol: float = 20_000_000
    scanner_1m_min_return_pct: float = 35.0
    scanner_3m_min_return_pct: float = 35.0
    scanner_6m_min_return_pct: float = 40.0
    test_scanner_lookback_days: int = 63
    test_scanner_min_gain_pct: float = 100.0
    test_scanner_consolidation_window: int = 10
    test_scanner_adr_period: int = 20
    test_scanner_min_adr_pct: float = 7.0
    test_scanner_min_avg_dollar_vol: float = 16_500_000
    scanner_history_dir: str = "scanner_history"
    scanner_carry_forward_days: int = 1
    csv_fallback_dir: str = "universe_csv"
    use_csv_fallback: bool = False


@dataclass
class EligibilityConfig:
    min_price: float = 10.0
    min_avg_dollar_volume_20d: float = 20_000_000.0
    max_spread_pct: float = 0.20
    us_equity_only: bool = True


@dataclass
class TrendQualityConfig:
    require_above_sma50: bool = True
    sma50_slope_lookback: int = 10
    require_above_sma200: bool = True
    require_relative_strength_vs_spy: bool = False
    relative_strength_months: int = 3


@dataclass
class ContractionConfig:
    atr_period: int = 14
    atr_percentile_window: int = 120
    atr_percentile_threshold: float = 25.0
    range_short: int = 10
    range_long: int = 30
    tight_closes_window: int = 5
    tight_closes_atr_mult: float = 1.5
    min_conditions_pass: int = 2


@dataclass
class RegimeConfig:
    benchmark: str = "SPY"
    sma_short: int = 10
    sma_long: int = 20
    decline_consecutive_days: int = 3
    rise_consecutive_days: int = 2
    require_close_above_sma20: bool = False
    cooldown_days: int = 3
    hit_rate_window: int = 20
    trend_hit_rate_threshold: float = 0.50
    chop_size_multiplier: float = 0.50
    chop_min_multiplier: float = 0.35
    chop_max_multiplier: float = 0.60
    chop_breakouts_enabled: bool = True
    chop_stricter_filters: bool = True


@dataclass
class BreakoutConfig:
    lookback: int = 40
    entry_buffer_pct: float = 0.15
    limit_offset_pct: float = 0.30
    min_rel_vol_pace: float = 1.5
    max_wait_minutes: int = 3
    trigger_start_hour: int = 10
    trigger_start_minute: int = 0
    trigger_end_hour: int = 15
    trigger_end_minute: int = 40


@dataclass
class PullbackConfig:
    use_sma20: bool = True
    use_sma50: bool = True
    entry_above_reclaim_close: bool = True
    stop_buffer_pct: float = 0.15


@dataclass
class StopConfig:
    stop_buffer_pct: float = 0.15
    breakeven_buffer_pct: float = 0.0


@dataclass
class SizingConfig:
    risk_per_trade_pct: float = 0.50
    risk_min_pct: float = 0.05
    risk_max_pct: float = 2.0
    max_positions_trend: int = 8
    max_positions_chop: int = 4
    max_total_open_risk_r: float = 6.0
    max_position_pct_equity: float = 20.0


@dataclass
class EODExitConfig:
    sma_closeness_threshold: float = 0.50
    eod_window_minutes: int = 5
    use_moc_if_available: bool = True


@dataclass
class SafetyConfig:
    daily_loss_lock_r: float = 3.0
    max_spread_reject_pct: float = 0.20
    stale_data_seconds: int = 60
    reconciliation_interval_seconds: int = 15
    signal_scan_interval_seconds: int = 30


@dataclass
class AppConfig:
    shadow_mode: bool = True
    manage_exits_while_off: bool = True
    enable_addons: bool = False
    reentry_cooldown_days: int = 1
    db_path: str = "momentum_bot.db"
    log_level: str = "INFO"


@dataclass
class AIChatConfig:
    anthropic_api_key: str = ""


@dataclass
class DataConfig:
    # Data source for backtesting imports:
    # auto = yfinance for daily/weekly/monthly and polygon for long-range intraday when key exists
    # yfinance = force yfinance only
    # polygon = force polygon for supported timeframes
    backtest_data_source: str = "auto"  # auto | yfinance | polygon
    polygon_api_key: str = ""
    default_history_years: int = 20


@dataclass
class StrategyRuntimeConfig:
    # Strategy selector for backtester/live engine wiring.
    active_backtest_strategy: str = "momentum_breakout"  # momentum_breakout | rule_based
    active_live_strategy: str = "momentum_breakout"  # momentum_breakout | rule_based
    active_strategy_spec_path: str = "strategy_specs/active_strategy.json"
    # Allocated capital for live trading (0 = use full account equity).
    # When set, position sizing uses this rolling balance instead of account equity.
    allocated_capital: float = 0.0


@dataclass
class Config:
    schwab: SchwabConfig = field(default_factory=SchwabConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    eligibility: EligibilityConfig = field(default_factory=EligibilityConfig)
    trend_quality: TrendQualityConfig = field(default_factory=TrendQualityConfig)
    contraction: ContractionConfig = field(default_factory=ContractionConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    breakout: BreakoutConfig = field(default_factory=BreakoutConfig)
    pullback: PullbackConfig = field(default_factory=PullbackConfig)
    stop: StopConfig = field(default_factory=StopConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    eod_exit: EODExitConfig = field(default_factory=EODExitConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    app: AppConfig = field(default_factory=AppConfig)
    ai_chat: AIChatConfig = field(default_factory=AIChatConfig)
    data: DataConfig = field(default_factory=DataConfig)
    strategy_runtime: StrategyRuntimeConfig = field(default_factory=StrategyRuntimeConfig)


def _merge_dict(target, source):
    """Recursively merge source dict into target dataclass fields."""
    for key, value in source.items():
        if hasattr(target, key):
            attr = getattr(target, key)
            if hasattr(attr, '__dataclass_fields__') and isinstance(value, dict):
                _merge_dict(attr, value)
            else:
                setattr(target, key, value)


def load_config(path: str = "config.yaml") -> Config:
    """Load config from YAML file, falling back to defaults for missing values."""
    config = Config()
    if os.path.exists(path):
        with open(path, 'r') as f:
            raw = yaml.safe_load(f) or {}
        _merge_dict(config, raw)
    return config


def save_config(config: Config, path: str = "config.yaml"):
    """Save current config to YAML."""
    import dataclasses

    def _to_dict(obj):
        if hasattr(obj, '__dataclass_fields__'):
            return {k: _to_dict(v) for k, v in dataclasses.asdict(obj).items()}
        if isinstance(obj, list):
            return [_to_dict(i) for i in obj]
        return obj

    with open(path, 'w') as f:
        yaml.dump(_to_dict(config), f, default_flow_style=False, sort_keys=False)
