"""Ghost's hand-typed NYSE holiday and early-close tables, checked against the maintained
exchange_calendars XNYS calendar (Apache-2.0). Live sessions come from Alpaca's calendar first; these
tables are the fallback and what the history replays (2024-2025) run on, so a typo here would silently
skip or invent a trading day in a replay. Test-only: nothing at runtime depends on exchange_calendars."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

xcals = pytest.importorskip("exchange_calendars")

from edge import backtest_edgar8k as E8, calendar as CAL  # noqa: E402

START, END = date(2024, 1, 1), date(2027, 12, 31)


def _official():
    import pandas as pd
    nyse = xcals.get_calendar("XNYS", start=START.isoformat(), end=END.isoformat())
    open_days = {d.date() for d in nyse.sessions}
    early = {}
    for d in nyse.sessions:
        close = nyse.session_close(d).tz_convert("America/New_York")
        if (close.hour, close.minute) != (16, 0):
            early[d.date().isoformat()] = (close.hour, close.minute)
    holidays = {(START + timedelta(n)).isoformat() for n in range((END - START).days + 1)
                if (START + timedelta(n)).weekday() < 5 and (START + timedelta(n)) not in open_days}
    return holidays, early


def test_builtin_holidays_match_the_official_nyse_calendar():
    holidays, _ = _official()
    assert set(CAL.BUILTIN_HOLIDAYS) == holidays, (
        f"missing: {sorted(holidays - set(CAL.BUILTIN_HOLIDAYS))}, "
        f"extra: {sorted(set(CAL.BUILTIN_HOLIDAYS) - holidays)}")


def test_builtin_early_closes_match_the_official_nyse_calendar():
    _, early = _official()
    assert dict(CAL.BUILTIN_EARLY_CLOSE) == early


def test_the_8k_replays_own_tables_agree_with_the_official_calendar():
    holidays, early = _official()
    replay_years = {h for h in holidays if h < "2026-01-01"}
    assert set(E8.EXTRA_HOLIDAYS) <= holidays and replay_years <= set(E8.EXTRA_HOLIDAYS) | set(CAL.BUILTIN_HOLIDAYS)
    assert set(E8.EXTRA_EARLY_CLOSE) <= set(early)
