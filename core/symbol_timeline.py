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
import re
from typing import Any, Callable, Dict, List, Tuple

LOGGER = logging.getLogger("ghost.symbol_timeline")
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")


def _squeeze_rows(symbol: str, cur) -> List[Dict[str, Any]]:
    """Squeeze outcomes (alerts + candidates) for a symbol, newest first."""
    cur.execute(
        """SELECT alerted_at, kind, source, confidence_pct, rvol,
                  peak_move_pct, buy, sell, outcome, hit_target
           FROM ghost_squeeze_outcomes
           WHERE symbol=%s ORDER BY alerted_at DESC LIMIT 50""",
        (symbol.upper(),),
    )
    rows = cur.fetchall()
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
    cur.execute(
        """SELECT observed_at, price, kind, confidence_pct
           FROM ghost_explosion_observations
           WHERE symbol=%s ORDER BY observed_at DESC LIMIT 100""",
        (symbol.upper(),),
    )
    rows = cur.fetchall()
    return [
        {"ts": r[0], "price": r[1], "kind": r[2], "confidence_pct": r[3]}
        for r in rows
    ]


def _news_rows(symbol: str, cur) -> List[Dict[str, Any]]:
    """News events (catalyst evidence) for a symbol."""
    cur.execute(
        """SELECT asof_ts, event_type, direction_hint, materiality, evidence,
                  derived, origin_symbol, extracted_at
           FROM ghost_news_events
           WHERE symbol=%s ORDER BY asof_ts DESC LIMIT 50""",
        (symbol.upper(),),
    )
    rows = cur.fetchall()
    return [
        {"ts": r[0], "event_type": r[1], "direction_hint": r[2],
         "materiality": r[3], "evidence": r[4], "derived": bool(r[5]),
         "origin_symbol": r[6], "extracted_at": r[7],
         "advisory_only": bool(r[5]), "decision_eligible": not bool(r[5])}
        for r in rows
    ]


def _external_rows(symbol: str, cur) -> List[Dict[str, Any]]:
    """External screener evidence, always advisory and trade-ineligible."""
    from core.external_context_ledger import external_observations_for_symbol

    return external_observations_for_symbol(symbol, cur)


def _read_db_surface(
    symbol: str,
    name: str,
    loader: Callable[[str, Any], List[Dict[str, Any]]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Read one surface in its own transaction so failures cannot cascade."""
    try:
        from core.db import db_conn
        with db_conn() as conn:
            rows = loader(symbol, conn.cursor())
        return rows, {"status": "available", "count": len(rows), "error": None}
    except Exception as exc:
        LOGGER.warning(
            "symbol timeline source failed symbol=%s source=%s type=%s error=%s",
            symbol, name, type(exc).__name__, str(exc)[:120],
        )
        return [], {"status": "unavailable", "count": 0, "error": "database_query_failed"}


def build_symbol_timeline(symbol: str) -> Dict[str, Any]:
    """Assemble an honest, partial-failure-aware timeline for one symbol."""
    sym = (symbol or "").strip().upper()
    if not _SYMBOL_RE.fullmatch(sym):
        raise ValueError("invalid equity symbol")

    squeeze, squeeze_status = _read_db_surface(sym, "squeeze", _squeeze_rows)
    obs, observation_status = _read_db_surface(sym, "observation", _observation_rows)
    news, news_status = _read_db_surface(sym, "news", _news_rows)
    external, external_status = _read_db_surface(sym, "external", _external_rows)
    sources = {
        "squeeze": squeeze_status,
        "observation": observation_status,
        "news": news_status,
        "external": external_status,
    }

    # Merge into one chronological list (newest first).
    events: List[Dict[str, Any]] = []
    for s in squeeze:
        events.append({"ts": s["ts"], "surface": "squeeze", **s})
    for o in obs:
        events.append({"ts": o["ts"], "surface": "observation", **o})
    for n in news:
        events.append({"ts": n["ts"], "surface": "news", **n})
    for item in external:
        events.append({"ts": item["ts"], "surface": "external_discovery", **item})
    events.sort(key=lambda e: e.get("ts") or 0, reverse=True)

    # Current state from the live scan report.
    current = {"watch": None, "candidate": None, "leader": None}
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
        sources["current"] = {
            "status": "available",
            "count": sum(value is not None for value in current.values()),
            "error": None,
        }
    except Exception as exc:
        LOGGER.warning("symbol timeline current state failed symbol=%s error=%s", sym, str(exc)[:100])
        sources["current"] = {
            "status": "unavailable",
            "count": 0,
            "error": "current_state_unavailable",
        }

    historical = ("squeeze", "observation", "news", "external")
    available_historical = sum(
        sources[name]["status"] == "available" for name in historical
    )
    failed_sources = [
        name for name, state in sources.items() if state["status"] != "available"
    ]
    if available_historical == 0:
        status, ok, error = "unavailable", False, "timeline_unavailable"
    elif not failed_sources:
        status, ok, error = "complete", True, None
    else:
        status, ok, error = "partial", True, None

    return {
        "ok": ok,
        "status": status,
        "error": error,
        "symbol": sym,
        "events": events,
        "event_count": len(events),
        "current": current,
        "sources": sources,
        "failed_sources": failed_sources,
        "note": "Unified detection timeline across squeeze radar, observations, "
                "news, and advisory external discovery. Detection history is "
                "visible even when no trade fired.",
    }
