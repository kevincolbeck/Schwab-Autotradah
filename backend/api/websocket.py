"""
WebSocket endpoint — broadcasts live bot state every 5 seconds to all connected clients.

Payload shape (matches frontend types.ts):
  {
    type: "state_update",
    ts: ISO string,
    bot_status: "RUNNING" | "PAUSED" | "STOPPED",
    market_open: bool,
    time_window: "MORNING" | "AFTERNOON" | "OUTSIDE",
    vix: float | null,
    vix_regime: "LOW" | "NORMAL" | "ELEVATED" | "HIGH",
    circuit_breaker: bool,
    paper_balance: float,
    paper_day_start: float,
    daily_pnl: float,
    total_trades: int,
    winning_trades: int,
    open_positions: [...],
    premarket_done: bool,
    tickers: { [ticker]: TickerState }
  }
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect

from backend.config import ET, ALL_TICKERS
from backend.strategy.paper_trader import paper_trader
from backend.strategy.vix_regime import get_vix, get_regime
from backend.strategy.time_filter import current_window, is_market_open
from backend.strategy.footprint import get_builder as get_footprint
from backend.strategy.order_book import get_monitor as get_ob
from backend.strategy.options_flow import options_flow_monitor
from backend.strategy.iv_rank import get_iv_rank
from backend.strategy.rvol import get_rvol_monitor
from backend.strategy.vwap import get_vwap_calc
from backend.strategy.key_levels import get_key_levels
from backend.strategy.opening_play import opening_play_analyzer
from backend.data.schwab_stream import stream_client

logger = logging.getLogger(__name__)

_connections: list[WebSocket] = []
_broadcast_task: Optional[asyncio.Task] = None


async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    _connections.append(ws)
    logger.info(f"WS client connected (total={len(_connections)})")
    try:
        # Send current state immediately on connect
        payload = await _build_payload()
        await ws.send_json(payload)
        # Keep alive — just drain any pings from the client
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"WS client error: {e}")
    finally:
        _connections.remove(ws)
        logger.info(f"WS client disconnected (total={len(_connections)})")


async def broadcast_state() -> None:
    """Push state to all connected clients. Called by bot engine and broadcast loop."""
    if not _connections:
        return
    payload = await _build_payload()
    dead = []
    for ws in list(_connections):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in _connections:
            _connections.remove(ws)


async def start_broadcast_loop(interval_seconds: float = 5.0) -> None:
    """Run indefinitely — called as a background task from main.py lifespan."""
    global _broadcast_task
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await broadcast_state()
        except Exception as e:
            logger.error(f"Broadcast loop error: {e}")


async def _build_payload() -> dict:
    from backend.database import AsyncSessionLocal, BotState, SignalTick
    from sqlalchemy import select, desc
    from backend.bot_engine import is_bot_running, is_bot_paused, _premarket_done

    # Bot state from DB
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(BotState).where(BotState.id == 1))
        state = result.scalar_one_or_none()

        # Last signal per ticker
        last_signals = {}
        for ticker in ALL_TICKERS:
            sig_result = await session.execute(
                select(SignalTick)
                .where(SignalTick.ticker == ticker)
                .order_by(desc(SignalTick.ts))
                .limit(1)
            )
            sig = sig_result.scalar_one_or_none()
            if sig:
                last_signals[ticker] = {
                    "ts": sig.ts.isoformat(),
                    "direction": sig.direction,
                    "conviction_score": sig.conviction_score,
                    "gate_passed": sig.gate_passed,
                    "gated_by": sig.gated_by,
                    "signal_fired": sig.signal_fired,
                }

    bal   = state.paper_account_balance if state else 100_000
    start = state.paper_day_start_balance if state else 100_000

    # Bot status string
    if not is_bot_running():
        bot_status = "STOPPED"
    elif is_bot_paused():
        bot_status = "PAUSED"
    else:
        bot_status = "RUNNING"

    # Per-ticker live state
    tickers: dict = {}
    for ticker in ALL_TICKERS:
        fp      = get_footprint(ticker)
        ob      = get_ob(ticker).get_state()
        rvol    = get_rvol_monitor(ticker).get_rvol()
        vwap    = get_vwap_calc(ticker).get_vwap()
        iv_rank = await get_iv_rank(ticker)
        flow    = options_flow_monitor.get_state(ticker)
        kl      = get_key_levels(ticker)
        quote   = stream_client.get_quote(ticker)
        price   = float(quote.get("last", 0) or quote.get("ask", 0) or 0)
        absorbed, abs_side = fp.get_absorption()

        op = opening_play_analyzer.get_state(ticker)
        tickers[ticker] = {
            "price": price,
            "bid": float(quote.get("bid", 0) or 0),
            "ask": float(quote.get("ask", 0) or 0),
            # GEX / key levels
            "gex_call_wall": kl.gex_call_wall if kl else None,
            "gex_put_wall": kl.gex_put_wall if kl else None,
            "gex_gamma_flip": kl.gex_gamma_flip if kl else None,
            "prev_day_high": kl.prev_day_high if kl else None,
            "prev_day_low": kl.prev_day_low if kl else None,
            "opening_range_high": kl.opening_range_high if kl else None,
            "opening_range_low": kl.opening_range_low if kl else None,
            "round_number_above": kl.round_number_above if kl else None,
            "round_number_below": kl.round_number_below if kl else None,
            # Opening play (pre-market analysis)
            "opening_play": op.to_dict() if (op.direction or op.queued or op.fired) else None,
            # Footprint
            "footprint_delta_1m": fp.get_delta_1m(),
            "footprint_delta_5m": fp.get_delta_5m(),
            "footprint_absorption": absorbed,
            "footprint_absorption_side": abs_side,
            # L2
            "l2_imbalance": ob.imbalance,
            "l2_imbalance_direction": ob.imbalance_direction(),
            "l2_bid_wall_price": ob.bid_wall_price,
            "l2_bid_wall_size": ob.bid_wall_size,
            "l2_ask_wall_price": ob.ask_wall_price,
            "l2_ask_wall_size": ob.ask_wall_size,
            # Filters
            "iv_rank": iv_rank,
            "rvol": rvol,
            "vwap": vwap,
            # Options flow
            "sweep_detected": flow.is_sweep_fresh(),
            "sweep_direction": flow.sweep_direction,
            "large_print_detected": flow.is_large_print_fresh(),
            "large_print_direction": flow.large_print_direction,
            # Last signal
            "last_signal": last_signals.get(ticker),
        }

    # Today's P&L curve (closed trades only)
    from backend.database import PaperTrade
    today_start = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
    async with AsyncSessionLocal() as pnl_session:
        pnl_result = await pnl_session.execute(
            select(PaperTrade)
            .where(
                PaperTrade.status == "CLOSED",
                PaperTrade.exit_ts >= today_start,
            )
            .order_by(PaperTrade.exit_ts)
        )
        pnl_trades = pnl_result.scalars().all()
    pnl_curve = []
    running_pnl = 0.0
    for t in pnl_trades:
        running_pnl += t.net_pnl_usd or 0
        pnl_curve.append({
            "ts": t.exit_ts.isoformat() if t.exit_ts else None,
            "cumulative_pnl": round(running_pnl, 2),
        })

    # Open positions
    positions = []
    for pos in paper_trader.positions.values():
        opt_q = stream_client.get_option_quote(pos.option_symbol)
        bid = float(opt_q.get("bid", 0) or 0)
        ask = float(opt_q.get("ask", 0) or 0)
        mid = (bid + ask) / 2 if bid and ask else 0
        unrealized = (mid - pos.entry_price) * pos.contracts * 100 if mid else None
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
            "current_mid": round(mid, 4) if mid else None,
            "unrealized_pnl": round(unrealized, 2) if unrealized is not None else None,
            "unrealized_pct": round((mid / pos.entry_price - 1) * 100, 2) if mid and pos.entry_price else None,
        })

    return {
        "type": "state_update",
        "ts": datetime.now(ET).isoformat(),
        "bot_status": bot_status,
        "market_open": is_market_open(),
        "time_window": current_window(),
        "vix": get_vix(),
        "vix_regime": get_regime(),
        "circuit_breaker": state.circuit_breaker_active if state else False,
        "circuit_breaker_reason": state.circuit_breaker_reason if state else None,
        "paper_balance": round(bal, 2),
        "paper_day_start": round(start, 2),
        "daily_pnl": round(bal - start, 2),
        "total_trades": state.total_trades if state else 0,
        "winning_trades": state.winning_trades if state else 0,
        "premarket_done": _premarket_done,
        "open_positions": positions,
        "pnl_curve": pnl_curve,
        "tickers": tickers,
    }
