"""US equity session clock — single source of truth in America/Chicago (CT).

Ghost is hardwired to Central Time for operators in Houston / US Central.
Cash session boundaries match NYSE/NASDAQ: 8:30 AM – 3:00 PM CT
(9:30 AM – 4:00 PM Eastern).
"""
from __future__ import annotations

import datetime as _dt
from typing import Tuple

SESSION_TZ = "America/Chicago"

# US equity sessions in Central wall-clock (same instants as ET schedule)
PREMARKET_START_MIN = 3 * 60          # 3:00 AM CT  (4:00 AM ET)
RTH_OPEN_MIN = 8 * 60 + 30            # 8:30 AM CT  (9:30 AM ET)
RTH_CLOSE_MIN = 15 * 60               # 3:00 PM CT  (4:00 PM ET)
AFTERHOURS_END_MIN = 19 * 60          # 7:00 PM CT  (8:00 PM ET)
RTH_MINUTES = RTH_CLOSE_MIN - RTH_OPEN_MIN


def _now_ct() -> _dt.datetime:
    try:
        from zoneinfo import ZoneInfo

        return _dt.datetime.now(ZoneInfo(SESSION_TZ))
    except Exception:
        return _dt.datetime.utcnow() - _dt.timedelta(hours=6)


def session_hm(now: _dt.datetime | None = None) -> Tuple[_dt.datetime, int]:
    """Return (now_ct, minutes_since_midnight Central)."""
    now = now or _now_ct()
    if now.tzinfo is not None:
        try:
            from zoneinfo import ZoneInfo

            now = now.astimezone(ZoneInfo(SESSION_TZ))
        except Exception:
            pass
    return now, now.hour * 60 + now.minute


def is_us_premarket(now: _dt.datetime | None = None) -> bool:
    """Mon–Fri 3:00 AM – 8:30 AM CT."""
    now, hm = session_hm(now)
    if now.weekday() >= 5:
        return False
    return PREMARKET_START_MIN <= hm < RTH_OPEN_MIN


def is_us_rth(now: _dt.datetime | None = None) -> bool:
    """Mon–Fri 8:30 AM – 3:00 PM CT."""
    now, hm = session_hm(now)
    if now.weekday() >= 5:
        return False
    return RTH_OPEN_MIN <= hm < RTH_CLOSE_MIN


def is_us_after_hours(now: _dt.datetime | None = None) -> bool:
    """Mon–Fri 3:00 PM – 7:00 PM CT."""
    now, hm = session_hm(now)
    if now.weekday() >= 5:
        return False
    return RTH_CLOSE_MIN <= hm < AFTERHOURS_END_MIN


def market_session_label(now: _dt.datetime | None = None) -> str:
    if is_us_rth(now):
        return "Market Open"
    if is_us_after_hours(now):
        return "After Hours"
    if is_us_premarket(now):
        return "Pre-Market"
    return "Market Closed"


def is_us_extended_hours(now: _dt.datetime | None = None) -> bool:
    """Mon–Fri 3:00 AM – 7:00 PM CT."""
    now, hm = session_hm(now)
    if now.weekday() >= 5:
        return False
    return PREMARKET_START_MIN <= hm < AFTERHOURS_END_MIN


def now_ct_iso(now: _dt.datetime | None = None) -> str:
    """Current Central wall clock for UI, e.g. '3:22 PM CT'."""
    n = now or _now_ct()
    try:
        return n.strftime("%-I:%M %p CT")
    except Exception:
        return n.strftime("%I:%M %p CT").lstrip("0")


# Backward-compatible aliases (legacy names pointed at ET; now Central)
_now_et = _now_ct
now_et_iso = now_ct_iso


def in_open_buffer_window_et(open_buffer_min: int) -> Tuple[bool, str]:
    """True during the first N minutes after 8:30 AM CT cash open."""
    if open_buffer_min <= 0:
        return False, ""
    now, hm = session_hm()
    if now.weekday() >= 5:
        return False, ""
    if RTH_OPEN_MIN <= hm < RTH_OPEN_MIN + open_buffer_min:
        return True, f"open buffer ({open_buffer_min}m after 8:30 AM CT)"
    return False, ""
