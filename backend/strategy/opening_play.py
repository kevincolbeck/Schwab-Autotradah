"""
Pre-market opening play analyzer (9:25–9:29 AM ET).

Logic:
  1. Tracks price snapshot at 9:25 AM for each ticker.
  2. At 9:29:30 AM, evaluates each ticker for an "opening play" — a high-conviction
     directional bet to be executed exactly at 9:30 AM market open.
  3. Scoring uses gap direction, last-5-minute momentum, L2 imbalance, and GEX walls.
  4. Queued plays are handed to paper_trader at 9:30 AM by bot_engine.

Signals used (all pre-market):
  - Gap %: vs prev day close — direction and magnitude
  - PM momentum (9:25→9:29 price delta): confirming or fading the gap
  - L2 imbalance: heavy bids = bullish, heavy asks = bearish
  - GEX wall proximity: wall blocking gap direction = strongly negative

Conviction thresholds:
  - ≥ 45: opening play queued for execution at open
  - < 45: no opening play for this ticker today
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from backend.config import ET, ALL_TICKERS

logger = logging.getLogger(__name__)

# Minimum conviction to queue an opening play
MIN_OPENING_PLAY_CONVICTION = 45

# Gap thresholds
GAP_STRONG_PCT = 0.007      # 0.7% — strong directional bias
GAP_MODERATE_PCT = 0.003    # 0.3% — moderate bias
GAP_FLAT_PCT = 0.001        # below this = too flat for gap bias

# GEX wall proximity — wall within this distance of current price is "blocking"
GEX_WALL_BLOCKING_PCT = 0.005   # 0.5%


@dataclass
class OpeningPlayState:
    ticker: str
    direction: Optional[str] = None      # "LONG" | "SHORT" | None
    conviction: int = 0
    gap_pct: Optional[float] = None      # signed: positive = gap up
    pm_momentum_pct: Optional[float] = None   # price change 9:25→9:29
    l2_direction: Optional[str] = None
    gex_blocking: bool = False
    queued: bool = False    # True after assessment locks in at ~9:29:30
    fired: bool = False     # True after trade executed at 9:30

    # For momentum tracking
    price_at_925: Optional[float] = None
    price_at_929: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "conviction": self.conviction,
            "gap_pct": round(self.gap_pct, 4) if self.gap_pct is not None else None,
            "pm_momentum_pct": round(self.pm_momentum_pct, 4) if self.pm_momentum_pct is not None else None,
            "l2_direction": self.l2_direction,
            "gex_blocking": self.gex_blocking,
            "queued": self.queued,
            "fired": self.fired,
        }


class OpeningPlayAnalyzer:
    """Singleton — manages opening play state across all tickers."""

    def __init__(self) -> None:
        self._states: dict[str, OpeningPlayState] = {}
        self._day: Optional[str] = None

    def _ensure_today(self) -> None:
        today = datetime.now(ET).strftime("%Y-%m-%d")
        if self._day != today:
            self._day = today
            self._states = {t: OpeningPlayState(ticker=t) for t in ALL_TICKERS}

    def snapshot_925(self, ticker: str, price: float) -> None:
        """Call at 9:25 AM to record the momentum baseline price."""
        self._ensure_today()
        if ticker in self._states:
            self._states[ticker].price_at_925 = price
            logger.info(f"OpeningPlay: {ticker} 9:25 snapshot ${price:.2f}")

    def evaluate(
        self,
        ticker: str,
        current_price: float,
        prev_close: Optional[float],
        l2_imbalance: Optional[float],
        l2_direction: Optional[str],   # "LONG" | "SHORT" | None
        gex_call_wall: Optional[float],
        gex_put_wall: Optional[float],
        iv_rank: Optional[float],
    ) -> OpeningPlayState:
        """
        Called at ~9:29:30 AM. Scores the ticker and queues a play if conviction >= threshold.
        Returns the updated state.
        """
        self._ensure_today()
        state = self._states.get(ticker, OpeningPlayState(ticker=ticker))
        self._states[ticker] = state

        if state.queued or state.fired:
            return state  # already assessed today

        state.price_at_929 = current_price

        # ── Gap calculation ───────────────────────────────────────────────────
        gap_pct: Optional[float] = None
        if prev_close and prev_close > 0:
            gap_pct = (current_price - prev_close) / prev_close
        state.gap_pct = gap_pct

        # ── Momentum (9:25 → 9:29) ────────────────────────────────────────────
        pm_momentum: Optional[float] = None
        if state.price_at_925 and state.price_at_925 > 0:
            pm_momentum = (current_price - state.price_at_925) / state.price_at_925
        state.pm_momentum_pct = pm_momentum

        # ── Determine candidate direction ─────────────────────────────────────
        candidate: Optional[str] = None

        if gap_pct is not None:
            if gap_pct > GAP_MODERATE_PCT:
                candidate = "LONG"
            elif gap_pct < -GAP_MODERATE_PCT:
                candidate = "SHORT"

        # If no strong gap, use L2 direction as the candidate
        if candidate is None and l2_direction:
            candidate = l2_direction

        if candidate is None:
            # No clear direction — don't trade the open
            state.direction = None
            state.conviction = 0
            state.queued = False
            return state

        # ── Scoring ───────────────────────────────────────────────────────────
        score = 0

        # 1. Gap strength
        if gap_pct is not None:
            abs_gap = abs(gap_pct)
            if abs_gap >= GAP_STRONG_PCT:
                score += 25
            elif abs_gap >= GAP_MODERATE_PCT:
                score += 15
            elif abs_gap < GAP_FLAT_PCT:
                score -= 5   # tiny gap, don't over-trust it

        # 2. Momentum aligns with candidate direction
        if pm_momentum is not None:
            momentum_aligns = (candidate == "LONG" and pm_momentum > 0) or \
                              (candidate == "SHORT" and pm_momentum < 0)
            momentum_strong = abs(pm_momentum) > 0.002  # 0.2% move in 4 min = significant
            if momentum_aligns and momentum_strong:
                score += 20
            elif momentum_aligns:
                score += 10
            else:
                score -= 10  # fading / reversing into open

        # 3. L2 imbalance
        state.l2_direction = l2_direction
        if l2_direction:
            if l2_direction == candidate:
                imbalance_strength = abs(l2_imbalance) if l2_imbalance else 0.0
                score += int(20 * min(imbalance_strength, 1.0))  # up to +20
            else:
                score -= 10  # book disagrees with gap direction

        # 4. GEX wall — is there a wall directly in the path of our candidate?
        gex_blocking = False
        if candidate == "LONG" and gex_call_wall:
            dist_to_wall = (gex_call_wall - current_price) / current_price
            if 0 < dist_to_wall < GEX_WALL_BLOCKING_PCT:
                gex_blocking = True
                score -= 20  # wall directly above, gap-up will stall
                logger.info(f"OpeningPlay: {ticker} LONG candidate blocked by call wall ${gex_call_wall}")
        elif candidate == "SHORT" and gex_put_wall:
            dist_to_wall = (current_price - gex_put_wall) / current_price
            if 0 < dist_to_wall < GEX_WALL_BLOCKING_PCT:
                gex_blocking = True
                score -= 20  # wall directly below, gap-down will bounce
                logger.info(f"OpeningPlay: {ticker} SHORT candidate blocked by put wall ${gex_put_wall}")

        state.gex_blocking = gex_blocking

        # 5. IV rank gate — don't buy expensive options
        if iv_rank is not None and iv_rank > 70:
            score -= 10

        # ── Finalize ──────────────────────────────────────────────────────────
        score = max(0, score)
        state.conviction = score
        state.direction = candidate if score >= MIN_OPENING_PLAY_CONVICTION else None

        if state.direction:
            state.queued = True
            logger.info(
                f"OpeningPlay: {ticker} {state.direction} queued "
                f"(conviction={score}, gap={gap_pct:.3%}, momentum={pm_momentum:.3%}, "
                f"l2={l2_direction}, gex_blocking={gex_blocking})"
            )
        else:
            logger.info(
                f"OpeningPlay: {ticker} no play (conviction={score} < {MIN_OPENING_PLAY_CONVICTION}, "
                f"candidate={candidate})"
            )

        return state

    def get_state(self, ticker: str) -> OpeningPlayState:
        self._ensure_today()
        return self._states.get(ticker, OpeningPlayState(ticker=ticker))

    def mark_fired(self, ticker: str) -> None:
        self._ensure_today()
        if ticker in self._states:
            self._states[ticker].fired = True
            self._states[ticker].queued = False

    def get_queued_plays(self) -> list[tuple[str, OpeningPlayState]]:
        """Returns all tickers with a queued (not yet fired) opening play."""
        self._ensure_today()
        return [
            (ticker, state)
            for ticker, state in self._states.items()
            if state.queued and not state.fired and state.direction
        ]

    def reset_day(self) -> None:
        """Called at market close to reset all states."""
        self._day = None
        self._states = {}


# Module-level singleton
opening_play_analyzer = OpeningPlayAnalyzer()
