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
    contracts: int
    premium_paid: float
    stop_price: float           # premium level for stop loss
    target_2r_price: float
    target_3r_price: float
    entry_underlying: float
    entry_ts: datetime
    signal_tick_id: int
    conviction_score: float = 0.0
    vix_at_entry: float = 0.0
    iv_rank_at_entry: float = 0.0
    time_window: str = ""


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

        # Simulate buying at ask + slippage
        fill_price = ask + slippage
        if fill_price <= 0:
            return None

        # Position sizing
        risk_usd = self._account_balance * 0.0025
        from backend.config import settings
        vix_mod = 1.0
        from backend.strategy.vix_regime import size_modifier
        vix_mod = size_modifier()
        risk_usd *= vix_mod

        max_premium = risk_usd / settings.option_stop_loss_pct
        contracts = max(1, int(max_premium / (fill_price * 100)))
        premium_paid = fill_price * contracts * 100
        commission = contracts * COMMISSION_PER_CONTRACT

        stop_price    = fill_price * (1 - settings.option_stop_loss_pct)
        target_2r     = fill_price * (1 + settings.option_stop_loss_pct * 2)
        target_3r     = fill_price * (1 + settings.option_stop_loss_pct * 3)

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
        Called every 5s. Checks SL/TP, EOD force-close, and key-level early exit.

        option_quotes: dict[option_symbol → {bid, ask, delta, iv, ...}]
        micro: dict[ticker → {price, footprint_delta_1m, l2_imbalance, key_levels}]
        """
        force_close = is_hard_close_time()
        for trade_id in list(self._positions.keys()):
            pos = self._positions[trade_id]
            quote = option_quotes.get(pos.option_symbol, {})
            current_bid = float(quote.get("bid", 0) or 0)
            current_ask = float(quote.get("ask", 0) or 0)
            mid = (current_bid + current_ask) / 2 if current_bid and current_ask else 0

            if mid <= 0:
                if force_close:
                    await self._close_position(pos, pos.entry_price * 0.5,
                                               "EOD_FORCE", quote)
                continue

            reason = None
            if force_close:
                reason = "EOD_FORCE"
            elif mid <= pos.stop_price:
                reason = "STOP_LOSS"
            elif mid >= pos.target_3r_price:
                reason = "TARGET_3R"
            elif mid >= pos.target_2r_price:
                reason = "TARGET_2R"
            else:
                # Key-level early exit: approaching opposing structural level
                # with confirming microstructure → get out before reversal
                if micro:
                    ticker_micro = micro.get(pos.ticker, {})
                    spot   = ticker_micro.get("price", 0)
                    delta  = ticker_micro.get("footprint_delta_1m")
                    imbal  = ticker_micro.get("l2_imbalance")
                    levels = ticker_micro.get("key_levels")

                    if spot and levels:
                        if pos.direction == "LONG":
                            exit_now, exit_reason = levels.should_exit_long(spot, delta, imbal)
                        else:
                            exit_now, exit_reason = levels.should_exit_short(spot, delta, imbal)

                        if exit_now:
                            reason = f"KEY_LEVEL_EXIT"
                            logger.info(f"Key-level early exit {pos.ticker}: {exit_reason}")

            if reason:
                await self._close_position(pos, mid, reason, quote)

    async def _close_position(self, pos: OpenPosition, current_mid: float,
                              reason: str, quote: dict) -> None:
        ticker = pos.ticker
        slippage = SLIPPAGE.get(ticker, SLIPPAGE["DEFAULT"])

        # Simulate selling at bid - slippage
        bid = float(quote.get("bid", current_mid) or current_mid)
        fill_price = max(0.01, bid - slippage)

        gross_pnl = (fill_price - pos.entry_price) * pos.contracts * 100
        slippage_cost = slippage * pos.contracts * 100 * 2   # entry + exit
        commission_cost = pos.contracts * COMMISSION_PER_CONTRACT * 2
        net_pnl = gross_pnl - slippage_cost - commission_cost

        exit_ts = datetime.now(ET)

        async with AsyncSessionLocal() as session:
            await session.execute(
                update(PaperTrade)
                .where(PaperTrade.id == pos.trade_id)
                .values(
                    status="CLOSED",
                    exit_ts=exit_ts,
                    exit_underlying_price=None,   # updated from quote if available
                    option_exit_price=fill_price,
                    exit_reason=reason,
                    delta_at_exit=quote.get("delta"),
                    iv_at_exit=quote.get("implied_volatility"),
                    gross_pnl_usd=gross_pnl,
                    slippage_usd=slippage_cost,
                    commission_usd=commission_cost,
                    net_pnl_usd=net_pnl,
                )
            )
            # Update bot state
            result = await session.execute(select(BotState).where(BotState.id == 1))
            state = result.scalar_one_or_none()
            if state:
                state.paper_account_balance += net_pnl
                state.total_trades += 1
                if net_pnl > 0:
                    state.winning_trades += 1
                state.total_net_pnl_usd += net_pnl
                self._account_balance = state.paper_account_balance

                # Check circuit breaker
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
        pct = net_pnl / pos.premium_paid * 100 if pos.premium_paid else 0
        logger.info(
            f"PAPER TRADE CLOSED: {pos.ticker} {pos.direction} [{reason}] "
            f"| entry=${pos.entry_price:.2f} exit=${fill_price:.2f} "
            f"| net_pnl=${net_pnl:+.0f} ({pct:+.1f}%)"
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
