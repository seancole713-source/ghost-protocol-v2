"""US equity market calendar: which days trade, and which close early.

Source of truth is Alpaca's market calendar (GET /v2/calendar on the PAPER
trading host), refreshed daily by the readiness probe job and stored with its
fetch time. When it is missing or stale, a built-in NYSE table is used and the
answer says so. EDGE_HOLIDAYS (comma-separated dates) still adds closures.

An EARLY-CLOSE day (13:00 ET) cannot run the frozen Gap-and-Go rule: its time
exit is 15:30 ET, after the close. Rather than bend a frozen rule, edge issues
no forecasts on those days and records why.
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any, Dict, Optional, Tuple

# NYSE full closures and 13:00 early closes (ET), 2026-2027, as published by NYSE Group.
BUILTIN_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}
BUILTIN_EARLY_CLOSE = {"2026-11-27": (13, 0), "2026-12-24": (13, 0), "2027-11-26": (13, 0)}
REGULAR = ((9, 30), (16, 0))
STALE_AFTER_S = 8 * 86400


def _env_holidays() -> set:
    return {x.strip() for x in os.getenv("EDGE_HOLIDAYS", "").split(",") if x.strip()}


def _hm(s: str) -> Tuple[int, int]:
    h, m = str(s).split(":")[:2]
    return int(h), int(m)


def _stored(store, now: Optional[int]) -> Optional[Dict[str, Any]]:
    if store is None:
        return None
    cal = store.get("edge_calendar", "current")
    if not cal or (now is not None and now - int(cal.get("fetched_at") or 0) > STALE_AFTER_S):
        return None
    return cal


def session(day: date, store=None, now: Optional[int] = None) -> Dict[str, Any]:
    """{"trading", "open", "close", "early_close", "source"} for `day`."""
    ds = day.isoformat()
    if day.weekday() >= 5 or ds in _env_holidays():
        return {"trading": False, "open": None, "close": None, "early_close": False,
                "source": "weekend" if day.weekday() >= 5 else "EDGE_HOLIDAYS"}
    cal = _stored(store, now)
    if cal and cal["start"] <= ds <= cal["end"]:
        d = cal["days"].get(ds)
        if not d:
            return {"trading": False, "open": None, "close": None, "early_close": False, "source": "alpaca"}
        o, c = _hm(d["open"]), _hm(d["close"])
        return {"trading": True, "open": o, "close": c, "early_close": c < REGULAR[1], "source": "alpaca"}
    if ds in BUILTIN_HOLIDAYS:
        return {"trading": False, "open": None, "close": None, "early_close": False, "source": "builtin"}
    c = BUILTIN_EARLY_CLOSE.get(ds, REGULAR[1])
    return {"trading": True, "open": REGULAR[0], "close": c, "early_close": c < REGULAR[1], "source": "builtin"}


def refresh(http, store, *, today: date, now: int, days: int = 400) -> Dict[str, Any]:
    """Fetch Alpaca's calendar through the paper host; keep the stored one if it fails."""
    from edge import paper
    try:
        r = http.get(f"{paper.base_url()}/v2/calendar", headers=paper._headers(), timeout=20,
                     params={"start": today.isoformat(), "end": (today + timedelta(days=days)).isoformat()})
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": type(exc).__name__}
    if r.status_code >= 400:
        return {"status": "error", "http_status": r.status_code}
    rows = r.json() or []
    got = {str(x["date"]): {"open": x["open"], "close": x["close"]} for x in rows
           if isinstance(x, dict) and x.get("date") and x.get("open") and x.get("close")}
    if not got:
        return {"status": "empty"}
    store.put("edge_calendar", "current", {"days": got, "start": today.isoformat(),
                                           "end": (today + timedelta(days=days)).isoformat(),
                                           "fetched_at": now, "source": "alpaca /v2/calendar"})
    early = sorted(d for d, v in got.items() if _hm(v["close"]) < REGULAR[1])
    return {"status": "ok", "trading_days": len(got), "early_closes": early[:10]}
