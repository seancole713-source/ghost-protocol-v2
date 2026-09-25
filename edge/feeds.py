"""Which live Alpaca feed to use: the free IEX view, or consolidated SIP once it is paid for.

Goal (operator, 2026-09-25): when the SIP subscription is bought, nothing else should need
to change. So the feed is never hard-coded: the first live call of each ET day asks Alpaca
whether this key may read RECENT SIP data (one snapshot of SPY on feed=sip). Allowed ->
every live call that day uses SIP; refused (403, the free plan) -> IEX, as before. The
answer is cached per day and stated on every card and forecast.

EDGE_LIVE_FEED=iex|sip forces a feed (e.g. to test, or to fall back); default "auto".

Only a SIP answer holds for the whole day. An IEX answer is re-asked every RECHECK_S, so a
subscription bought after the first check of the morning is picked up within half an hour
instead of the next day (audit 2026-09-25). Each forecast and card states its own feed, and
the ledger never pools IEX and SIP records (edge/ledger.py).
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict

from edge.contracts import ET

PROBE_SYMBOL = "SPY"
RECHECK_S = 1800          # an IEX answer is re-asked after this long; a SIP answer holds all day
UNDECIDED_RECHECK_S = 300  # a network error is re-asked sooner


def live_feed(get, store, *, now: int) -> str:
    forced = (os.getenv("EDGE_LIVE_FEED") or "auto").strip().lower()
    if forced in ("iex", "sip"):
        return forced
    day = datetime.fromtimestamp(now, tz=ET).date().isoformat()
    cached = store.get("edge_feed", day) if store is not None else None
    if cached:
        if cached.get("feed") == "sip":
            return "sip"
        ttl = RECHECK_S if cached.get("decided", True) else UNDECIDED_RECHECK_S
        if now - int(cached.get("checked_at") or 0) < ttl:
            return cached["feed"]
    rec = check(get, now=now)
    if store is not None:
        store.put("edge_feed", day, {**rec, "day": day})
    return rec["feed"]


def check(get, *, now: int) -> Dict[str, Any]:
    """{"feed", "decided", "why"}: SIP only if the key may read recent SIP data right now."""
    from edge.providers import alpaca as A
    try:
        snaps = A.snapshots(get, [PROBE_SYMBOL], feed="sip")
    except Exception as exc:  # noqa: BLE001 - 403 on the free plan; a network error is undecided
        msg = f"{type(exc).__name__}: {str(exc)[:120]}"
        decided = "403" in msg or "forbidden" in msg.lower() or "subscription" in msg.lower()
        return {"feed": "iex", "decided": decided, "why": msg, "checked_at": now}
    ok = bool((snaps or {}).get(PROBE_SYMBOL))
    return {"feed": "sip" if ok else "iex", "decided": True, "checked_at": now,
            "why": "recent SIP data allowed" if ok else "SIP answered with no data"}
