"""SQLAlchemy ORM models for trade logging, events, and state persistence."""

import datetime
from sqlalchemy import (
    create_engine, Column, Integer, Float, String, DateTime, Boolean, Text, Enum as SAEnum
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from core.constants import TradeStatus, OrderStatus, EventType, Regime

Base = declarative_base()


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True)
    status = Column(String(30), nullable=False, default=TradeStatus.PENDING_ENTRY.name)

    # Entry
    entry_signal_time = Column(DateTime)
    entry_order_id = Column(String(50))
    entry_price = Column(Float)
    entry_time = Column(DateTime)
    shares = Column(Integer, default=0)
    entry_reason = Column(Text)

    # Stop
    initial_stop_price = Column(Float)
    current_stop_price = Column(Float)
    stop_order_id = Column(String(50))
    breakeven_set = Column(Boolean, default=False)
    r_value = Column(Float)  # entry_price - initial_stop_price

    # Exit
    exit_price = Column(Float)
    exit_time = Column(DateTime)
    exit_reason = Column(String(50))

    # Metrics
    pnl_dollars = Column(Float)
    pnl_r_multiple = Column(Float)
    slippage_entry = Column(Float)
    slippage_exit = Column(Float)

    # Regime at entry
    regime_at_entry = Column(String(20))
    size_multiplier = Column(Float, default=1.0)

    # Manual override tracking
    manually_overridden = Column(Boolean, default=False)
    bot_managed = Column(Boolean, default=True)

    # Metadata
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class BotOrder(Base):
    __tablename__ = "bot_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_id = Column(Integer, index=True)
    symbol = Column(String(20), nullable=False)
    broker_order_id = Column(String(50), index=True)
    side = Column(String(10))  # BUY / SELL
    order_type = Column(String(20))  # STOP_LIMIT, MARKET, etc.
    status = Column(String(20), default=OrderStatus.PENDING.name)

    requested_qty = Column(Integer)
    filled_qty = Column(Integer, default=0)
    stop_price = Column(Float)
    limit_price = Column(Float)
    filled_price = Column(Float)

    placed_at = Column(DateTime)
    filled_at = Column(DateTime)
    cancelled_at = Column(DateTime)
    expires_at = Column(DateTime)

    purpose = Column(String(30))  # ENTRY, STOP_LOSS, EOD_EXIT, REGIME_EXIT, KILL_EXIT

    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    event_type = Column(String(40), nullable=False, index=True)
    symbol = Column(String(20), index=True)
    trade_id = Column(Integer)
    message = Column(Text)
    details = Column(Text)  # JSON string for extra data


class RegimeState(Base):
    __tablename__ = "regime_states"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    regime = Column(String(20), nullable=False)
    benchmark = Column(String(10))
    sma10_value = Column(Float)
    sma20_value = Column(Float)
    sma10_declining_days = Column(Integer, default=0)
    sma20_declining_days = Column(Integer, default=0)
    sma10_rising_days = Column(Integer, default=0)
    sma20_rising_days = Column(Integer, default=0)
    benchmark_close = Column(Float)
    hit_rate = Column(Float)
    cooldown_remaining = Column(Integer, default=0)


class DailyPnL(Base):
    __tablename__ = "daily_pnl"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String(10), nullable=False, unique=True, index=True)
    realized_pnl = Column(Float, default=0.0)
    unrealized_pnl = Column(Float, default=0.0)
    total_r = Column(Float, default=0.0)
    trades_opened = Column(Integer, default=0)
    trades_closed = Column(Integer, default=0)
    regime = Column(String(20))
    locked_out = Column(Boolean, default=False)


class ManualOverride(Base):
    __tablename__ = "manual_overrides"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True)
    override_type = Column(String(30))  # CLOSED, PARTIAL, STOP_MODIFIED
    detected_at = Column(DateTime, default=datetime.datetime.utcnow)
    reentry_allowed_after = Column(DateTime)
    bot_management_reenabled = Column(Boolean, default=False)
    reenabled_at = Column(DateTime)


class AIDecision(Base):
    __tablename__ = "ai_decisions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    analysis_type = Column(String(30))  # DAILY, WEEKLY, TRADE_REVIEW, PARAMETER_TUNE
    reasoning = Column(Text)  # Full AI reasoning text
    changes_proposed = Column(Text)  # JSON of proposed parameter changes
    changes_applied = Column(Text)  # JSON of actually applied changes
    backtest_before = Column(Text)  # JSON of backtest metrics before change
    backtest_after = Column(Text)  # JSON of backtest metrics after change
    approved = Column(Boolean, default=False)  # Whether changes passed validation
    metrics_snapshot = Column(Text)  # JSON snapshot of performance at decision time
    spec_before = Column(Text)  # JSON of strategy spec before changes
    spec_after = Column(Text)  # JSON of strategy spec after changes


def init_db(db_path: str = "momentum_bot.db") -> sessionmaker:
    """Initialize the database and return a session factory."""
    engine = create_engine(f"sqlite:///{db_path}", echo=False)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)
