"""
FastAPI dashboard endpoints.

All endpoints return JSON for the React frontend.
No authentication — intended for LAN/VPN access only.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func, case
from datetime import datetime, date, timedelta
from typing import Optional
import json

from backend.database import (
    get_db, SignalTick, SignalOutcome, PaperTrade, BotState, GexSnapshot,
    MarketSnapshot, IvHistory,
)
from backend.strategy.paper_trader import paper_trader
from backend.strategy.vix_regime import get_vix, get_regime
from backend.strategy.time_filter import current_window, is_market_open
from backend.data.schwab_auth import has_tokens
from backend.config import ALL_TICKERS

router = APIRouter(prefix="/api")


# ── Bot status ────────────────────────────────────────────────────────────────

@router.get("/status")
async def get_status(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(BotState).where(BotState.id == 1))
    state = result.scalar_one_or_none()
    return {
        "status": state.status if state else "UNKNOWN",
        "market_open": is_market_open(),
        "time_window": current_window(),
        "vix": get_vix(),
        "vix_regime": get_regime(),
        "paper_balance": state.paper_account_balance if state else 100_000,
        "paper_pnl_usd": state.total_net_pnl_usd if state else 0,
        "total_trades": state.total_trades if state else 0,
        "winning_trades": state.winning_trades if state else 0,
        "win_rate": (
            state.winning_trades / state.total_trades * 100
            if state and state.total_trades > 0 else 0
        ),
        "circuit_breaker": state.circuit_breaker_active if state else False,
        "circuit_breaker_reason": state.circuit_breaker_reason if state else None,
        "auth_ok": has_tokens(),
        "open_positions": paper_trader.open_position_count,
    }


# ── Paper PnL ─────────────────────────────────────────────────────────────────

@router.get("/pnl")
async def get_pnl(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
):
    """Cumulative paper PnL curve, grouped by day."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    result = await db.execute(
        select(PaperTrade)
        .where(PaperTrade.status == "CLOSED", PaperTrade.exit_ts >= cutoff)
        .order_by(PaperTrade.exit_ts)
    )
    trades = result.scalars().all()

    daily: dict[str, float] = {}
    cumulative = 0.0
    curve = []
    for t in trades:
        day_str = t.exit_ts.strftime("%Y-%m-%d") if t.exit_ts else "?"
        daily[day_str] = daily.get(day_str, 0) + (t.net_pnl_usd or 0)

    running = 0.0
    for day in sorted(daily.keys()):
        running += daily[day]
        curve.append({"date": day, "daily_pnl": round(daily[day], 2), "cumulative": round(running, 2)})

    return {"curve": curve, "total_pnl": round(running, 2)}


# ── Open positions ────────────────────────────────────────────────────────────

@router.get("/positions")
async def get_positions():
    """Live open paper positions."""
    from backend.data.schwab_stream import stream_client
    positions = []
    for pos in paper_trader.positions.values():
        opt_quote = stream_client.get_option_quote(pos.option_symbol)
        current_bid = float(opt_quote.get("bid", 0) or 0)
        current_ask = float(opt_quote.get("ask", 0) or 0)
        current_mid = (current_bid + current_ask) / 2 if current_bid and current_ask else 0

        unrealized = (current_mid - pos.entry_price) * pos.contracts * 100 if current_mid else None
        unrealized_pct = (current_mid / pos.entry_price - 1) * 100 if current_mid and pos.entry_price else None

        positions.append({
            "trade_id": pos.trade_id,
            "ticker": pos.ticker,
            "direction": pos.direction,
            "option_symbol": pos.option_symbol,
            "option_strike": pos.option_strike,
            "option_type": pos.option_type,
            "option_expiry": pos.option_expiry,
            "entry_price": pos.entry_price,
            "contracts": pos.contracts,
            "premium_paid": pos.premium_paid,
            "stop_price": pos.stop_price,
            "target_2r": pos.target_2r_price,
            "target_3r": pos.target_3r_price,
            "current_mid": round(current_mid, 4) if current_mid else None,
            "unrealized_pnl": round(unrealized, 2) if unrealized is not None else None,
            "unrealized_pct": round(unrealized_pct, 2) if unrealized_pct is not None else None,
            "entry_ts": pos.entry_ts.isoformat(),
            "conviction_score": pos.conviction_score,
        })
    return {"positions": positions}


# ── Trade history ─────────────────────────────────────────────────────────────

@router.get("/trades")
async def get_trades(
    ticker: Optional[str] = None,
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    cutoff = datetime.utcnow() - timedelta(days=days)
    q = select(PaperTrade).where(
        PaperTrade.status == "CLOSED",
        PaperTrade.entry_ts >= cutoff,
    )
    if ticker:
        q = q.where(PaperTrade.ticker == ticker)
    q = q.order_by(desc(PaperTrade.entry_ts)).limit(limit)
    result = await db.execute(q)
    trades = result.scalars().all()

    return {"trades": [
        {
            "id": t.id,
            "ticker": t.ticker,
            "direction": t.direction,
            "option_symbol": t.option_symbol,
            "option_strike": t.option_strike,
            "option_type": t.option_type,
            "option_expiry": t.option_expiry,
            "entry_ts": t.entry_ts.isoformat() if t.entry_ts else None,
            "exit_ts": t.exit_ts.isoformat() if t.exit_ts else None,
            "entry_price": t.option_entry_price,
            "exit_price": t.option_exit_price,
            "contracts": t.contracts,
            "premium_paid": t.premium_paid,
            "exit_reason": t.exit_reason,
            "gross_pnl": t.gross_pnl_usd,
            "net_pnl": t.net_pnl_usd,
            "conviction_score": t.conviction_score_at_entry,
            "iv_rank": t.iv_rank_at_entry,
            "vix": t.vix_at_entry,
            "time_window": t.time_window,
        }
        for t in trades
    ]}


# ── Signal feed ───────────────────────────────────────────────────────────────

@router.get("/signals")
async def get_signals(
    ticker: Optional[str] = None,
    fired_only: bool = False,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Recent signal ticks — both fired and gated."""
    q = select(SignalTick).order_by(desc(SignalTick.ts)).limit(limit)
    if ticker:
        q = q.where(SignalTick.ticker == ticker)
    if fired_only:
        q = q.where(SignalTick.signal_fired == True)
    result = await db.execute(q)
    ticks = result.scalars().all()

    return {"signals": [
        {
            "id": t.id,
            "ts": t.ts.isoformat(),
            "ticker": t.ticker,
            "price": t.price,
            "direction": t.direction,
            "conviction_score": t.conviction_score,
            "score_breakdown": json.loads(t.score_breakdown) if t.score_breakdown else {},
            "gate_passed": t.gate_passed,
            "gated_by": t.gated_by,
            "signal_fired": t.signal_fired,
            "gex_call_wall": t.gex_call_wall,
            "gex_put_wall": t.gex_put_wall,
            "gex_proximity_pct": t.gex_proximity_pct,
            "gex_wall_side": t.gex_wall_side,
            "footprint_delta_1m": t.footprint_delta_1m,
            "footprint_absorption": t.footprint_absorption,
            "l2_imbalance": t.l2_imbalance,
            "options_sweep_detected": t.options_sweep_detected,
            "large_print_detected": t.large_print_detected,
            "iv_rank": t.iv_rank,
            "rvol": t.rvol,
            "vix_level": t.vix_level,
            "time_window": t.time_window,
        }
        for t in ticks
    ]}


# ── Signal accuracy stats ─────────────────────────────────────────────────────

@router.get("/stats")
async def get_stats(
    days: int = Query(30, ge=7, le=365),
    db: AsyncSession = Depends(get_db),
):
    """Signal directional accuracy and paper trade performance per ticker."""
    cutoff = datetime.utcnow() - timedelta(days=days)

    stats = {}
    for ticker in ALL_TICKERS:
        # Paper trade win rate
        trade_result = await db.execute(
            select(
                func.count(PaperTrade.id).label("total"),
                func.sum(
                    case((PaperTrade.net_pnl_usd > 0, 1), else_=0)
                ).label("wins"),
                func.sum(PaperTrade.net_pnl_usd).label("total_pnl"),
                func.avg(PaperTrade.net_pnl_usd).label("avg_pnl"),
            ).where(
                PaperTrade.ticker == ticker,
                PaperTrade.status == "CLOSED",
                PaperTrade.entry_ts >= cutoff,
            )
        )
        row = trade_result.one()

        # Signal outcome accuracy
        outcome_result = await db.execute(
            select(
                func.count(SignalOutcome.id).label("total"),
                func.sum(case((SignalOutcome.correct_15m, 1), else_=0)).label("correct_15m"),
                func.sum(case((SignalOutcome.correct_30m, 1), else_=0)).label("correct_30m"),
                func.sum(case((SignalOutcome.correct_1h, 1), else_=0)).label("correct_1h"),
            ).where(SignalOutcome.ticker == ticker)
        )
        orow = outcome_result.one()

        total = int(row.total or 0)
        wins  = int(row.wins or 0)
        stats[ticker] = {
            "trades": total,
            "wins": wins,
            "win_rate": round(wins / total * 100, 1) if total > 0 else 0,
            "total_pnl": round(float(row.total_pnl or 0), 2),
            "avg_pnl": round(float(row.avg_pnl or 0), 2),
            "signal_accuracy_15m": (
                round(float(orow.correct_15m or 0) / int(orow.total or 1) * 100, 1)
                if orow.total else 0
            ),
            "signal_accuracy_30m": (
                round(float(orow.correct_30m or 0) / int(orow.total or 1) * 100, 1)
                if orow.total else 0
            ),
            "signal_accuracy_1h": (
                round(float(orow.correct_1h or 0) / int(orow.total or 1) * 100, 1)
                if orow.total else 0
            ),
        }

    return {"stats": stats, "period_days": days}


# ── GEX levels ────────────────────────────────────────────────────────────────

@router.get("/gex/{ticker}")
async def get_gex(ticker: str, db: AsyncSession = Depends(get_db)):
    """Most recent GEX snapshot for a ticker."""
    result = await db.execute(
        select(GexSnapshot)
        .where(GexSnapshot.ticker == ticker.upper())
        .order_by(desc(GexSnapshot.ts))
        .limit(1)
    )
    snap = result.scalar_one_or_none()
    if not snap:
        return {"error": "No GEX data yet"}
    return {
        "ticker": snap.ticker,
        "ts": snap.ts.isoformat(),
        "spot_price": snap.spot_price,
        "call_wall": snap.call_wall,
        "put_wall": snap.put_wall,
        "gamma_flip": snap.gamma_flip,
        "net_gex": snap.net_gex,
        "net_gex_regime": snap.net_gex_regime,
    }


# ── IV rank history ───────────────────────────────────────────────────────────

@router.get("/iv/{ticker}")
async def get_iv_history(ticker: str, days: int = 30, db: AsyncSession = Depends(get_db)):
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    result = await db.execute(
        select(IvHistory)
        .where(IvHistory.ticker == ticker.upper(), IvHistory.date >= cutoff)
        .order_by(IvHistory.date)
    )
    rows = result.scalars().all()
    return {"ticker": ticker, "history": [{"date": r.date, "iv": r.atm_iv} for r in rows]}


# ── Bot controls ──────────────────────────────────────────────────────────────
# Each endpoint includes a human-readable description so the frontend can
# display it next to the button (definitions field).

@router.post("/bot/start")
async def bot_start(db: AsyncSession = Depends(get_db)):
    """
    Resume signal evaluation and paper trading.
    The bot will evaluate signals and open new positions normally.
    """
    from backend.bot_engine import set_bot_running, set_bot_paused
    set_bot_running(True)
    set_bot_paused(False)
    result = await db.execute(select(BotState).where(BotState.id == 1))
    state = result.scalar_one_or_none()
    if state:
        state.status = "RUNNING"
        await db.commit()
    return {
        "status": "ok",
        "bot_status": "RUNNING",
        "definition": "Bot is now running. Signal evaluation active. New positions will be opened on valid signals.",
    }


@router.post("/bot/stop")
async def bot_stop(db: AsyncSession = Depends(get_db)):
    """
    Graceful stop — halt new entries but let open positions run to SL/TP naturally.
    Signal evaluation continues (data still logged). No new trades will open.
    """
    from backend.bot_engine import set_bot_paused
    set_bot_paused(True)
    result = await db.execute(select(BotState).where(BotState.id == 1))
    state = result.scalar_one_or_none()
    if state:
        state.status = "PAUSED"
        await db.commit()
    return {
        "status": "ok",
        "bot_status": "PAUSED",
        "definition": "Bot paused. No new positions will open. All open positions continue running and will exit at their stop-loss, take-profit, or EOD close.",
    }


@router.post("/bot/emergency-close")
async def bot_emergency_close():
    """
    Force-close ALL open paper positions immediately at current market price.
    The bot continues running — this only closes positions, it does not stop the bot.
    """
    from backend.bot_engine import emergency_close_all
    count = await emergency_close_all()
    return {
        "status": "ok",
        "positions_closed": count,
        "definition": "Emergency close executed. All open positions have been force-closed at current market price. The bot is still running and will evaluate new signals.",
    }


@router.post("/bot/kill")
async def bot_kill(db: AsyncSession = Depends(get_db)):
    """
    Hard stop — halt all evaluation immediately. No new positions, no monitoring.
    Existing positions are NOT closed (they will be picked up on next restart).
    Use Emergency Close first if you want positions closed.
    """
    from backend.bot_engine import set_bot_running
    set_bot_running(False)
    result = await db.execute(select(BotState).where(BotState.id == 1))
    state = result.scalar_one_or_none()
    if state:
        state.status = "STOPPED"
        await db.commit()
    return {
        "status": "ok",
        "bot_status": "STOPPED",
        "definition": "Bot fully stopped. No evaluation, no position monitoring. Open positions remain open and will be managed on next restart. Use Emergency Close first if you want positions closed now.",
    }


@router.get("/bot/key-levels/{ticker}")
async def get_key_levels_endpoint(ticker: str):
    """Current key levels for a ticker."""
    from backend.strategy.key_levels import get_key_levels
    levels = get_key_levels(ticker.upper())
    if not levels:
        return {"error": "No key levels computed yet (pre-market prep may not have run)"}
    return {"ticker": ticker.upper(), "levels": levels.to_dict()}
