"""Exchange-date alignment for daily features and intraday reference prices."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from core.market_hours import (
    DAILY_MODEL_ISSUANCE_DELAY_MIN, _rth_close_for, is_market_holiday, session_hm,
)


def bar_session_date(value: Any) -> date | None:
    """Daily midnight-UTC values are date labels, not trades the prior evening."""
    try:
        text = str(value).strip()
        if len(text) == 10:
            return date.fromisoformat(text)
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if stamp.time().replace(tzinfo=None).isoformat() == "00:00:00":
            return stamp.date()
        if stamp.tzinfo is None:
            return stamp.date()
        return stamp.astimezone(ZoneInfo("America/New_York")).date()
    except (ValueError, TypeError, OverflowError):
        return None


def previous_session(day: date) -> date:
    day -= timedelta(days=1)
    while is_market_holiday(day):
        day -= timedelta(days=1)
    return day


def _latest_completed_session(now: datetime | None = None) -> date:
    current, minute = session_hm(now)
    expected = current.date()
    if is_market_holiday(expected) or minute < _rth_close_for(current) + DAILY_MODEL_ISSUANCE_DELAY_MIN:
        expected = previous_session(expected)
    return expected


def drop_incomplete_daily_bars(rows: list[dict], *, now: datetime | None = None) -> list[dict]:
    """Training history: drop any bar for a session that has not closed yet.

    Unlike completed_daily_bars() this does not require the latest session to
    be present (a lagging feed still trains on what it has); it only removes
    today's in-progress bar, whose partial high/low/close would otherwise label
    the last horizons (audit U65). Undatable rows are kept in place.
    """
    expected = _latest_completed_session(now)
    kept = []
    for row in rows or []:
        day = bar_session_date(row.get("ts"))
        if day is not None and day > expected:
            continue
        kept.append(row)
    return kept


def completed_daily_bars(rows: list[dict], *, now: datetime | None = None) -> list[dict]:
    """Require the latest completed session; never score a partial/stale bar."""
    expected = _latest_completed_session(now)
    dated = [(bar_session_date(row.get("ts")), row) for row in rows]
    selected = sorted(
        ((day, row) for day, row in dated if day is not None and day <= expected),
        key=lambda pair: pair[0],
    )
    if not selected or selected[-1][0] != expected:
        return []
    return [row for _, row in selected]


def prior_daily_bars(rows: list[dict], session_date: date) -> list[dict]:
    """Completed baseline only: today's partial daily volume is never in RVOL."""
    expected = previous_session(session_date)
    dated = [(bar_session_date(row.get("t")), row) for row in rows]
    selected = sorted(
        ((day, row) for day, row in dated if day is not None and day <= expected),
        key=lambda pair: pair[0],
    )
    if not selected or selected[-1][0] != expected:
        return []
    return [row for _, row in selected]
