from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Float, Boolean, DateTime, Integer, Text, Enum as SAEnum
from datetime import datetime
from typing import Optional
import enum

from backend.config import settings

engine = create_async_engine(f"sqlite+aiosqlite:///{settings.sqlite_db_path}", echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# ── Enums ─────────────────────────────────────────────────────────────────────

class TradeDirection(str, enum.Enum):
    LONG = "LONG"
    SHORT = "SHORT"

class TradeStatus(str, enum.Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"

class ExitReason(str, enum.Enum):
    TARGET_2R = "TARGET_2R"
    TARGET_3R = "TARGET_3R"
    STOP_LOSS = "STOP_LOSS"
    EOD_FORCE  = "EOD_FORCE"
    MANUAL     = "MANUAL"

class VixRegime(str, enum.Enum):
    LOW      = "LOW"       # VIX < 15
    NORMAL   = "NORMAL"    # 15–25
    ELEVATED = "ELEVATED"  # 25–30
    HIGH     = "HIGH"      # > 30

class BotStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    PAUSED  = "PAUSED"
    ERROR   = "ERROR"


# ── Signal ticks ──────────────────────────────────────────────────────────────

class SignalTick(Base):
    """
    Full signal state captured every 60s per ticker when a directional candidate exists.
    Written for both fired and blocked signals so every evaluation is replayable.
    """
    __tablename__ = "signal_ticks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    ticker: Mapped[str] = mapped_column(String(10), index=True)
    price: Mapped[float] = mapped_column(Float)

    # ── GEX levels ───────────────────────────────────────────────────────────
    gex_call_wall: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gex_put_wall: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gex_gamma_flip: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gex_net: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gex_proximity_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gex_wall_side: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)   # CALL / PUT / FLIP

    # ── Footprint candles ────────────────────────────────────────────────────
    footprint_delta_1m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    footprint_delta_5m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    footprint_absorption: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    footprint_absorption_side: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)

    # ── Order book (L2) ──────────────────────────────────────────────────────
    l2_bid_wall_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    l2_bid_wall_size: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    l2_ask_wall_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    l2_ask_wall_size: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    l2_imbalance: Mapped[Optional[float]] = mapped_column(Float, nullable=True)        # -1 to +1

    # ── Options flow ─────────────────────────────────────────────────────────
    options_sweep_detected: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    options_sweep_side: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    options_sweep_strike: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    options_sweep_vol_oi_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    large_print_detected: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    large_print_side: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    large_print_notional: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # ── Filter signals ───────────────────────────────────────────────────────
    iv_rank: Mapped[Optional[float]] = mapped_column(Float, nullable=True)             # 0–100
    rvol: Mapped[Optional[float]] = mapped_column(Float, nullable=True)                # e.g. 1.4 = 140% of avg
    vwap: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    dist_from_vwap_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vix_level: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vix_regime: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    is_earnings_day: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    is_macro_event: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    time_window: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)      # MORNING / AFTERNOON / OUTSIDE

    # ── Output ───────────────────────────────────────────────────────────────
    direction: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)         # LONG / SHORT
    conviction_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)    # 0–100
    score_breakdown: Mapped[Optional[str]] = mapped_column(Text, nullable=True)        # JSON
    gate_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    gated_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)               # reason if blocked
    signal_fired: Mapped[bool] = mapped_column(Boolean, default=False)                 # True if paper trade opened

    # ── Option selected for paper trade ──────────────────────────────────────
    option_symbol: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    option_strike: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    option_expiry: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    option_type: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)       # CALL / PUT


# ── Signal outcomes (lazily filled) ──────────────────────────────────────────

class SignalOutcome(Base):
    """Forward price outcomes for every SignalTick, filled by the outcome_filler script."""
    __tablename__ = "signal_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_tick_id: Mapped[int] = mapped_column(Integer, index=True, unique=True)
    ticker: Mapped[str] = mapped_column(String(10))
    entry_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    direction: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)

    price_5m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_15m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_30m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_1h: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_2h: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    return_5m_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    return_15m_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    return_30m_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    return_1h_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    return_2h_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    correct_5m: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    correct_15m: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    correct_30m: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    correct_1h: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    correct_2h: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    ts_filled_5m: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ts_filled_15m: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ts_filled_30m: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ts_filled_1h: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ts_filled_2h: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


# ── Paper trades ──────────────────────────────────────────────────────────────

class PaperTrade(Base):
    """One row per paper trade opened. Captures full entry/exit with Greeks and slippage."""
    __tablename__ = "paper_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_tick_id: Mapped[int] = mapped_column(Integer, index=True)
    ticker: Mapped[str] = mapped_column(String(10), index=True)
    direction: Mapped[str] = mapped_column(String(5))
    status: Mapped[str] = mapped_column(String(10), default="OPEN")

    # Entry
    entry_ts: Mapped[datetime] = mapped_column(DateTime)
    entry_underlying_price: Mapped[float] = mapped_column(Float)
    option_symbol: Mapped[str] = mapped_column(String(30))
    option_strike: Mapped[float] = mapped_column(Float)
    option_expiry: Mapped[str] = mapped_column(String(12))
    option_type: Mapped[str] = mapped_column(String(4))            # CALL / PUT
    option_entry_price: Mapped[float] = mapped_column(Float)       # premium per share
    contracts: Mapped[int] = mapped_column(Integer)
    premium_paid: Mapped[float] = mapped_column(Float)             # total $ in

    # Greeks at entry
    delta_at_entry: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gamma_at_entry: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    theta_at_entry: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vega_at_entry: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    iv_at_entry: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Targets & stops
    stop_price: Mapped[float] = mapped_column(Float)               # premium level that triggers stop
    target_2r_price: Mapped[float] = mapped_column(Float)
    target_3r_price: Mapped[float] = mapped_column(Float)

    # Exit
    exit_ts: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    exit_underlying_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    option_exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # Greeks at exit
    delta_at_exit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    iv_at_exit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # PnL (all in USD)
    gross_pnl_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    slippage_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    commission_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    net_pnl_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Context
    conviction_score_at_entry: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vix_at_entry: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    iv_rank_at_entry: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    time_window: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)


# ── Options chain snapshots ────────────────────────────────────────────────────

class OptionsChainSnapshot(Base):
    """Full options chain stored at the moment each signal fires."""
    __tablename__ = "options_chain_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    signal_tick_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    ticker: Mapped[str] = mapped_column(String(10))
    expiry: Mapped[str] = mapped_column(String(12))
    strike: Mapped[float] = mapped_column(Float)
    option_type: Mapped[str] = mapped_column(String(4))            # CALL / PUT
    bid: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ask: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    iv: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    delta: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gamma: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    theta: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vega: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    open_interest: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    volume: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


# ── GEX snapshots ─────────────────────────────────────────────────────────────

class GexSnapshot(Base):
    """GEX key levels per ticker, polled every 5 minutes."""
    __tablename__ = "gex_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    ticker: Mapped[str] = mapped_column(String(10), index=True)
    spot_price: Mapped[float] = mapped_column(Float)
    call_wall: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    put_wall: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gamma_flip: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    net_gex: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    net_gex_regime: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)  # POSITIVE / NEGATIVE
    raw_levels: Mapped[Optional[str]] = mapped_column(Text, nullable=True)            # JSON: full per-strike GEX


# ── IV history (for rank computation) ─────────────────────────────────────────

class IvHistory(Base):
    """Daily ATM IV per ticker — used to compute rolling IV rank."""
    __tablename__ = "iv_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String(10), index=True)   # YYYY-MM-DD
    ticker: Mapped[str] = mapped_column(String(10), index=True)
    atm_iv: Mapped[float] = mapped_column(Float)                # ATM implied volatility (0–1 scale)


# ── Market snapshots ──────────────────────────────────────────────────────────

class MarketSnapshot(Base):
    """Full per-ticker market state every 60s. Used for walk-forward replay."""
    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    ticker: Mapped[str] = mapped_column(String(10), index=True)
    price: Mapped[float] = mapped_column(Float)
    vwap: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rvol: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    iv_rank: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vix: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vix_regime: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    gex_call_wall: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gex_put_wall: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gex_gamma_flip: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    l2_imbalance: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    footprint_delta_1m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


# ── Bot state ──────────────────────────────────────────────────────────────────

class BotState(Base):
    __tablename__ = "bot_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    status: Mapped[str] = mapped_column(String(10), default="STOPPED")
    paper_account_balance: Mapped[float] = mapped_column(Float, default=100_000.0)
    paper_day_start_balance: Mapped[float] = mapped_column(Float, default=100_000.0)
    total_trades: Mapped[int] = mapped_column(Integer, default=0)
    winning_trades: Mapped[int] = mapped_column(Integer, default=0)
    total_net_pnl_usd: Mapped[float] = mapped_column(Float, default=0.0)
    circuit_breaker_active: Mapped[bool] = mapped_column(Boolean, default=False)
    circuit_breaker_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BotLog(Base):
    __tablename__ = "bot_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    level: Mapped[str] = mapped_column(String(10))
    category: Mapped[str] = mapped_column(String(32))
    ticker: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# ── DB init ───────────────────────────────────────────────────────────────────

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select
        result = await session.execute(select(BotState).where(BotState.id == 1))
        state = result.scalar_one_or_none()
        if not state:
            session.add(BotState(
                id=1,
                status="STOPPED",
                paper_account_balance=100_000.0,
                paper_day_start_balance=100_000.0,
            ))
            await session.commit()
