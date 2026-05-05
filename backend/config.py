from pydantic_settings import BaseSettings
from pydantic import Field
from typing import List
import pytz

ET = pytz.timezone("America/New_York")


class Settings(BaseSettings):
    # Schwab API
    schwab_client_id: str = ""
    schwab_client_secret: str = ""
    schwab_redirect_uri: str = "https://127.0.0.1:8182"
    schwab_access_token: str = ""
    schwab_refresh_token: str = ""
    schwab_token_expiry: float = 0.0

    # External APIs
    finnhub_api_key: str = ""

    # Database
    sqlite_db_path: str = "./equities_bot.db"
    duckdb_path: str = "./tick_archive.duckdb"

    # Paper trading risk params
    paper_account_size: float = 100_000.0
    risk_per_trade_pct: float = 0.0025        # 0.25% of account
    option_stop_loss_pct: float = 0.30        # -30% on premium
    target_rr: float = 2.5                    # 2.5R default

    # Server
    port: int = 8001

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()

# ── Instruments ──────────────────────────────────────────────────────────────

MAG7 = ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA"]
ETFS = ["SPY", "QQQ"]
ALL_TICKERS = ETFS + MAG7

# Tickers that have true 0DTE (daily expiry) options
ZERO_DTE_TICKERS = {"SPY", "QQQ"}

# Schwab API base URLs
SCHWAB_API_BASE = "https://api.schwabapi.com"
SCHWAB_STREAM_URL = "wss://streamer-api.schwab.com/ws"

# ── Trading time windows (ET) ─────────────────────────────────────────────────

# (hour, minute) tuples
WINDOW_MORNING_START = (9, 30)
WINDOW_MORNING_END   = (11, 0)
WINDOW_AFTERNOON_START = (15, 0)
WINDOW_AFTERNOON_END   = (15, 55)   # last entry at 3:55; positions must close by 3:58
HARD_CLOSE_TIME        = (15, 58)   # force-close ALL positions at this time

# ── Signal thresholds ────────────────────────────────────────────────────────

GEX_PROXIMITY_PCT      = 0.003      # price within 0.3% of a GEX wall → approaching
MIN_CONVICTION_SCORE   = 55         # minimum score to emit a signal
HIGH_CONVICTION_SCORE  = 75         # score at which position size gets a boost
MIN_CONFIRMATIONS      = 2          # out of 4 confirmation signals required

# L2 / order book
L2_IMBALANCE_THRESHOLD = 0.60       # 60% bid or ask dominance

# Options flow sweep detection
SWEEP_VOL_OI_RATIO     = 3.0        # sweep if volume > 3× open interest on a strike
LARGE_PRINT_NOTIONAL   = 200_000    # $200k+ single trade counts as large print

# Relative volume
RVOL_MIN_THRESHOLD     = 1.3        # 30% above 20-day average

# IV rank
IV_RANK_MAX            = 60         # don't buy options above 60th percentile

# VIX thresholds
VIX_ELEVATED           = 25
VIX_HIGH               = 30

# Footprint divergence: consecutive bars where delta disagrees with price
FOOTPRINT_DIVERGENCE_BARS = 2

# Portfolio risk limits
MAX_OPEN_POSITIONS     = 3
MAX_TOTAL_PREMIUM_PCT  = 0.010      # 1.0% of account in open premium at once
DAILY_CIRCUIT_BREAKER_PCT = 0.020  # pause if paper account down 2% on the day

# Slippage model (per-contract, in dollars of premium)
SLIPPAGE = {
    "SPY": 0.0,    # buy at ask, sell at bid (spread is slippage)
    "QQQ": 0.0,    # same
    "DEFAULT": 0.02,  # Mag7 weekly: extra $0.02/contract on top of spread
}
COMMISSION_PER_CONTRACT = 0.65      # Schwab standard

# Evaluation intervals (seconds)
EVAL_INTERVAL_SECONDS       = 15    # baseline eval cycle
POSITION_CHECK_INTERVAL_SEC = 5     # how often open positions are checked for SL/TP

# Fast-path thresholds — trigger immediate eval when these are breached in the stream
FAST_PATH_GEX_PROXIMITY_PCT = 0.0015   # 0.15% from wall → skip waiting for next cycle
FAST_PATH_ABSORPTION_BARS   = 2        # consecutive absorption bars → immediate eval

# VWAP / RVOL history lookback
RVOL_LOOKBACK_DAYS     = 20
IV_RANK_LOOKBACK_DAYS  = 252        # ~1 trading year

# Footprint candle period
FOOTPRINT_CANDLE_SECONDS = 60       # 1-minute footprint candles

# Key level thresholds
KEY_LEVEL_EXIT_THRESHOLD_PCT  = 0.002   # 0.2% — approaching opposing level triggers exit check
KEY_LEVEL_ENTRY_BOOST_PCT     = 0.003   # 0.3% — within this of a supporting level → +5 conviction

# Pre-market window (bot starts but no trades fire)
PREMARKET_START      = (9, 0)    # 9:00 AM ET — startup + GEX/level computation
PRE_OPEN_WATCH_START = (9, 25)   # 9:25 AM ET — pre-open signal analysis window
PRE_OPEN_WATCH_END   = (9, 30)   # 9:30 AM ET — lock in opening plays, then fire at open
MARKET_OPEN          = (9, 30)   # 9:30 AM ET — entries allowed per window logic
MARKET_CLOSE         = (16, 10)  # 4:10 PM ET — process can idle after this
