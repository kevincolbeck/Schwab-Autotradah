"""
Trading time window management.

Windows (ET):
  MORNING:    9:30 – 11:00  (entries allowed)
  AFTERNOON:  3:00 – 3:55   (entries allowed)
  HARD_CLOSE: 3:58           (force-close all positions)
  OUTSIDE:    all other hours (no new entries; existing positions hold)

Rules:
  1. New entries only during MORNING or AFTERNOON windows.
  2. If the bot is in a position when a window closes, the position continues
     to run until SL or TP is hit — it is NOT force-closed at window end.
  3. Hard close at 3:58 PM ET closes ALL open positions regardless of P&L.
  4. Any position open at or after 3:59 PM must have been entered in the
     afternoon window; it gets force-closed at 3:58 ET no matter what.
"""

from datetime import datetime, time as dtime
from typing import Literal
import pytz

from backend.config import (
    ET,
    WINDOW_MORNING_START, WINDOW_MORNING_END,
    WINDOW_AFTERNOON_START, WINDOW_AFTERNOON_END,
    HARD_CLOSE_TIME,
    PRE_OPEN_WATCH_START, PRE_OPEN_WATCH_END,
)

TimeWindow = Literal["MORNING", "AFTERNOON", "PRE_OPEN_WATCH", "OUTSIDE"]


def _et_now() -> datetime:
    return datetime.now(ET)


def _hm_to_time(hm: tuple[int, int]) -> dtime:
    return dtime(hm[0], hm[1])


def current_window() -> TimeWindow:
    """Return the current trading window classification."""
    now = _et_now().time()
    pre_open_watch_start = _hm_to_time(PRE_OPEN_WATCH_START)
    morning_start = _hm_to_time(WINDOW_MORNING_START)
    morning_end   = _hm_to_time(WINDOW_MORNING_END)
    afternoon_start = _hm_to_time(WINDOW_AFTERNOON_START)
    afternoon_end   = _hm_to_time(WINDOW_AFTERNOON_END)

    if pre_open_watch_start <= now < morning_start:
        return "PRE_OPEN_WATCH"
    if morning_start <= now < morning_end:
        return "MORNING"
    if afternoon_start <= now < afternoon_end:
        return "AFTERNOON"
    return "OUTSIDE"


def is_pre_open_watch() -> bool:
    """True between 9:25 and 9:30 AM ET — pre-open signal analysis window."""
    return current_window() == "PRE_OPEN_WATCH"


def is_entry_allowed() -> bool:
    """True if new positions may be opened right now."""
    return current_window() in ("MORNING", "AFTERNOON")


def is_hard_close_time() -> bool:
    """True at or after 3:58 PM ET — all positions must be force-closed."""
    now = _et_now().time()
    return now >= _hm_to_time(HARD_CLOSE_TIME)


def is_market_open() -> bool:
    """True between 9:30 AM and 4:00 PM ET, Mon–Fri."""
    now_et = _et_now()
    if now_et.weekday() >= 5:   # Saturday=5, Sunday=6
        return False
    t = now_et.time()
    return dtime(9, 30) <= t < dtime(16, 0)


def minutes_until_hard_close() -> float:
    """Returns minutes remaining until 3:58 PM ET. Negative if already past."""
    now_et = _et_now()
    close_today = now_et.replace(
        hour=HARD_CLOSE_TIME[0], minute=HARD_CLOSE_TIME[1], second=0, microsecond=0
    )
    delta = (close_today - now_et).total_seconds() / 60
    return delta


def should_skip_afternoon_entry(min_trade_duration_minutes: float = 3.0) -> bool:
    """
    Return True if there is not enough time left in the afternoon window
    to allow a trade to reach SL/TP with minimal time to play out.
    """
    mins_left = minutes_until_hard_close()
    return mins_left < min_trade_duration_minutes
