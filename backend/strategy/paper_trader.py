"""
Paper trading engine.

Manages fake positions with realistic slippage, commission, and EOD force-close.

Risk math:
  account_size       = $100,000 (paper)
  risk_per_trade     = 0.25% = $250
  option_stop_loss   = -30% of premium
  position_size      = $250 / 0.30 = $833 in premium
  contracts          = floor($833 / (ask_price × 100))

Stop:   option premium falls 30% below entry → exit
Target: option premium rises 60% (2R) or 90% (3R) above entry → exit
EOD:    all positions force-closed at 3:58 PM ET regardless of P&L

Slippage model:
  SPY/QQQ 0DTE: buy at ask, sell at bid
  Mag7 weekly:  buy at ask + $0.02, sell at bid - $0.02
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from backend.config import (
    ET, ALL_TICKERS, ETFS, MAG7,
    SLIPPAGE, COMMISSION_PER_CONTRACT,
    MAX_OPEN_POSITIONS, MAX_TOTAL_PREMIUM_PCT, DAILY_CIRCUIT_BREAKER_PCT,
    L2_IMBALANCE_THRESHOLD,
    TP1_PNL_PCT, TP1_SELL_FRACTION, TP2_PNL_PCT, TP2_TOTAL_SOLD_FRAC,
    TRAILING_STOP_FACTOR, OPPOSING_EXIT_COUNT,
)
from backend.database import AsyncSessionLocal, PaperTrade, BotState, SignalTick
from backend.strategy.time_filter import is_hard_close_time
from backend.data.schwab_rest import get_options_chain
from sqlalchemy import select, update

logger = logging.getLogger(__name__)


@dataclass
class OpenPosition:
    trade_id: int
    ticker: str
    direction: str
    option_symbol: str
    option_strike: float
    option_expiry: str
    option_type: str
    entry_price: float          # per-share premium paid
    contracts: int              # original contract count (never changes)
    premium_paid: float
    stop_price: float           # current stop level (moves to BE after TP1, trails after TP2)
    target_2r_price: float
    target_3r_price: float
    entry_underlying: float
    entry_ts: datetime
    signal_tick_id: int
    conviction_score: float = 0.0
    vix_at_entry: float = 0.0
    iv_rank_at_entry: float = 0.0
    time_window: str = ""
    # Partial exit tracking
    original_contracts: int = 0
    contracts_remaining: int = 0
    tp1_hit: bool = False
    tp2_hit: bool = False
    stop_at_breakeven: bool = False
    peak_unrealized_pct: float = 0.0     # highest (mid/entry - 1)*100 seen
    realized_partials_usd: float = 0.0   # cumulative $ booked via partial closes


class PaperTrader:

    def __init__(self):
        self._positions: dict[int, OpenPosition] = {}   # trade_id → position
        self._account_balance: float = 100_000.0
        self._day_start_balance: float = 100_000.0
        self._circuit_breaker: bool = False
        self._circuit_breaker_reason: str = ""

    async def sync_state(self) -> None:
        """Load account state from DB on startup."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(BotState).where(BotState.id == 1))
            state = result.scalar_one_or_none()
            if state:
                self._account_balance   = state.paper_account_balance
                self._day_start_balance = state.paper_day_start_balance
                self._circuit_breaker   = state.circuit_breaker_active

            # Reload any open positions from previous run
            open_trades = await session.execute(
                select(PaperTrade).where(PaperTrade.status == "OPEN")
            )
            for trade in open_trades.scalars().all():
                pos = OpenPosition(
                    trade_id=trade.id,
                    ticker=trade.ticker,
                    direction=trade.direction,
                    option_symbol=trade.option_symbol,
                    option_strike=trade.option_strike,
                    option_expiry=trade.option_expiry,
                    option_type=trade.option_type,
                    entry_price=trade.option_entry_price,
                    contracts=trade.contracts,
                    premium_paid=trade.premium_paid,
                    stop_price=trade.stop_price,
                    target_2r_price=trade.target_2r_price,
                    target_3r_price=trade.target_3r_price,
                    entry_underlying=trade.entry_underlying_price,
                    entry_ts=trade.entry_ts,
                    signal_tick_id=trade.signal_tick_id,
                    conviction_score=trade.conviction_score_at_entry or 0,
                    vix_at_entry=trade.vix_at_entry or 0,
                    iv_rank_at_entry=trade.iv_rank_at_entry or 0,
                    time_window=trade.time_window or "",
                    original_contracts=trade.original_contracts or trade.contracts,
                    contracts_remaining=trade.contracts_remaining or trade.contracts,
                    tp1_hit=bool(trade.tp1_hit),
                    tp2_hit=bool(trade.tp2_hit),
                    stop_at_breakeven=bool(trade.stop_at_breakeven),
                    realized_partials_usd=trade.realized_partials_usd or 0.0,
                )
                self._positions[trade.id] = pos

        logger.info(
            f"Paper trader loaded: balance=${self._account_balance:,.0f}, "
            f"open_positions={len(self._positions)}"
        )

    # ── Position opening ──────────────────────────────────────────────────────

    def can_open_position(self) -> tuple[bool, str]:
        """Check portfolio-level guards before opening."""
        if self._circuit_breaker:
            return False, "CIRCUIT_BREAKER"
        if len(self._positions) >= MAX_OPEN_POSITIONS:
            return False, f"MAX_POSITIONS_{MAX_OPEN_POSITIONS}"
        total_at_risk = sum(
            p.premium_paid for p in self._positions.values()
        )
        max_at_risk = self._account_balance * MAX_TOTAL_PREMIUM_PCT
        if total_at_risk >= max_at_risk:
            return False, f"MAX_PREMIUM_RISK_{total_at_risk:.0f}"
        return True, ""

    async def try_open(
        self, signal, chain_data: dict, underlying_quote: dict
    ) -> Optional[OpenPosition]:
        """
        Attempt to open a paper position from a fired signal.
        signal: SignalResult with gate_passed=True
        chain_data: raw options chain from Schwab REST
        underlying_quote: L1 quote dict
        Returns OpenPosition if opened, None if blocked.
        """
        can_open, reason = self.can_open_position()
        if not can_open:
            logger.info(f"Paper trade blocked for {signal.ticker}: {reason}")
            return None

        # Select the option contract
        option = _select_option(signal.ticker, signal.direction, chain_data)
        if not option:
            logger.warning(f"No suitable option found for {signal.ticker} {signal.direction}")
            return None

        ticker = signal.ticker
        ask = option["ask"]
        bid = option["bid"]
        slippage = SLIPPAGE.get(ticker, SLIPPAGE["DEFAULT"])

        # Simulate buying at ask (SPY/QQQ) or ask + extra slippage (Mag7).
        # Slippage is tracked as a separate P&L line item — NOT baked into fill_price.
        fill_price = ask
        if fill_price <= 0:
            return None

        # Position sizing
        from backend.config import settings
        from backend.strategy.vix_regime import size_modifier
        risk_usd = self._account_balance * settings.risk_per_trade_pct
        risk_usd *= size_modifier()

        max_premium = risk_usd / settings.option_stop_loss_pct
        contracts = max(1, int(max_premium / (fill_price * 100)))
        premium_paid = fill_price * contracts * 100
        commission = contracts * COMMISSION_PER_CONTRACT

        stop_price = fill_price * (1 - settings.option_stop_loss_pct)   # hard -30% initial
        target_2r  = fill_price * (1 + settings.option_stop_loss_pct * 2)
        target_3r  = fill_price * (1 + settings.option_stop_loss_pct * 3)

        entry_ts = datetime.now(ET)

        async with AsyncSessionLocal() as session:
            trade = PaperTrade(
                signal_tick_id=getattr(signal, "_tick_id", 0),
                ticker=ticker,
                direction=signal.direction,
                status="OPEN",
                entry_ts=entry_ts,
                entry_underlying_price=signal.price,
                option_symbol=option["symbol"],
                option_strike=option["strike"],
                option_expiry=option["expiry"],
                option_type=option["type"],
                option_entry_price=fill_price,
                contracts=contracts,
                premium_paid=premium_paid,
                delta_at_entry=option.get("delta"),
                gamma_at_entry=option.get("gamma"),
                theta_at_entry=option.get("theta"),
                vega_at_entry=option.get("vega"),
                iv_at_entry=option.get("iv"),
                stop_price=stop_price,
                target_2r_price=target_2r,
                target_3r_price=target_3r,
                conviction_score_at_entry=signal.conviction_score,
                vix_at_entry=signal.vix_level or 0,
                iv_rank_at_entry=signal.iv_rank or 0,
                time_window=signal.time_window,
                original_contracts=contracts,
                contracts_remaining=contracts,
                realized_partials_usd=0.0,
                tp1_hit=False,
                tp2_hit=False,
                stop_at_breakeven=False,
            )
            session.add(trade)
            await session.flush()
            trade_id = trade.id

            # Mark signal tick as fired
            tick_id = getattr(signal, "_tick_id", None)
            if tick_id:
                await session.execute(
                    update(SignalTick)
                    .where(SignalTick.id == tick_id)
                    .values(signal_fired=True, option_symbol=option["symbol"],
                            option_strike=option["strike"],
                            option_expiry=option["expiry"],
                            option_type=option["type"])
                )
            await session.commit()

        pos = OpenPosition(
            trade_id=trade_id,
            ticker=ticker,
            direction=signal.direction,
            option_symbol=option["symbol"],
            option_strike=option["strike"],
            option_expiry=option["expiry"],
            option_type=option["type"],
            entry_price=fill_price,
            contracts=contracts,
            premium_paid=premium_paid,
            stop_price=stop_price,
            target_2r_price=target_2r,
            target_3r_price=target_3r,
            entry_underlying=signal.price,
            entry_ts=entry_ts,
            signal_tick_id=getattr(signal, "_tick_id", 0),
            conviction_score=signal.conviction_score,
            vix_at_entry=signal.vix_level or 0,
            iv_rank_at_entry=signal.iv_rank or 0,
            time_window=signal.time_window,
            original_contracts=contracts,
            contracts_remaining=contracts,
        )
        self._positions[trade_id] = pos

        logger.info(
            f"PAPER TRADE OPENED: {ticker} {signal.direction} "
            f"| {contracts}x {option['symbol']} @ ${fill_price:.2f} "
            f"| stop=${stop_price:.2f} target2R=${target_2r:.2f} "
            f"| premium=${premium_paid:.0f}"
        )
        return pos

    # ── Position monitoring ───────────────────────────────────────────────────

    async def check_positions(self, option_quotes: dict, micro: dict = None) -> None:
        """
        Called every 1s. Implements TP ladder:
          Phase 1 (pre-TP1): hard -30% stop, exit at +30% to sell 10% + move stop to BE
          Phase 2 (post-TP1, pre-TP2): breakeven stop, exit at +50% to sell to 75% total sold
          Phase 3 (post-TP2): trailing stop at peak × 50%, exit on 2+ opposing signals

        option_quotes: dict[option_symbol → {bid, ask, delta, iv, ...}]
        micro: dict[ticker → {price, footprint_*, l2_*, sweep_*, key_levels}]
        """
        force_close = is_hard_close_time()
        for trade_id in list(self._positions.keys()):
            pos = self._positions.get(trade_id)
            if pos is None:
                continue

            quote = option_quotes.get(pos.option_symbol, {})
            bid = float(quote.get("bid", 0) or 0)
            ask = float(quote.get("ask", 0) or 0)
            mid = (bid + ask) / 2 if bid and ask else 0

            if mid <= 0:
                if force_close:
                    await self._close_position(pos, pos.entry_price * 0.5, "EOD_FORCE", quote)
                continue

            # Track peak unrealized
            unrealized_pct = (mid / pos.entry_price - 1) * 100
            if unrealized_pct > pos.peak_unrealized_pct:
                pos.peak_unrealized_pct = unrealized_pct

            # EOD: always exit all remaining
            if force_close:
                await self._close_position(pos, mid, "EOD_FORCE", quote)
                continue

            ticker_micro = (micro or {}).get(pos.ticker, {})

            if not pos.tp1_hit:
                # ── Phase 1: hard -30% stop ──────────────────────────────────
                if mid <= pos.stop_price:
                    await self._close_position(pos, mid, "STOP_LOSS", quote)
                    continue

                if unrealized_pct >= TP1_PNL_PCT * 100:
                    # Sell TP1 tranche — 10% of original, min 1, keep at least 1 remaining
                    to_sell = max(1, round(pos.original_contracts * TP1_SELL_FRACTION))
                    to_sell = min(to_sell, pos.contracts_remaining - 1)
                    bid_fill = max(0.01, float(quote.get("bid", mid) or mid))
                    pos.tp1_hit = True
                    pos.stop_at_breakeven = True
                    pos.stop_price = pos.entry_price  # move stop to breakeven
                    if to_sell > 0:
                        await self._partial_close(pos, to_sell, bid_fill, quote, "TP1")
                    else:
                        await self._close_position(pos, mid, "TP1_FULL", quote)
                    continue

                if _should_key_level_exit(pos, ticker_micro):
                    await self._close_position(pos, mid, "KEY_LEVEL_EXIT", quote)

            elif not pos.tp2_hit:
                # ── Phase 2: breakeven stop ──────────────────────────────────
                if mid <= pos.stop_price:
                    await self._close_position(pos, mid, "STOP_LOSS_BE", quote)
                    continue

                if unrealized_pct >= TP2_PNL_PCT * 100:
                    # Sell enough to reach 75% total sold
                    sold_so_far = pos.original_contracts - pos.contracts_remaining
                    target_sold = round(pos.original_contracts * TP2_TOTAL_SOLD_FRAC)
                    to_sell = max(0, target_sold - sold_so_far)
                    to_sell = min(to_sell, pos.contracts_remaining - 1)
                    bid_fill = max(0.01, float(quote.get("bid", mid) or mid))
                    pos.tp2_hit = True
                    if to_sell > 0:
                        await self._partial_close(pos, to_sell, bid_fill, quote, "TP2")
                    else:
                        # Nothing to sell but mark TP2 hit; persist it
                        async with AsyncSessionLocal() as sess:
                            await sess.execute(
                                update(PaperTrade)
                                .where(PaperTrade.id == pos.trade_id)
                                .values(tp2_hit=True)
                            )
                            await sess.commit()
                    continue

                if _count_opposing_signals(pos, ticker_micro) >= OPPOSING_EXIT_COUNT:
                    await self._close_position(pos, mid, "OPPOSING_SIGNALS", quote)

            else:
                # ── Phase 3: trailing stop on remaining ──────────────────────
                trailing_price = pos.entry_price * (
                    1 + pos.peak_unrealized_pct * TRAILING_STOP_FACTOR / 100
                )
                pos.stop_price = trailing_price  # update in-memory for dashboard display

                if mid <= trailing_price:
                    await self._close_position(pos, mid, "TRAILING_STOP", quote)
                elif _count_opposing_signals(pos, ticker_micro) >= OPPOSING_EXIT_COUNT:
                    await self._close_position(pos, mid, "OPPOSING_SIGNALS", quote)

    async def _partial_close(
        self, pos: OpenPosition, contracts_to_sell: int,
        fill_price: float, quote: dict, reason: str
    ) -> None:
        """Sell a subset of contracts. Updates in-memory state and DB; credits account."""
        ticker = pos.ticker
        slippage = SLIPPAGE.get(ticker, SLIPPAGE["DEFAULT"])

        gross = (fill_price - pos.entry_price) * contracts_to_sell * 100
        slip  = slippage * contracts_to_sell * 100 * 2  # proportional round-trip share
        comm  = contracts_to_sell * COMMISSION_PER_CONTRACT * 2
        net   = gross - slip - comm

        pos.contracts_remaining  -= contracts_to_sell
        pos.realized_partials_usd += net

        update_vals: dict = dict(
            contracts_remaining=pos.contracts_remaining,
            realized_partials_usd=pos.realized_partials_usd,
            tp1_hit=pos.tp1_hit,
            tp2_hit=pos.tp2_hit,
            stop_price=pos.stop_price,
            stop_at_breakeven=pos.stop_at_breakeven,
        )
        if reason == "TP1":
            update_vals["tp1_contracts_sold"] = contracts_to_sell
            update_vals["tp1_price"] = fill_price
        elif reason == "TP2":
            update_vals["tp2_contracts_sold"] = contracts_to_sell
            update_vals["tp2_price"] = fill_price

        async with AsyncSessionLocal() as session:
            await session.execute(
                update(PaperTrade).where(PaperTrade.id == pos.trade_id).values(**update_vals)
            )
            result = await session.execute(select(BotState).where(BotState.id == 1))
            state = result.scalar_one_or_none()
            if state:
                state.paper_account_balance += net
                self._account_balance = state.paper_account_balance
            await session.commit()

        logger.info(
            f"PARTIAL [{reason}]: {pos.ticker} {pos.direction} "
            f"sold {contracts_to_sell}x @ ${fill_price:.2f} | net=${net:+.0f} "
            f"| remaining={pos.contracts_remaining}/{pos.original_contracts} "
            f"| stop now=${pos.stop_price:.2f}"
        )

    async def _close_position(self, pos: OpenPosition, current_mid: float,
                              reason: str, quote: dict) -> None:
        """Close all remaining contracts. Total trade P&L = partials + this leg."""
        ticker = pos.ticker
        slippage = SLIPPAGE.get(ticker, SLIPPAGE["DEFAULT"])

        bid = float(quote.get("bid", current_mid) or current_mid)
        fill_price = max(0.01, bid)
        contracts = pos.contracts_remaining if pos.contracts_remaining > 0 else pos.contracts

        gross_pnl    = (fill_price - pos.entry_price) * contracts * 100
        slippage_cost = slippage * contracts * 100 * 2
        commission_cost = contracts * COMMISSION_PER_CONTRACT * 2
        net_pnl_leg  = gross_pnl - slippage_cost - commission_cost

        # Total trade P&L across all partial closes + this final leg
        total_net_pnl = pos.realized_partials_usd + net_pnl_leg

        exit_ts = datetime.now(ET)

        async with AsyncSessionLocal() as session:
            await session.execute(
                update(PaperTrade)
                .where(PaperTrade.id == pos.trade_id)
                .values(
                    status="CLOSED",
                    exit_ts=exit_ts,
                    option_exit_price=fill_price,
                    exit_reason=reason,
                    contracts_remaining=0,
                    delta_at_exit=quote.get("delta"),
                    iv_at_exit=quote.get("implied_volatility"),
                    gross_pnl_usd=gross_pnl,
                    slippage_usd=slippage_cost,
                    commission_usd=commission_cost,
                    net_pnl_usd=total_net_pnl,
                )
            )
            result = await session.execute(select(BotState).where(BotState.id == 1))
            state = result.scalar_one_or_none()
            if state:
                state.paper_account_balance += net_pnl_leg  # partials already credited
                state.total_trades += 1
                if total_net_pnl > 0:
                    state.winning_trades += 1
                state.total_net_pnl_usd += total_net_pnl
                self._account_balance = state.paper_account_balance

                day_loss = (state.paper_day_start_balance - state.paper_account_balance)
                day_loss_pct = day_loss / state.paper_day_start_balance
                if day_loss_pct >= DAILY_CIRCUIT_BREAKER_PCT:
                    state.circuit_breaker_active = True
                    state.circuit_breaker_reason = (
                        f"Daily loss {day_loss_pct:.1%} exceeded "
                        f"{DAILY_CIRCUIT_BREAKER_PCT:.1%} threshold"
                    )
                    self._circuit_breaker = True
                    logger.warning(f"CIRCUIT BREAKER ACTIVATED: {state.circuit_breaker_reason}")

            await session.commit()

        del self._positions[pos.trade_id]
        pct = total_net_pnl / pos.premium_paid * 100 if pos.premium_paid else 0
        logger.info(
            f"PAPER TRADE CLOSED: {pos.ticker} {pos.direction} [{reason}] "
            f"| entry=${pos.entry_price:.2f} exit=${fill_price:.2f} "
            f"| partials=${pos.realized_partials_usd:+.0f} final=${net_pnl_leg:+.0f} "
            f"total=${total_net_pnl:+.0f} ({pct:+.1f}%)"
        )

    async def reset_day(self) -> None:
        """Reset daily tracking at start of each session."""
        self._circuit_breaker = False
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(BotState).where(BotState.id == 1))
            state = result.scalar_one_or_none()
            if state:
                state.paper_day_start_balance = state.paper_account_balance
                state.circuit_breaker_active = False
                state.circuit_breaker_reason = None
                self._day_start_balance = state.paper_account_balance
            await session.commit()

    @property
    def open_position_count(self) -> int:
        return len(self._positions)

    @property
    def positions(self) -> dict:
        return dict(self._positions)

    @property
    def account_balance(self) -> float:
        return self._account_balance

    @property
    def circuit_breaker_active(self) -> bool:
        return self._circuit_breaker


# ── Opposing-signal helpers ───────────────────────────────────────────────────

def _should_key_level_exit(pos: OpenPosition, ticker_micro: dict) -> bool:
    spot   = ticker_micro.get("price", 0)
    delta  = ticker_micro.get("footprint_delta_1m")
    imbal  = ticker_micro.get("l2_imbalance")
    levels = ticker_micro.get("key_levels")
    if not (spot and levels):
        return False
    if pos.direction == "LONG":
        exit_now, _ = levels.should_exit_long(spot, delta, imbal)
    else:
        exit_now, _ = levels.should_exit_short(spot, delta, imbal)
    return exit_now


def _count_opposing_signals(pos: OpenPosition, ticker_micro: dict) -> int:
    """Count how many microstructure signals oppose the current position direction."""
    count = 0
    direction = pos.direction

    # 1. Footprint absorption against direction
    if ticker_micro.get("footprint_absorption"):
        abs_side = ticker_micro.get("footprint_absorption_side")
        if direction == "LONG" and abs_side == "BEARISH":
            count += 1
        elif direction == "SHORT" and abs_side == "BULLISH":
            count += 1

    # 2. L2 imbalance heavily against direction
    l2_dir   = ticker_micro.get("l2_imbalance_direction")
    l2_imbal = abs(ticker_micro.get("l2_imbalance") or 0)
    if l2_imbal >= L2_IMBALANCE_THRESHOLD:
        if direction == "LONG" and l2_dir == "SHORT":
            count += 1
        elif direction == "SHORT" and l2_dir == "LONG":
            count += 1

    # 3. Fresh sweep against direction
    if ticker_micro.get("sweep_fresh"):
        sweep_dir = ticker_micro.get("sweep_direction")
        if direction == "LONG" and sweep_dir == "SHORT":
            count += 1
        elif direction == "SHORT" and sweep_dir == "LONG":
            count += 1

    return count


# ── Option selection ──────────────────────────────────────────────────────────

def _select_option(ticker: str, direction: str, chain_data: dict) -> Optional[dict]:
    """
    Pick the best-fit option contract for a directional signal.
    Target: delta ~0.40 (slight OTM for leverage, not too far OTM).
    Selects nearest expiry (0DTE for ETFs, nearest weekly for Mag7).
    """
    opt_type = "CALL" if direction == "LONG" else "PUT"
    target_delta = 0.40 if direction == "LONG" else -0.40

    date_map = chain_data.get(
        "callExpDateMap" if opt_type == "CALL" else "putExpDateMap", {}
    )
    if not date_map:
        return None

    # Pick nearest expiry
    nearest_date = sorted(date_map.keys())[0]
    strikes = date_map[nearest_date]
    expiry_str = nearest_date.split(":")[0]   # "YYYY-MM-DD:N" → "YYYY-MM-DD"

    # Find strike closest to target delta
    best = None
    best_delta_dist = float("inf")

    for strike_str, contracts in strikes.items():
        strike = float(strike_str.split(":")[0])
        for c in contracts:
            delta = c.get("delta", 0) or 0
            bid   = c.get("bid",   0) or 0
            ask   = c.get("ask",   0) or 0
            sym   = c.get("symbol", "")
            if bid <= 0 or ask <= 0:
                continue
            # Reject contracts with a spread wider than 25% of ask —
            # wide spreads at the open would simulate an immediate unrealistic loss.
            if (ask - bid) / ask > 0.25:
                continue
            dist = abs(abs(delta) - abs(target_delta))
            if dist < best_delta_dist:
                best_delta_dist = dist
                best = {
                    "symbol": sym,
                    "strike": strike,
                    "expiry": expiry_str,
                    "type": opt_type,
                    "bid": bid,
                    "ask": ask,
                    "delta": delta,
                    "gamma": c.get("gamma"),
                    "theta": c.get("theta"),
                    "vega": c.get("vega"),
                    "iv": c.get("volatility"),
                }

    return best


# Singleton
paper_trader = PaperTrader()
