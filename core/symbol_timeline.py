"""core/symbol_timeline.py — unified per-symbol detection timeline.

The ARCT post-mortem's core UI complaint: Squeeze Radar caught the move, but
`/picks` emphasized current Hunter candidates and hid the historical radar
detections + delivered alerts, so it looked like Ghost saw nothing.

This module assembles ONE timeline per symbol from every detection surface:
  - explosion-benchmark observations (WATCH / candidate / alert),
  - squeeze outcomes (delivered Telegram alerts + candidates),
  - news events (catalyst evidence),
  - the current WATCH / candidate / leader state.

It is read-only and advisory. It never changes a pick; it only makes the full
detection history visible in one place.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

LOGGER = logging.getLogger("ghost.symbol_timeline")


def _squeeze_rows(symbol: str, cur) -> List[Dict[str, Any]]:
    """Squeeze outcomes (alerts + candidates) for a symbol, newest first."""
    try:
        cur.execute(
            """SELECT alerted_at, kind, source, confidence_pct, rvol,
                      peak_move_pct, buy, sell, outcome, hit_target
               FROM ghost_squeeze_outcomes
               WHERE symbol=%s ORDER BY alerted_at DESC LIMIT 50""",
            (symbol.upper(),),
        )
        rows = cur.fetchall()
    except Exception as exc:
        LOGGER.debug("symbol_timeline squeeze %s: %s", symbol, str(exc)[:80])
        return []
    out = []
    for r in rows:
        out.append({
            "ts": r[0],
            "kind": r[1],
            "source": r[2],
            "confidence_pct": r[3],
            "rvol": r[4],
            "peak_move_pct": r[5],
            "buy": r[6],
            "sell": r[7],
            "outcome": r[8],
            "hit_target": r[9],
        })
    return out


def _observation_rows(symbol: str, cur) -> List[Dict[str, Any]]:
    """Explosion-benchmark observations (WATCH / candidate / alert)."""
    try:
        cur.execute(
            """SELECT observed_at, price, kind, confidence_pct
               FROM ghost_explosion_observations
               WHERE symbol=%s ORDER BY observed_at DESC LIMIT 100""",
            (symbol.upper(),),
        )
        rows = cur.fetchall()
    except Exception as exc:
        LOGGER.debug("symbol_timeline observations %s: %s", symbol, str(exc)[:80])
        return []
    return [
        {"ts": r[0], "price": r[1], "kind": r[2], "confidence_pct": r[3]}
        for r in rows
    ]


def _news_rows(symbol: str, cur) -> List[Dict[str, Any]]:
    """News events (catalyst evidence) for a symbol."""
    try:
        cur.execute(
            """SELECT asof_ts, event_type, direction_hint, materiality, evidence
               FROM ghost_news_events
               WHERE symbol=%s ORDER BY asof_ts DESC LIMIT 50""",
            (symbol.upper(),),
        )
        rows = cur.fetchall()
    except Exception as exc:
        LOGGER.debug("symbol_timeline news %s: %s", symbol, str(exc)[:80])
        return []
    return [
        {"ts": r[0], "event_type": r[1], "direction_hint": r[2],
         "materiality": r[3], "evidence": r[4]}
        for r in rows
    ]


def build_symbol_timeline(symbol: str, cur=None) -> Dict[str, Any]:
    """Assemble the unified timeline for one symbol.

    Returns a merged, time-sorted list of events across all detection surfaces,
    plus the current WATCH/candidate/leader state. Read-only.
    """
    sym = (symbol or "").upper()
    if not sym:
        return {"ok": False, "error": "empty symbol"}

    try:
        if cur is not None:
            squeeze = _squeeze_rows(sym, cur)
            obs = _observation_rows(sym, cur)
            news = _news_rows(sym, cur)
        else:
            from core.db import db_conn
            with db_conn() as conn:
                c = conn.cursor()
                squeeze = _squeeze_rows(sym, c)
                obs = _observation_rows(sym, c)
                news = _news_rows(sym, c)
    except Exception as exc:
        LOGGER.warning("build_symbol_timeline(%s): %s", sym, str(exc)[:120])
        return {"ok": False, "error": str(exc)[:120]}

    # Merge into one chronological list (newest first).
    events: List[Dict[str, Any]] = []
    for s in squeeze:
        events.append({"ts": s["ts"], "surface": "squeeze", **s})
    for o in obs:
        events.append({"ts": o["ts"], "surface": "observation", **o})
    for n in news:
        events.append({"ts": n["ts"], "surface": "news", **n})
    events.sort(key=lambda e: e.get("ts") or 0, reverse=True)

    # Current state from the live scan report.
    current = {}
    try:
        from core.squeeze_monitor import get_squeeze_status
        st = get_squeeze_status()
        watches = {w.get("symbol"): w for w in (st.get("watches") or [])}
        candidates = {p.get("symbol"): p for p in (st.get("candidates") or [])}
        leaders = {ld.get("symbol"): ld for ld in (st.get("leaders") or [])}
        current = {
            "watch": watches.get(sym),
            "candidate": candidates.get(sym),
            "leader": leaders.get(sym),
        }
    except Exception as exc:
        LOGGER.debug("symbol_timeline current state %s: %s", sym, str(exc)[:80])

    return {
        "ok": True,
        "symbol": sym,
        "events": events,
        "event_count": len(events),
        "current": current,
        "note": "Unified detection timeline across squeeze radar, observations, "
                "and news. Detection history is visible even when no trade fired.",
    }
