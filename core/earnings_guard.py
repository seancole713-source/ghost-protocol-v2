"""
core/earnings_guard.py — earnings-calendar tripwire for the intraday lane
=========================================================================
The sniper/intraday book closes positions at the bell, but an earnings date
turns any intraday hold into an event bet: pre-market gap whipsaw, halts, and
the risk that an unexited lot rides the after-hours print (a 5-min-poll paper
wallet cannot guarantee the 15:00 close-out fills before the release). The
guard asks Finnhub for the earnings calendar (today .. +N days, session TZ)
and the intraday lane skips any symbol on it.

Fail-open BY DESIGN: if Finnhub is down we log + surface ok=False in diag,
we do not block the whole lane — the guard is a seatbelt, not an engine
interlock. Callers show `earnings_guard_ok` so a silent outage is visible.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import time
from typing import Dict, Set, Tuple
from zoneinfo import ZoneInfo

from core.market_hours import SESSION_TZ

LOGGER = logging.getLogger("ghost.earnings_guard")

_TIMEOUT = float(os.getenv("PRICE_PROVIDER_TIMEOUT_S", "8.0"))
# One calendar pull covers the whole watchlist; 6h TTL means <=4 calls/day.
_CACHE_TTL_S = int(os.getenv("EARNINGS_GUARD_CACHE_TTL_S", "21600"))
_cache: Dict[str, Tuple[float, Set[str], bool]] = {}


def earnings_guard_enabled() -> bool:
    return (os.getenv("PAPER_INTRADAY_EARNINGS_GUARD", "1") or "1").strip().lower() in (
        "1", "on", "true", "yes"
    )


def _window_days() -> int:
    """Days ahead (beyond today) to treat as earnings-blocked. Default 1 so a
    report tonight or tomorrow morning both block today's intraday entry."""
    return max(0, int(os.getenv("PAPER_INTRADAY_EARNINGS_WINDOW_DAYS", "1")))


def _session_today(now_ts: float | None = None) -> _dt.date:
    now = (
        _dt.datetime.now(ZoneInfo(SESSION_TZ))
        if now_ts is None
        else _dt.datetime.fromtimestamp(float(now_ts), ZoneInfo(SESSION_TZ))
    )
    return now.date()


def upcoming_earnings_symbols(now_ts: float | None = None) -> Tuple[Set[str], bool]:
    """Return (symbols reporting today..today+window, fetch_ok).

    fetch_ok=False means the calendar was unavailable and the empty set is
    IGNORANCE, not safety — callers must surface that distinction.
    """
    frm = _session_today(now_ts)
    to = frm + _dt.timedelta(days=_window_days())
    key = f"{frm.isoformat()}:{to.isoformat()}"
    hit = _cache.get(key)
    now_mono = time.time()
    if hit and now_mono - hit[0] < _CACHE_TTL_S:
        return set(hit[1]), hit[2]

    token = (os.getenv("FINNHUB_API_KEY") or "").strip()
    if not token:
        LOGGER.warning("earnings guard: FINNHUB_API_KEY missing — guard blind")
        _cache[key] = (now_mono, set(), False)
        return set(), False
    try:
        import requests

        r = requests.get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={"from": frm.isoformat(), "to": to.isoformat(), "token": token},
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            LOGGER.warning("earnings guard: finnhub HTTP %s", r.status_code)
            _cache[key] = (now_mono, set(), False)
            return set(), False
        rows = (r.json() or {}).get("earningsCalendar") or []
        syms = {
            str(row.get("symbol") or "").strip().upper()
            for row in rows
            if row.get("symbol")
        }
        syms.discard("")
        _cache[key] = (now_mono, syms, True)
        return set(syms), True
    except Exception as exc:
        LOGGER.warning("earnings guard fetch failed: %s", str(exc)[:100])
        _cache[key] = (now_mono, set(), False)
        return set(), False
