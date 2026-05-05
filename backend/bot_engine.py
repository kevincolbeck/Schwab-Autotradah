"""
Main bot engine.

Lifecycle:
  9:00 AM ET  — Pre-market prep: fetch GEX, key levels, earnings, RVOL baselines
  9:30 AM ET  — Entry windows open (9:30–11:00 and 15:00–15:55)
  15:58 ET    — Hard close all positions
  16:10 ET    — Eval loop idles until next pre-market

Evaluation:
  Baseline: every 15s per ticker
  Fast path: triggered immediately when streaming data crosses a proximity threshold
             (absorption detected OR price within 0.15% of a GEX wall)

Bot state flags (set via /api/bot/start, stop, emergency-close):
  _bot_running:   False → no new evaluations or entries
  _bot_paused:    True  → evaluations run, entries blocked (graceful stop state)
"""

import asyncio
import logging
from datetime import datetime, date, time as dtime
from typing import Optional

from backend.config import (
    ET, ALL_TICKERS,
    EVAL_INTERVAL_SECONDS, POSITION_CHECK_INTERVAL_SEC,
    FAST_PATH_GEX_PROXIMITY_PCT, FAST_PATH_ABSORPTION_BARS,
    GEX_PROXIMITY_PCT,
)
from backend.database import init_db, AsyncSessionLocal, BotState, MarketSnapshot, GexSnapshot
from backend.data.schwab_stream import stream_client
from backend.data.schwab_rest import get_options_chain, get_minute_bars, get_quote
from backend.data.gex_calculator import compute_gex, GexLevels
from backend.data.event_calendar import refresh_earnings_cache
from backend.data.tick_archive import tick_archive
from backend.strategy.time_filter import is_market_open, is_hard_close_time, current_window
from backend.strategy.key_levels import (
    init_key_levels, get_key_levels, update_gex_in_key_levels,
    get_opening_range_tracker,
)
from backend.strategy.footprint import get_builder as get_footprint
from backend.strategy.order_book import get_monitor as get_ob
from backend.strategy.options_flow import options_flow_monitor
from backend.strategy.vix_regime import vix_monitor, get_vix, get_regime
from backend.strategy.vwap import get_vwap_calc
from backend.strategy.rvol import get_rvol_monitor
from backend.strategy.iv_rank import record_daily_iv
from backend.strategy.signal_engine import evaluate_ticker
from backend.strategy.paper_trader import paper_trader
from backend.strategy.opening_play import opening_play_analyzer
from backend.strategy.time_filter import is_pre_open_watch
from sqlalchemy import select, update

logger = logging.getLogger(__name__)

# ── Bot control flags ─────────────────────────────────────────────────────────
_bot_running: bool = True     # False = fully stopped (no evals, no entries)
_bot_paused: bool = False     # True = evals run, NO new entries (graceful stop)

def set_bot_running(val: bool) -> None:
    global _bot_running
    _bot_running = val
    logger.info(f"Bot running set to {val}")

def set_bot_paused(val: bool) -> None:
    global _bot_paused
    _bot_paused = val
    logger.info(f"Bot paused set to {val}")

def is_bot_running() -> bool:
    return _bot_running

def is_bot_paused() -> bool:
    return _bot_paused

# ── Caches ────────────────────────────────────────────────────────────────────
_gex_cache: dict[str, tuple[GexLevels, float]] = {}
_GEX_REFRESH_SECONDS = 300

_chain_cache: dict[str, tuple[dict, float]] = {}
_CHAIN_REFRESH_SECONDS = 60

_last_eval: dict[str, float] = {}        # ticker → last eval timestamp
_fast_path_pending: set[str] = set()     # tickers flagged for immediate eval
_streamer_info_cache: dict = {}

_premarket_done: bool = False
_premarket_date: Optional[date] = None

_pre_open_snapshot_done: bool = False   # 9:25 AM price snapshots taken
_pre_open_evaluated: bool = False       # 9:29:30 opening play assessment run
_opening_plays_fired: bool = False      # 9:30 AM queued plays executed

# ── Startup ───────────────────────────────────────────────────────────────────

async def startup() -> None:
    logger.info("Bot engine starting up...")
    await init_db()
    await paper_trader.sync_state()
    await refresh_earnings_cache()

    or_tracker = get_opening_range_tracker()

    for ticker in ALL_TICKERS:
        fp   = get_footprint(ticker)
        ob   = get_ob(ticker)
        vwap = get_vwap_calc(ticker)
        rvol = get_rvol_monitor(ticker)
        options_flow_monitor.register_ticker(ticker)

        # Archive raw ticks with side classification
        def _make_tick_cb(t):
            def _cb(symbol, price, size, ts):
                if symbol != t:
                    return
                bid = stream_client.get_quote(t).get("bid", 0) or 0
                ask = stream_client.get_quote(t).get("ask", 0) or 0
                mid = (bid + ask) / 2 if bid and ask else price
                side = "ASK" if price >= ask else ("BID" if price <= bid else
                       ("ASK" if price > mid else "BID"))
                tick_archive.write_tick(t, price, size, side, ts)
            return _cb

        stream_client.on_equity_tick(_make_tick_cb(ticker))
        stream_client.on_equity_tick(fp.on_tick)
        stream_client.on_equity_tick(vwap.on_tick)
        stream_client.on_equity_tick(or_tracker.on_tick)
        stream_client.on_l2_update(ob.on_l2_update)
        stream_client.on_quote_update(rvol.on_quote_update)

    stream_client.on_quote_update(vix_monitor.on_quote_update)
    stream_client.on_option_tick(options_flow_monitor.on_option_tick)

    # Register fast-path triggers on footprint absorption
    for ticker in ALL_TICKERS:
        def _make_fast_path_cb(t):
            def _cb(symbol, price, size, ts):
                if symbol != t:
                    return
                fp = get_footprint(t)
                absorbed, _ = fp.get_absorption()
                if absorbed:
                    _fast_path_pending.add(t)
            return _cb
        stream_client.on_equity_tick(_make_fast_path_cb(ticker))

    asyncio.create_task(_load_rvol_baselines())
    logger.info("Bot engine startup complete")


async def _load_rvol_baselines() -> None:
    for ticker in ALL_TICKERS:
        try:
            bars = await get_minute_bars(ticker, days=20)
            get_rvol_monitor(ticker).load_historical(bars)
            logger.info(f"RVOL baseline loaded: {ticker}")
        except Exception as e:
            logger.warning(f"RVOL baseline load failed for {ticker}: {e}")
        await asyncio.sleep(0.5)


# ── Pre-market routine ────────────────────────────────────────────────────────

async def run_premarket_prep() -> None:
    """
    Runs at 9:00 AM ET. Fetches GEX, builds key levels, warms up chain cache.
    No trades fire during this window. Logs a pre-market summary to DB.
    """
    global _premarket_done, _premarket_date
    logger.info("=" * 60)
    logger.info("PRE-MARKET PREP STARTING")
    logger.info("=" * 60)

    await refresh_earnings_cache()

    import time as _time
    now_ts = _time.time()

    for ticker in ALL_TICKERS:
        try:
            quote = await get_quote(ticker)
            price = float(quote.get("quote", {}).get("lastPrice", 0) or 0)
            if price <= 0:
                logger.warning(f"Pre-market: no price for {ticker}")
                continue

            # Compute GEX
            gex = await compute_gex(ticker)
            if gex:
                _gex_cache[ticker] = (gex, now_ts)
                await _persist_gex_snapshot(ticker, gex)

            # Build key levels
            levels = await init_key_levels(ticker, price, gex)

            # Warm options chain
            chain = await get_options_chain(ticker)
            if chain:
                _chain_cache[ticker] = (chain, now_ts)
                options_flow_monitor.update_open_interest(ticker, chain)
                if price:
                    await record_daily_iv(ticker, chain, price)

            logger.info(
                f"  {ticker}: price={price:.2f} | "
                f"call_wall={levels.gex_call_wall} put_wall={levels.gex_put_wall} "
                f"PDH={levels.prev_day_high} PDL={levels.prev_day_low}"
            )
            await asyncio.sleep(0.3)   # stagger REST calls

        except Exception as e:
            logger.error(f"Pre-market prep error for {ticker}: {e}", exc_info=True)

    _premarket_done = True
    _premarket_date = date.today()
    # Reset daily opening-play flags for the new day
    global _pre_open_snapshot_done, _pre_open_evaluated, _opening_plays_fired
    _pre_open_snapshot_done = False
    _pre_open_evaluated = False
    _opening_plays_fired = False
    opening_play_analyzer.reset_day()
    logger.info("PRE-MARKET PREP COMPLETE — Pre-open watch begins at 9:25 ET")

    # Broadcast pre-market state to any connected WS clients
    try:
        from backend.api.websocket import broadcast_state
        await broadcast_state()
    except Exception:
        pass


# ── GEX / chain refresh helpers ───────────────────────────────────────────────

async def _refresh_gex_if_needed(ticker: str, now_ts: float) -> Optional[GexLevels]:
    import time as _time
    cached = _gex_cache.get(ticker)
    if cached and (_time.time() - cached[1]) < _GEX_REFRESH_SECONDS:
        return cached[0]
    gex = await compute_gex(ticker)
    if gex:
        _gex_cache[ticker] = (gex, _time.time())
        await _persist_gex_snapshot(ticker, gex)
        # Sync into key levels
        quote = stream_client.get_quote(ticker)
        price = float(quote.get("last", 0) or 0)
        if price:
            update_gex_in_key_levels(ticker, gex, price)
    return gex if gex else (cached[0] if cached else None)


async def _refresh_chain_if_needed(ticker: str) -> dict:
    import time as _time
    cached = _chain_cache.get(ticker)
    if cached and (_time.time() - cached[1]) < _CHAIN_REFRESH_SECONDS:
        return cached[0]
    try:
        chain = await get_options_chain(ticker)
        _chain_cache[ticker] = (chain, _time.time())
        options_flow_monitor.update_open_interest(ticker, chain)
        spot = chain.get("underlyingPrice", 0)
        if spot:
            fp = get_footprint(ticker)
            q = stream_client.get_quote(ticker)
            bid = float(q.get("bid", 0) or 0)
            ask = float(q.get("ask", 0) or 0)
            if bid and ask:
                fp.update_quote(bid, ask)
            await record_daily_iv(ticker, chain, float(spot))
        return chain
    except Exception as e:
        logger.warning(f"Chain refresh failed for {ticker}: {e}")
        return cached[0] if cached else {}


async def _get_streamer_info_cached() -> dict:
    global _streamer_info_cache
    if not _streamer_info_cache:
        from backend.data.schwab_auth import get_streamer_info
        _streamer_info_cache = await get_streamer_info()
    return _streamer_info_cache


# ── Persist helpers ───────────────────────────────────────────────────────────

async def _persist_gex_snapshot(ticker: str, gex: GexLevels) -> None:
    async with AsyncSessionLocal() as session:
        session.add(GexSnapshot(
            ticker=ticker,
            spot_price=gex.spot_price,
            call_wall=gex.call_wall,
            put_wall=gex.put_wall,
            gamma_flip=gex.gamma_flip,
            net_gex=gex.net_gex,
            net_gex_regime=gex.net_gex_regime,
            raw_levels=gex.to_json(),
        ))
        await session.commit()


async def _persist_market_snapshot(ticker: str, price: float, gex: Optional[GexLevels]) -> None:
    from backend.strategy.iv_rank import get_iv_rank
    iv_rank = await get_iv_rank(ticker)
    rvol    = get_rvol_monitor(ticker).get_rvol()
    vwap    = get_vwap_calc(ticker).get_vwap()
    fp      = get_footprint(ticker)
    ob      = get_ob(ticker).get_state()
    async with AsyncSessionLocal() as session:
        session.add(MarketSnapshot(
            ticker=ticker,
            price=price,
            vwap=vwap,
            rvol=rvol,
            iv_rank=iv_rank,
            vix=get_vix(),
            vix_regime=get_regime(),
            gex_call_wall=gex.call_wall if gex else None,
            gex_put_wall=gex.put_wall if gex else None,
            gex_gamma_flip=gex.gamma_flip if gex else None,
            l2_imbalance=ob.imbalance,
            footprint_delta_1m=fp.get_delta_1m(),
        ))
        await session.commit()


# ── Evaluation of one ticker ──────────────────────────────────────────────────

async def _eval_ticker(ticker: str) -> None:
    """Full signal evaluation for a single ticker."""
    if not _bot_running:
        return
    import time as _time
    now_ts = _time.time()

    quote = stream_client.get_quote(ticker)
    price = float(quote.get("last", 0) or quote.get("ask", 0) or 0)
    if price <= 0:
        return

    gex   = await _refresh_gex_if_needed(ticker, now_ts)
    chain = await _refresh_chain_if_needed(ticker)

    signal = await evaluate_ticker(ticker, price, gex, paused=_bot_paused)

    if signal.gate_passed and signal.direction and not _bot_paused:
        pos = await paper_trader.try_open(signal, chain, quote)
        if pos:
            info = await _get_streamer_info_cached()
            await stream_client.subscribe_options(
                [pos.option_symbol],
                info.get("schwabClientCustomerId", ""),
                info.get("schwabClientCorrelId", ""),
            )

    await _persist_market_snapshot(ticker, price, gex)
    _last_eval[ticker] = now_ts

    # Check if price is now within fast-path proximity of GEX wall
    if gex:
        prox, _ = gex.proximity_to_nearest_wall()
        if prox <= FAST_PATH_GEX_PROXIMITY_PCT:
            _fast_path_pending.add(ticker)


# ── Pre-open watch helpers ────────────────────────────────────────────────────

async def _run_pre_open_evaluation() -> None:
    """
    Called at 9:29:30 AM ET. Scores each ticker for an opening play.
    Plays that cross the conviction threshold are queued for execution at 9:30.
    """
    from backend.strategy.iv_rank import get_iv_rank
    logger.info("PRE-OPEN WATCH: Running opening play evaluation...")
    for ticker in ALL_TICKERS:
        try:
            q = stream_client.get_quote(ticker)
            price = float(q.get("last", 0) or q.get("ask", 0) or 0)
            if price <= 0:
                continue
            kl = get_key_levels(ticker)
            ob_state = get_ob(ticker).get_state()
            iv_rank = await get_iv_rank(ticker)
            gex = _gex_cache.get(ticker, (None, 0))[0]
            opening_play_analyzer.evaluate(
                ticker=ticker,
                current_price=price,
                prev_close=kl.prev_day_close if kl else None,
                l2_imbalance=ob_state.imbalance,
                l2_direction=ob_state.imbalance_direction(),
                gex_call_wall=gex.call_wall if gex else None,
                gex_put_wall=gex.put_wall if gex else None,
                iv_rank=iv_rank,
            )
        except Exception as e:
            logger.error(f"Pre-open eval error {ticker}: {e}", exc_info=True)
    queued = opening_play_analyzer.get_queued_plays()
    logger.info(f"PRE-OPEN WATCH: {len(queued)} play(s) queued for 9:30 — "
                + ", ".join(f"{t} {s.direction}" for t, s in queued))


async def _fire_opening_plays() -> None:
    """
    Called at the first 9:30 AM cycle. Executes all queued opening plays.
    These bypass the normal signal engine gates — the pre-open analysis IS the gate.
    """
    from backend.strategy.signal_engine import SignalResult
    plays = opening_play_analyzer.get_queued_plays()
    if not plays:
        logger.info("PRE-OPEN: No opening plays queued for execution")
        return
    logger.info(f"PRE-OPEN: Firing {len(plays)} opening play(s) at market open")
    for ticker, state in plays:
        if _bot_paused or not _bot_running:
            break
        try:
            q = stream_client.get_quote(ticker)
            price = float(q.get("last", 0) or q.get("ask", 0) or 0)
            if price <= 0:
                continue
            chain = await _refresh_chain_if_needed(ticker)
            if not chain:
                logger.warning(f"Opening play: no chain for {ticker}, skipping")
                continue
            signal = SignalResult(
                ticker=ticker,
                ts=datetime.now(ET),
                price=price,
                direction=state.direction,
                conviction_score=float(state.conviction),
                gate_passed=True,
                gated_by=None,
                time_window="MORNING",
                score_breakdown={"opening_play": state.conviction,
                                 "gap_pct": state.gap_pct,
                                 "pm_momentum_pct": state.pm_momentum_pct},
            )
            pos = await paper_trader.try_open(signal, chain, q)
            if pos:
                opening_play_analyzer.mark_fired(ticker)
                logger.info(
                    f"Opening play FIRED: {ticker} {state.direction} "
                    f"conviction={state.conviction} gap={state.gap_pct:.3%}"
                )
                info = await _get_streamer_info_cached()
                await stream_client.subscribe_options(
                    [pos.option_symbol],
                    info.get("schwabClientCustomerId", ""),
                    info.get("schwabClientCorrelId", ""),
                )
            else:
                logger.warning(f"Opening play: {ticker} blocked by paper trader guards")
        except Exception as e:
            logger.error(f"Opening play fire error {ticker}: {e}", exc_info=True)


# ── Eval loop ─────────────────────────────────────────────────────────────────

async def _eval_loop() -> None:
    """Main loop: fires every 2s, evaluates each ticker on its own schedule."""
    global _premarket_done, _premarket_date
    global _pre_open_snapshot_done, _pre_open_evaluated, _opening_plays_fired

    while True:
        await asyncio.sleep(2)

        now_et = datetime.now(ET)
        today  = now_et.date()
        t      = now_et.time()

        # Pre-market prep — run once per day at 9:00 AM
        if (t >= dtime(9, 0) and t < dtime(9, 30) and
                (_premarket_date != today or not _premarket_done)):
            await run_premarket_prep()
            continue

        # ── Pre-open watch window (9:25–9:30 ET) ─────────────────────────────
        if dtime(9, 25) <= t < dtime(9, 30) and _premarket_done:
            # Snapshot prices at 9:25 AM for momentum tracking
            if not _pre_open_snapshot_done:
                for ticker in ALL_TICKERS:
                    q = stream_client.get_quote(ticker)
                    price = float(q.get("last", 0) or q.get("ask", 0) or 0)
                    if price > 0:
                        opening_play_analyzer.snapshot_925(ticker, price)
                _pre_open_snapshot_done = True
                logger.info("PRE-OPEN WATCH: 9:25 price snapshots taken")
            # Lock in evaluation at 9:29:30
            if t >= dtime(9, 29, 30) and not _pre_open_evaluated:
                await _run_pre_open_evaluation()
                _pre_open_evaluated = True
            await asyncio.sleep(2)
            continue
        # ─────────────────────────────────────────────────────────────────────

        if not is_market_open():
            await asyncio.sleep(15)
            continue

        if not _bot_running:
            await asyncio.sleep(5)
            continue

        # Hard close at 3:58 ET
        if is_hard_close_time():
            await _hard_close_all()
            await asyncio.sleep(120)
            continue

        import time as _time
        now_ts = _time.time()

        # Fire queued opening plays on the first MORNING cycle
        if not _opening_plays_fired and t >= dtime(9, 30) and _pre_open_evaluated:
            await _fire_opening_plays()
            _opening_plays_fired = True

        # Fast-path eval for flagged tickers
        for ticker in list(_fast_path_pending):
            _fast_path_pending.discard(ticker)
            try:
                await _eval_ticker(ticker)
            except Exception as e:
                logger.error(f"Fast-path eval error {ticker}: {e}", exc_info=True)

        # Baseline eval — each ticker on its own 15s cycle
        for ticker in ALL_TICKERS:
            last = _last_eval.get(ticker, 0)
            if now_ts - last >= EVAL_INTERVAL_SECONDS:
                try:
                    await _eval_ticker(ticker)
                except Exception as e:
                    logger.error(f"Eval error {ticker}: {e}", exc_info=True)
                await asyncio.sleep(0.1)   # small stagger between tickers


async def _position_check_loop() -> None:
    """Separate loop: checks open positions every 5s for SL/TP/key-level exits."""
    while True:
        await asyncio.sleep(POSITION_CHECK_INTERVAL_SEC)
        if not _bot_running:
            continue
        try:
            # Build per-ticker microstructure for key-level exit logic
            micro = {}
            for ticker in ALL_TICKERS:
                fp  = get_footprint(ticker)
                ob  = get_ob(ticker).get_state()
                q   = stream_client.get_quote(ticker)
                micro[ticker] = {
                    "price": float(q.get("last", 0) or 0),
                    "footprint_delta_1m": fp.get_delta_1m(),
                    "l2_imbalance": ob.imbalance,
                    "key_levels": get_key_levels(ticker),
                }
            await paper_trader.check_positions(
                dict(stream_client.option_quotes), micro
            )
            tick_archive.flush()
        except Exception as e:
            logger.error(f"Position check error: {e}", exc_info=True)


# ── Hard EOD close ────────────────────────────────────────────────────────────

async def _hard_close_all() -> None:
    if not paper_trader.positions:
        return
    logger.warning(f"HARD EOD CLOSE: closing {len(paper_trader.positions)} positions")
    for pos in list(paper_trader.positions.values()):
        quote = dict(stream_client.option_quotes.get(pos.option_symbol, {}))
        if not quote:
            quote = {"bid": pos.entry_price * 0.70}
        await paper_trader._close_position(
            pos,
            float(quote.get("bid", pos.entry_price * 0.70)),
            "EOD_FORCE",
            quote,
        )
    await paper_trader.reset_day()


async def emergency_close_all() -> int:
    """Close all positions immediately. Called from /api/bot/emergency-close."""
    count = len(paper_trader.positions)
    await _hard_close_all()
    logger.warning(f"EMERGENCY CLOSE: closed {count} positions")
    return count


# ── Entry point ───────────────────────────────────────────────────────────────

async def run() -> None:
    await startup()
    await asyncio.gather(
        stream_client.start(),
        _eval_loop(),
        _position_check_loop(),
    )
