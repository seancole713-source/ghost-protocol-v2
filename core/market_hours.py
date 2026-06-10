"""US equity session clock — single source of truth in America/New_York (ET).

Several modules previously mixed CT wall-clock times with ET session labels,
which made is_premarket true for ~60 minutes after the 9:30 ET open during
CDT (Bug: "Market Open" + is_premarket true at 9:35 ET).
"""
from __future__ import annotations

import datetime as _dt
from typing import Tuple


def _now_et() -> _dt.datetime:
    try:
        from zoneinfo import ZoneInfo

        return _dt.datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return _dt.datetime.utcnow() - _dt.timedelta(hours=5)


def session_hm(now: _dt.datetime | None = None) -> Tuple[_dt.datetime, int]:
    """Return (now_et, minutes_since_midnight)."""
    now = now or _now_et()
    return now, now.hour * 60 + now.minute


def is_us_premarket(now: _dt.datetime | None = None) -> bool:
    """Mon–Fri 4:00 AM – 9:30 AM ET (exclusive of open)."""
    now, hm = session_hm(now)
    if now.weekday() >= 5:
        return False
    return (4 * 60) <= hm < (9 * 60 + 30)


def is_us_rth(now: _dt.datetime | None = None) -> bool:
    """Mon–Fri 9:30 AM – 4:00 PM ET."""
    now, hm = session_hm(now)
    if now.weekday() >= 5:
        return False
    return (9 * 60 + 30) <= hm < (16 * 60)


def is_us_after_hours(now: _dt.datetime | None = None) -> bool:
    """Mon–Fri 4:00 PM – 8:00 PM ET."""
    now, hm = session_hm(now)
    if now.weekday() >= 5:
        return False
    return (16 * 60) <= hm < (20 * 60)


def market_session_label(now: _dt.datetime | None = None) -> str:
    """Human label aligned with api.wolf_endpoints._market_status."""
    if is_us_rth(now):
        return "Market Open"
    if is_us_after_hours(now):
        return "After Hours"
    if is_us_premarket(now):
        return "Pre-Market"
    return "Market Closed"


def in_open_buffer_window_et(open_buffer_min: int) -> Tuple[bool, str]:
    """True during the first N minutes after 9:30 ET cash open."""
    if open_buffer_min <= 0:
        return False, ""
    now, hm = session_hm()
    if now.weekday() >= 5:
        return False, ""
    open_min = 9 * 60 + 30
    if open_min <= hm < open_min + open_buffer_min:
        return True, f"open buffer ({open_buffer_min}m after 9:30 ET)"
    return False, ""
