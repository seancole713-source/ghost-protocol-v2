"""The supported universe: every listed US common stock, snapshotted daily.

A miss is only a coverage failure if the stock was OUTSIDE what the system
claims to cover. A listed common stock the radar simply never surfaced is a
detection failure. Without a maintained universe those two collapse into one.

Source: Polygon reference tickers (probe-verified 2026-09-22), paginated via
next_url. Fetched a few pages per scheduler tick and resumed from a stored
cursor, so no single tick can exceed its timeout or rate-limit Ghost's shared
key. Each finished snapshot carries known_at and the day's listings/delistings.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from edge.providers import polygon as PG

COMMON_TYPES = frozenset({"CS", "ADRC"})   # common stock; ADR common


def _pace() -> float:
    try:
        return float(os.getenv("EDGE_PROBE_POLYGON_PACE_S", "13"))
    except ValueError:
        return 13.0


def step(get, store, *, day: str, now: int, pages_per_tick: int = 5, sleep=time.sleep) -> Dict[str, Any]:
    """Advance today's snapshot by up to `pages_per_tick` pages. Idempotent."""
    if store.get("edge_universe", day):
        return {"status": "already_complete", "day": day}
    prog = store.get("edge_universe_progress", day) or {"next_url": None, "rows": {}, "pages": 0}
    key = (os.getenv("POLYGON_API_KEY") or "").strip()
    if not key:
        return {"status": "error", "error": "POLYGON_API_KEY not set"}
    url = prog["next_url"] or (PG._base_url() + "/v3/reference/tickers")
    params: Optional[Dict[str, Any]] = (
        {"market": "stocks", "active": "true", "limit": 1000, "apiKey": key}
        if not prog["next_url"] else {"apiKey": key})
    for i in range(pages_per_tick):
        if i:
            sleep(_pace())
        r = PG._get_patiently(get, url, params, sleep=sleep)
        if getattr(r, "status_code", 200) >= 400:
            return {"status": "error", "error": f"HTTP {r.status_code}", "pages": prog["pages"]}
        p = r.json() or {}
        for t in p.get("results") or []:
            sym = str(t.get("ticker") or "").upper()
            if sym and t.get("type") in COMMON_TYPES:
                prog["rows"][sym] = t.get("primary_exchange") or ""
        prog["pages"] += 1
        nxt = p.get("next_url")
        if not nxt:
            return _finish(store, day=day, now=now, rows=prog["rows"], pages=prog["pages"])
        url, params = nxt, {"apiKey": key}
        prog["next_url"] = nxt
    store.put("edge_universe_progress", day, prog)
    return {"status": "in_progress", "pages": prog["pages"], "so_far": len(prog["rows"])}


def _finish(store, *, day: str, now: int, rows: Dict[str, str], pages: int) -> Dict[str, Any]:
    prev = latest(store, before=day)
    prev_set = set(prev["symbols"]) if prev else set()
    cur = sorted(rows)
    snap = {"day": day, "known_at": now, "count": len(cur), "pages": pages, "symbols": cur,
            "added": sorted(set(cur) - prev_set) if prev else [],
            "removed": sorted(prev_set - set(cur)) if prev else [],
            "types": sorted(COMMON_TYPES), "source": "polygon reference tickers"}
    store.put("edge_universe", day, snap)
    store.put("edge_universe_progress", day, {"next_url": None, "rows": {}, "pages": pages, "done": True})
    return {"status": "complete", "count": len(cur), "added": len(snap["added"]), "removed": len(snap["removed"])}


def latest(store, *, before: Optional[str] = None) -> Optional[Dict[str, Any]]:
    snaps = [s for s in store.scan("edge_universe") if before is None or s["day"] < before]
    return max(snaps, key=lambda s: s["day"]) if snaps else None


def symbols_as_of(store, day: str) -> Optional[set]:
    """The universe the system could claim on `day`: the latest snapshot on or before it."""
    snaps = [s for s in store.scan("edge_universe") if s["day"] <= day]
    return set(max(snaps, key=lambda s: s["day"])["symbols"]) if snaps else None
