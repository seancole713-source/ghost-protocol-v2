"""US equity session clock — single source of truth in America/Chicago (CT).

Ghost is hardwired to Central Time for operators in Houston / US Central.
Cash session boundaries match NYSE/NASDAQ: 8:30 AM – 3:00 PM CT
(9:30 AM – 4:00 PM Eastern).
"""
from __future__ import annotations
from core.quiet import note_suppressed

import datetime as _dt
import os
from typing import Tuple

SESSION_TZ = "America/Chicago"

# US equity sessions in Central wall-clock (same instants as ET schedule)
PREMARKET_START_MIN = 3 * 60          # 3:00 AM CT  (4:00 AM ET)
RTH_OPEN_MIN = 8 * 60 + 30            # 8:30 AM CT  (9:30 AM ET)
RTH_CLOSE_MIN = 15 * 60               # 3:00 PM CT  (4:00 PM ET)
AFTERHOURS_END_MIN = 19 * 60          # 7:00 PM CT  (8:00 PM ET)
RTH_MINUTES = RTH_CLOSE_MIN - RTH_OPEN_MIN
PREMARKET_MINUTES = RTH_OPEN_MIN - PREMARKET_START_MIN  # 330 min (3:00–8:30 AM CT)

# NYSE full-day closures (market closed all day). Half-days close early at
# 1:00 PM ET (12:00 PM CT). This is a data table, not logic — update annually.
# Source: NYSE holiday schedule. Env override GHOST_HOLIDAYS (comma-separated
# YYYY-MM-DD) lets operators add/remove dates without a deploy.
_NYSE_FULL_DAY_HOLIDAYS = frozenset({
    "2026-01-01",  # New Year's Day
    "2026-01-19",  # Martin Luther King Jr. Day
    "2026-02-16",  # Presidents' Day
    "2026-04-03",  # Good Friday
    "2026-05-25",  # Memorial Day
    "2026-06-19",  # Juneteenth
    "2026-07-03",  # Independence Day (observed; Jul 4 is a Saturday)
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving
    "2026-12-25",  # Christmas
})

# NYSE early-close days (RTH ends 12:00 PM CT instead of 3:00 PM CT).
_NYSE_HALF_DAYS = frozenset({
    "2026-11-27",  # Day after Thanksgiving
    "2026-12-24",  # Christmas Eve
})

_HALF_DAY_CLOSE_MIN = 12 * 60  # 12:00 PM CT
DAILY_MODEL_ISSUANCE_DELAY_MIN = 5
DAILY_MODEL_ISSUANCE_WINDOW_MIN = 55


def _holiday_overrides() -> "frozenset[str]":
    raw = os.getenv("GHOST_HOLIDAYS", "")
    if not raw:
        return frozenset()
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def is_market_holiday(d: _dt.date | None = None) -> bool:
    """True when the NYSE is closed all day (weekend or listed holiday)."""
    d = d or _now_ct().date()
    if d.weekday() >= 5:
        return True
    iso = d.isoformat()
    if iso in _holiday_overrides():
        return True
    return iso in _NYSE_FULL_DAY_HOLIDAYS


def is_half_day(d: _dt.date | None = None) -> bool:
    """True when the NYSE closes early (12:00 PM CT) on a weekday."""
    d = d or _now_ct().date()
    if d.weekday() >= 5:
        return False
    return d.isoformat() in _NYSE_HALF_DAYS


def _rth_close_for(now: _dt.datetime) -> int:
    """Effective RTH close minute, honoring half-day early closes."""
    if is_half_day(now.date()):
        return _HALF_DAY_CLOSE_MIN
    return RTH_CLOSE_MIN


def _now_ct() -> _dt.datetime:
    try:
        from zoneinfo import ZoneInfo

        return _dt.datetime.now(ZoneInfo(SESSION_TZ))
    except Exception:
        return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) - _dt.timedelta(hours=6)


def session_hm(now: _dt.datetime | None = None) -> Tuple[_dt.datetime, int]:
    """Return (now_ct, minutes_since_midnight Central)."""
    now = now or _now_ct()
    if now.tzinfo is not None:
        try:
            from zoneinfo import ZoneInfo

            now = now.astimezone(ZoneInfo(SESSION_TZ))
        except Exception:
            note_suppressed()
    return now, now.hour * 60 + now.minute


def is_us_premarket(now: _dt.datetime | None = None) -> bool:
    """Mon–Fri 3:00 AM – 8:30 AM CT, excluding market holidays."""
    now, hm = session_hm(now)
    if now.weekday() >= 5 or is_market_holiday(now.date()):
        return False
    return PREMARKET_START_MIN <= hm < RTH_OPEN_MIN


def is_us_rth(now: _dt.datetime | None = None) -> bool:
    """Mon–Fri 8:30 AM – close CT (3:00 PM, or 12:00 PM on half-days)."""
    now, hm = session_hm(now)
    if now.weekday() >= 5 or is_market_holiday(now.date()):
        return False
    return RTH_OPEN_MIN <= hm < _rth_close_for(now)


def is_us_after_hours(now: _dt.datetime | None = None) -> bool:
    """Mon–Fri close – 7:00 PM CT, excluding market holidays."""
    now, hm = session_hm(now)
    if now.weekday() >= 5 or is_market_holiday(now.date()):
        return False
    return _rth_close_for(now) <= hm < AFTERHOURS_END_MIN


def in_daily_model_issuance_window(now: _dt.datetime | None = None) -> bool:
    """True only in the frozen post-close daily-model sampling window.

    Training features use completed daily bars and labels enter at that close.
    Production and shadow issuance therefore sample 5-60 minutes after the
    applicable cash close, including early-close sessions. Intraday/premarket
    scans remain useful diagnostics but are not comparable outcome evidence.
    """
    now, hm = session_hm(now)
    if is_market_holiday(now.date()):
        return False
    close_min = _rth_close_for(now)
    start = close_min + DAILY_MODEL_ISSUANCE_DELAY_MIN
    end = close_min + DAILY_MODEL_ISSUANCE_DELAY_MIN + DAILY_MODEL_ISSUANCE_WINDOW_MIN
    return start <= hm < end


def market_session_label(now: _dt.datetime | None = None) -> str:
    if is_us_rth(now):
        return "Market Open"
    if is_us_after_hours(now):
        return "After Hours"
    if is_us_premarket(now):
        return "Pre-Market"
    return "Market Closed"


def is_us_extended_hours(now: _dt.datetime | None = None) -> bool:
    """Mon–Fri 3:00 AM – 7:00 PM CT, excluding market holidays."""
    now, hm = session_hm(now)
    if now.weekday() >= 5 or is_market_holiday(now.date()):
        return False
    return PREMARKET_START_MIN <= hm < AFTERHOURS_END_MIN


def next_radar_resume_label(now: _dt.datetime | None = None) -> str:
    """Human label for when the squeeze radar next wakes (premarket 3:00 AM CT)."""
    now, hm = session_hm(now)
    if is_us_extended_hours(now):
        return "now (live)"
    # Weekend → next Monday 3:00 AM CT
    if now.weekday() == 5:
        return "Mon 3:00 AM CT"
    if now.weekday() == 6:
        return "Mon 3:00 AM CT"
    # Weekday overnight (after 7 PM) or before 3 AM → next 3:00 AM CT
    if hm >= AFTERHOURS_END_MIN or hm < PREMARKET_START_MIN:
        if now.weekday() == 4 and hm >= AFTERHOURS_END_MIN:
            return "Mon 3:00 AM CT"
        return "3:00 AM CT"
    return "3:00 AM CT"


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
