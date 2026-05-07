"""
Signal engine — the brain of the bot.

Called every 60 seconds per ticker during market hours.
Runs all gates in order, computes a conviction score, determines direction,
and emits a SignalTick to the database.

Gate order (each must pass before the next is evaluated):
  Gate 1 — Market conditions:  time window, VIX regime, earnings, macro event
  Gate 2 — Ticker conditions:  RVOL, IV rank, ticker allowed in current regime
  Gate 3 — Structural signal:  GEX wall proximity (primary driver)
  Gate 4 — Microstructure:    footprint delta, L2 imbalance, options flow (≥2 of 4)

Conviction score (0–100):
  GEX proximity tightness:   0–30 pts
  Footprint delta strength:  0–25 pts
  L2 imbalance magnitude:    0–20 pts
  Large print present:       0–15 pts
  Options sweep present:     0–10 pts
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from backend.config import (
    ET, MIN_CONVICTION_SCORE, HIGH_CONVICTION_SCORE,
    MIN_CONFIRMATIONS, GEX_PROXIMITY_PCT, FOOTPRINT_DIVERGENCE_BARS,
    BREAKOUT_RVOL_MIN, BREAKOUT_MIN_CONFIRMATIONS, GEX_BREAKOUT_WINDOW_PCT,
)
from backend.database import AsyncSessionLocal, SignalTick, SignalOutcome
from backend.data.event_calendar import is_earnings_day, is_macro_event_today, is_fomc_window
from backend.strategy.time_filter import (
    is_entry_allowed, current_window, is_market_open, should_skip_afternoon_entry
)
from backend.strategy.vix_regime import get_regime, is_ticker_allowed, get_vix
from backend.strategy.iv_rank import iv_rank_passes_gate
from backend.strategy.footprint import get_builder as get_footprint
from backend.strategy.order_book import get_monitor as get_ob
from backend.strategy.options_flow import options_flow_monitor
from backend.strategy.rvol import get_rvol_monitor
from backend.strategy.vwap import get_vwap_calc

logger = logging.getLogger(__name__)


@dataclass
class SignalResult:
    ticker: str
    ts: datetime
    price: float
    direction: Optional[str] = None        # LONG / SHORT / None
    conviction_score: float = 0.0
    gate_passed: bool = False
    gated_by: Optional[str] = None
    score_breakdown: dict = field(default_factory=dict)
    time_window: str = "OUTSIDE"

    # Raw signal values (stored to DB)
    gex_call_wall: Optional[float] = None
    gex_put_wall: Optional[float] = None
    gex_gamma_flip: Optional[float] = None
    gex_net: Optional[float] = None
    gex_proximity_pct: Optional[float] = None
    gex_wall_side: Optional[str] = None

    footprint_delta_1m: Optional[float] = None
    footprint_delta_5m: Optional[float] = None
    footprint_absorption: Optional[bool] = None
    footprint_absorption_side: Optional[str] = None

    l2_bid_wall_price: Optional[float] = None
    l2_bid_wall_size: Optional[float] = None
    l2_ask_wall_price: Optional[float] = None
    l2_ask_wall_size: Optional[float] = None
    l2_imbalance: Optional[float] = None

    options_sweep_detected: Optional[bool] = None
    options_sweep_side: Optional[str] = None
    options_sweep_strike: Optional[float] = None
    options_sweep_vol_oi_ratio: Optional[float] = None
    large_print_detected: Optional[bool] = None
    large_print_side: Optional[str] = None
    large_print_notional: Optional[float] = None

    iv_rank: Optional[float] = None
    rvol: Optional[float] = None
    vwap: Optional[float] = None
    dist_from_vwap_pct: Optional[float] = None
    vix_level: Optional[float] = None
    vix_regime: Optional[str] = None
    is_earnings_day: bool = False
    is_macro_event: bool = False

    # Option selected for this signal
    option_symbol: Optional[str] = None
    option_strike: Optional[float] = None
    option_expiry: Optional[str] = None
    option_type: Optional[str] = None


def _detect_wall_breakout(prev_gex, price: float) -> tuple[bool, str]:
    """
    Returns (True, side) if price just crossed a GEX wall since the previous reading.
    Only counts as fresh if within GEX_BREAKOUT_WINDOW_PCT past the wall — avoids
    chasing a move that already ran far from the level.
    """
    if prev_gex is None:
        return False, ""
    if prev_gex.call_wall and prev_gex.spot_price < prev_gex.call_wall:
        if price >= prev_gex.call_wall:
            if (price - prev_gex.call_wall) / prev_gex.call_wall <= GEX_BREAKOUT_WINDOW_PCT:
                return True, "CALL"
    if prev_gex.put_wall and prev_gex.spot_price > prev_gex.put_wall:
        if price <= prev_gex.put_wall:
            if (prev_gex.put_wall - price) / prev_gex.put_wall <= GEX_BREAKOUT_WINDOW_PCT:
                return True, "PUT"
    return False, ""


async def _score_breakout(
    ticker: str, price: float, gex_levels, cross_side: str,
    prev_gex, result: SignalResult, paused: bool,
) -> SignalResult:
    """
    Breakout signal path. Same 4 microstructure signals as rejection,
    but requires BREAKOUT_MIN_CONFIRMATIONS (3) and BREAKOUT_RVOL_MIN (2.0).
    Skips the entry-window filter — valid any time the market is open.
    """
    direction = "LONG" if cross_side == "CALL" else "SHORT"
    crossed_wall = prev_gex.call_wall if cross_side == "CALL" else prev_gex.put_wall

    # Gate B1: higher RVOL bar
    _, rvol = get_rvol_monitor(ticker).passes_gate()
    result.rvol = rvol
    if rvol is not None and rvol < BREAKOUT_RVOL_MIN:
        result.gated_by = f"BREAKOUT_LOW_RVOL_{rvol:.2f}"
        await _persist(result)
        return result

    confirmations = 0
    score = 0.0
    breakdown: dict = {"signal_type": "BREAKOUT"}

    # Freshness score (0–30): tighter cross = stronger signal
    distance_pct = abs(price - crossed_wall) / crossed_wall
    freshness = max(0.0, 30.0 * (1 - distance_pct / GEX_BREAKOUT_WINDOW_PCT))
    score += freshness
    breakdown["wall_cross_freshness"] = round(freshness, 1)

    # Footprint delta (0–25)
    fp = get_footprint(ticker)
    delta_1m = fp.get_delta_1m()
    delta_5m = fp.get_delta_5m()
    absorption, absorption_side = fp.get_absorption()
    result.footprint_delta_1m = delta_1m
    result.footprint_delta_5m = delta_5m
    result.footprint_absorption = absorption
    result.footprint_absorption_side = absorption_side

    delta_aligned = delta_5m is not None and (
        (direction == "LONG"  and delta_5m > 0) or
        (direction == "SHORT" and delta_5m < 0)
    )
    if delta_aligned:
        fp_score = min(25, abs(delta_5m) / 500_000 * 25) if delta_5m else 15
        score += fp_score
        breakdown["footprint"] = round(fp_score, 1)
        confirmations += 1
    else:
        breakdown["footprint"] = 0

    # L2 imbalance (0–20)
    ob = get_ob(ticker)
    ob_state = ob.get_state()
    result.l2_bid_wall_price = ob_state.bid_wall_price
    result.l2_bid_wall_size  = ob_state.bid_wall_size
    result.l2_ask_wall_price = ob_state.ask_wall_price
    result.l2_ask_wall_size  = ob_state.ask_wall_size
    result.l2_imbalance      = ob_state.imbalance

    if ob_state.imbalance_direction() == direction:
        l2_score = min(20, abs(ob_state.imbalance) * 20)
        score += l2_score
        breakdown["l2_imbalance"] = round(l2_score, 1)
        confirmations += 1
    else:
        breakdown["l2_imbalance"] = 0

    # Large print (0–15)
    flow = options_flow_monitor.get_state(ticker)
    result.large_print_detected = flow.large_print_detected
    result.large_print_side     = flow.large_print_direction
    result.large_print_notional = flow.last_large_print.notional if flow.last_large_print else None

    if flow.is_large_print_fresh() and flow.large_print_direction == direction:
        score += 15
        breakdown["large_print"] = 15
        confirmations += 1
    else:
        breakdown["large_print"] = 0

    # Options sweep (0–10)
    result.options_sweep_detected      = flow.sweep_detected
    result.options_sweep_side          = flow.sweep_direction
    result.options_sweep_strike        = flow.last_sweep.strike if flow.last_sweep else None
    result.options_sweep_vol_oi_ratio  = flow.last_sweep.vol_oi_ratio if flow.last_sweep else None

    if flow.is_sweep_fresh() and flow.sweep_direction == direction:
        score += 10
        breakdown["options_sweep"] = 10
        confirmations += 1
    else:
        breakdown["options_sweep"] = 0

    # VWAP context
    vwap_calc = get_vwap_calc(ticker)
    result.vwap = vwap_calc.get_vwap()
    result.dist_from_vwap_pct = vwap_calc.dist_from_vwap_pct(price)

    result.conviction_score = round(score, 1)
    result.score_breakdown  = breakdown

    if confirmations < BREAKOUT_MIN_CONFIRMATIONS:
        result.gated_by = f"BREAKOUT_INSUF_CONF_{confirmations}/{BREAKOUT_MIN_CONFIRMATIONS}"
        await _persist(result)
        return result

    if score < MIN_CONVICTION_SCORE:
        result.gated_by = f"BREAKOUT_LOW_CONVICTION_{score:.0f}"
        await _persist(result)
        return result

    if paused:
        result.gated_by = "BOT_PAUSED"
        await _persist(result)
        return result

    if should_skip_afternoon_entry():
        result.gated_by = "TOO_CLOSE_TO_EOD"
        await _persist(result)
        return result

    result.gate_passed = True
    result.direction   = direction
    logger.info(
        f"BREAKOUT {ticker} {direction} @ {price:.2f} | "
        f"{'call' if cross_side == 'CALL' else 'put'}_wall={crossed_wall:.2f} "
        f"score={score:.0f} conf={confirmations} rvol={rvol:.2f}"
    )
    await _persist(result)
    return result


async def evaluate_ticker(ticker: str, price: float, gex_levels, prev_gex=None, paused: bool = False) -> SignalResult:
    """
    Run the full gate + scoring logic for one ticker at the current moment.
    gex_levels: GexLevels object (may be None if GEX unavailable).
    Returns a SignalResult — always writes to DB even if gated.
    """
    now_et = datetime.now(ET)
    result = SignalResult(ticker=ticker, ts=now_et, price=price)
    result.time_window = current_window()

    # ── Gate 1: Market conditions ─────────────────────────────────────────────

    if not is_market_open():
        result.gated_by = "MARKET_CLOSED"
        await _persist(result)
        return result

    result.vix_level = get_vix()
    result.vix_regime = get_regime()
    result.is_macro_event = is_macro_event_today() or is_fomc_window()

    if result.is_macro_event:
        result.gated_by = "MACRO_EVENT"
        await _persist(result)
        return result

    result.is_earnings_day = await is_earnings_day(ticker)
    if result.is_earnings_day:
        result.gated_by = "EARNINGS_DAY"
        await _persist(result)
        return result

    if not is_ticker_allowed(ticker):
        result.gated_by = f"VIX_REGIME_{result.vix_regime}"
        await _persist(result)
        return result

    # ── Gate 2: Ticker conditions ─────────────────────────────────────────────

    rvol_ok, rvol = get_rvol_monitor(ticker).passes_gate()
    result.rvol = rvol
    if not rvol_ok:
        result.gated_by = f"LOW_RVOL_{rvol:.2f}" if rvol else "LOW_RVOL"
        await _persist(result)
        return result

    iv_ok, iv_rank = await iv_rank_passes_gate(ticker)
    result.iv_rank = iv_rank
    if not iv_ok:
        result.gated_by = f"IV_RANK_HIGH_{iv_rank:.0f}"
        await _persist(result)
        return result

    # ── Gate 3: GEX structural signal ────────────────────────────────────────

    if gex_levels is None:
        result.gated_by = "NO_GEX_DATA"
        await _persist(result)
        return result

    result.gex_call_wall  = gex_levels.call_wall
    result.gex_put_wall   = gex_levels.put_wall
    result.gex_gamma_flip = gex_levels.gamma_flip
    result.gex_net        = gex_levels.net_gex

    approaching, wall_side = gex_levels.approaching_wall(GEX_PROXIMITY_PCT)
    proximity_pct, _ = gex_levels.proximity_to_nearest_wall()
    result.gex_proximity_pct = proximity_pct
    result.gex_wall_side = wall_side

    if not approaching:
        # Check for wall breakout before giving up
        crossed, cross_side = _detect_wall_breakout(prev_gex, price)
        if crossed:
            return await _score_breakout(
                ticker, price, gex_levels, cross_side, prev_gex, result, paused
            )
        result.gated_by = f"NOT_NEAR_GEX_WALL_{proximity_pct:.3%}"
        await _persist(result)
        return result

    # Direction from GEX wall:
    # approaching put wall from above → expect bounce → LONG
    # approaching call wall from below → expect rejection → SHORT
    # approaching gamma flip → use GEX regime
    if wall_side == "PUT":
        candidate_direction = "LONG"
    elif wall_side == "CALL":
        candidate_direction = "SHORT"
    else:
        # Gamma flip: long if in negative GEX (trending), short if positive (mean-reverting)
        candidate_direction = "LONG" if gex_levels.net_gex < 0 else "SHORT"

    # ── Gate 4: Microstructure confirmations ──────────────────────────────────

    confirmations = 0
    score = 0.0
    breakdown = {}

    # GEX proximity score (0–30): tighter = higher
    proximity_score = max(0, 30 * (1 - proximity_pct / GEX_PROXIMITY_PCT))
    score += proximity_score
    breakdown["gex_proximity"] = round(proximity_score, 1)

    # Footprint delta (0–25)
    fp = get_footprint(ticker)
    delta_1m = fp.get_delta_1m()
    delta_5m = fp.get_delta_5m()
    absorption, absorption_side = fp.get_absorption()
    result.footprint_delta_1m = delta_1m
    result.footprint_delta_5m = delta_5m
    result.footprint_absorption = absorption
    result.footprint_absorption_side = absorption_side

    delta_aligned = delta_5m is not None and (
        (candidate_direction == "LONG"  and delta_5m > 0) or
        (candidate_direction == "SHORT" and delta_5m < 0)
    )
    absorption_aligned = (
        absorption and (
            (candidate_direction == "LONG"  and absorption_side == "BULLISH") or
            (candidate_direction == "SHORT" and absorption_side == "BEARISH")
        )
    )
    if delta_aligned or absorption_aligned:
        fp_score_raw = abs(delta_5m) if delta_5m is not None else 0
        fp_score = min(25, fp_score_raw / 500_000 * 25) if fp_score_raw > 0 else 10
        if absorption_aligned:
            fp_score = min(25, fp_score + 5)
        score += fp_score
        breakdown["footprint"] = round(fp_score, 1)
        confirmations += 1
    else:
        breakdown["footprint"] = 0

    # L2 imbalance (0–20)
    ob = get_ob(ticker)
    ob_state = ob.get_state()
    result.l2_bid_wall_price = ob_state.bid_wall_price
    result.l2_bid_wall_size  = ob_state.bid_wall_size
    result.l2_ask_wall_price = ob_state.ask_wall_price
    result.l2_ask_wall_size  = ob_state.ask_wall_size
    result.l2_imbalance      = ob_state.imbalance

    l2_dir = ob_state.imbalance_direction()
    l2_aligned = (l2_dir == candidate_direction)
    if l2_aligned:
        l2_score = min(20, abs(ob_state.imbalance) * 20)
        score += l2_score
        breakdown["l2_imbalance"] = round(l2_score, 1)
        confirmations += 1
    else:
        breakdown["l2_imbalance"] = 0

    # Large print (0–15)
    flow = options_flow_monitor.get_state(ticker)
    result.large_print_detected = flow.large_print_detected
    result.large_print_side     = flow.large_print_direction
    result.large_print_notional = (
        flow.last_large_print.notional if flow.last_large_print else None
    )

    large_print_aligned = (
        flow.is_large_print_fresh() and
        flow.large_print_direction == candidate_direction
    )
    if large_print_aligned:
        score += 15
        breakdown["large_print"] = 15
        confirmations += 1
    else:
        breakdown["large_print"] = 0

    # Options sweep (0–10)
    result.options_sweep_detected = flow.sweep_detected
    result.options_sweep_side     = flow.sweep_direction
    result.options_sweep_strike   = (
        flow.last_sweep.strike if flow.last_sweep else None
    )
    result.options_sweep_vol_oi_ratio = (
        flow.last_sweep.vol_oi_ratio if flow.last_sweep else None
    )

    sweep_aligned = (
        flow.is_sweep_fresh() and
        flow.sweep_direction == candidate_direction
    )
    if sweep_aligned:
        score += 10
        breakdown["options_sweep"] = 10
        confirmations += 1
    else:
        breakdown["options_sweep"] = 0

    # VWAP context (stored but not gating)
    vwap_calc = get_vwap_calc(ticker)
    result.vwap = vwap_calc.get_vwap()
    result.dist_from_vwap_pct = vwap_calc.dist_from_vwap_pct(price)

    # ── Final decision ────────────────────────────────────────────────────────

    result.conviction_score = round(score, 1)
    result.score_breakdown  = breakdown

    if confirmations < MIN_CONFIRMATIONS:
        result.gated_by = f"INSUFFICIENT_CONFIRMATIONS_{confirmations}/{MIN_CONFIRMATIONS}"
        await _persist(result)
        return result

    if score < MIN_CONVICTION_SCORE:
        result.gated_by = f"LOW_CONVICTION_{score:.0f}"
        await _persist(result)
        return result

    if paused:
        result.gated_by = "BOT_PAUSED"
        await _persist(result)
        return result

    if not is_entry_allowed():
        result.gated_by = "OUTSIDE_ENTRY_WINDOW"
        await _persist(result)
        return result

    if should_skip_afternoon_entry():
        result.gated_by = "TOO_CLOSE_TO_EOD"
        await _persist(result)
        return result

    # Signal fires
    result.gate_passed = True
    result.direction   = candidate_direction
    logger.info(
        f"SIGNAL {ticker} {candidate_direction} | score={score:.0f} "
        f"wall={wall_side} confirmations={confirmations}"
    )
    await _persist(result)
    return result


async def _persist(result: SignalResult) -> None:
    """Write SignalTick and stub SignalOutcome to database."""
    async with AsyncSessionLocal() as session:
        tick = SignalTick(
            ts=result.ts,
            ticker=result.ticker,
            price=result.price,
            gex_call_wall=result.gex_call_wall,
            gex_put_wall=result.gex_put_wall,
            gex_gamma_flip=result.gex_gamma_flip,
            gex_net=result.gex_net,
            gex_proximity_pct=result.gex_proximity_pct,
            gex_wall_side=result.gex_wall_side,
            footprint_delta_1m=result.footprint_delta_1m,
            footprint_delta_5m=result.footprint_delta_5m,
            footprint_absorption=result.footprint_absorption,
            footprint_absorption_side=result.footprint_absorption_side,
            l2_bid_wall_price=result.l2_bid_wall_price,
            l2_bid_wall_size=result.l2_bid_wall_size,
            l2_ask_wall_price=result.l2_ask_wall_price,
            l2_ask_wall_size=result.l2_ask_wall_size,
            l2_imbalance=result.l2_imbalance,
            options_sweep_detected=result.options_sweep_detected,
            options_sweep_side=result.options_sweep_side,
            options_sweep_strike=result.options_sweep_strike,
            options_sweep_vol_oi_ratio=result.options_sweep_vol_oi_ratio,
            large_print_detected=result.large_print_detected,
            large_print_side=result.large_print_side,
            large_print_notional=result.large_print_notional,
            iv_rank=result.iv_rank,
            rvol=result.rvol,
            vwap=result.vwap,
            dist_from_vwap_pct=result.dist_from_vwap_pct,
            vix_level=result.vix_level,
            vix_regime=result.vix_regime,
            is_earnings_day=result.is_earnings_day,
            is_macro_event=result.is_macro_event,
            time_window=result.time_window,
            direction=result.direction,
            conviction_score=result.conviction_score,
            score_breakdown=json.dumps(result.score_breakdown),
            gate_passed=result.gate_passed,
            gated_by=result.gated_by,
            signal_fired=False,   # set to True by paper_trader after opening position
            option_symbol=result.option_symbol,
            option_strike=result.option_strike,
            option_expiry=result.option_expiry,
            option_type=result.option_type,
        )
        session.add(tick)
        await session.flush()

        # Stub outcome row so outcome_filler knows to fill it later
        if result.gate_passed and result.direction:
            session.add(SignalOutcome(
                signal_tick_id=tick.id,
                ticker=result.ticker,
                entry_price=result.price,
                direction=result.direction,
            ))

        await session.commit()
        result._tick_id = tick.id   # carry ID for paper trader
