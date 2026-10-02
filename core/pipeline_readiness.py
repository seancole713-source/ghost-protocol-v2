"""Pipeline readiness: did today's due jobs actually complete? (audit I02)

Liveness (``/health``, ``/api/health``) answers "is the process up and the DB
reachable" -- Railway's healthcheck depends on it. It must NOT answer "did the
pipeline do today's work": on 2026-10-02 the edge morning card never appeared
and the `today` readout quietly served Oct 1 while liveness read healthy.

This module is the separate readiness answer. It is read-only, never raises,
and is NEVER folded into the liveness score or HTTP status. Each component
reports one of:

  ok        the due work is present and fresh
  pending   not due yet today (its deadline is later)
  not_due   nothing is due now (non-trading day, early close, window closed)
  missing   it was due and is not there
  stale     it exists but is older than its freshness bound
  degraded  present but self-reports degraded
  unknown   could not be read (the error is named)

Components:
  edge_morning_card  the edge shadow card for today's session, due 09:30 ET
  intraday_ticks     the edge intraday radar heartbeat, fresh during 09:45-15:00 ET
  agent_workflow     core.agent_workflow.workflow_health() degraded state
  scan_coverage      the squeeze scan's usable-evidence coverage and age
"""
from __future__ import annotations

import time
from datetime import datetime, time as dtime
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

CARD_DUE_ET = (9, 30)
INTRADAY_FIRST_DUE_ET = (9, 55)    # first tick 09:45, one 5-min tick + grace
INTRADAY_LAST_TICK_ET = (14, 30)   # edge.intraday.ISSUE_UNTIL: the radar stops ticking here
INTRADAY_CHECK_UNTIL_ET = (15, 0)
INTRADAY_FRESH_S = 15 * 60         # three missed 5-minute ticks
SCAN_FRESH_S = 15 * 60
RTH = ((9, 30), (16, 0))

_OK_STATES = frozenset({"ok", "pending", "not_due"})


def _at(day, hm) -> int:
    return int(datetime.combine(day, dtime(*hm), tzinfo=ET).timestamp())


def _err(exc: BaseException) -> str:
    try:
        from shared.redaction import redact_exc
        return redact_exc(exc, 160)
    except Exception:  # noqa: BLE001
        return type(exc).__name__


def _session(day, store, now: int) -> Dict[str, Any]:
    from edge import calendar as CAL
    s = CAL.session(day, store, now)
    return {"date": day.isoformat(), "trading": bool(s.get("trading")),
            "early_close": bool(s.get("early_close")), "calendar_source": s.get("source")}


def card_component(store, sess: Dict[str, Any], now: int) -> Dict[str, Any]:
    day = datetime.fromtimestamp(now, tz=ET).date()
    due_at = _at(day, CARD_DUE_ET)
    base = {"due_by_et": "09:30", "session": sess["date"]}
    if not sess["trading"]:
        return {**base, "status": "not_due", "note": "not a trading day"}
    card = store.get("edge_cards", sess["date"])
    if card:
        out = {**base, "status": "degraded" if card.get("degraded") else "ok",
               "card_status": card.get("status"),
               "forecasts": len(card.get("forecasts") or []),
               "issued_at": card.get("issued_at") or card.get("created_at")}
        if card.get("degraded"):
            out["note"] = "card issued degraded (a data source failed)"
        return out
    if now < due_at:
        return {**base, "status": "pending", "note": "the card is written 09:05-09:28 ET"}
    return {**base, "status": "missing", "note": f"no edge morning card for {sess['date']} by 09:30 ET"}


def intraday_component(store, sess: Dict[str, Any], now: int) -> Dict[str, Any]:
    day = datetime.fromtimestamp(now, tz=ET).date()
    base = {"window_et": "09:45-15:00", "fresh_within_s": INTRADAY_FRESH_S, "session": sess["date"]}
    if not sess["trading"] or sess["date"] != day.isoformat():
        return {**base, "status": "not_due", "note": "not a trading day"}
    if sess["early_close"]:
        return {**base, "status": "not_due", "note": "early close: the intraday radar does not run"}
    from edge.intraday import TICK_TABLE
    beat = store.get(TICK_TABLE, sess["date"]) or {}
    detail = {k: beat.get(k) for k in ("last_tick_at", "last_ok_at", "status", "ticks", "errors",
                                       "error", "source_errors") if k in beat}
    if now < _at(day, INTRADAY_FIRST_DUE_ET):
        return {**base, **detail, "status": "pending", "note": "first intraday tick is due by ~09:55 ET"}
    if now >= _at(day, INTRADAY_CHECK_UNTIL_ET):
        return {**base, **detail, "status": "not_due", "note": "intraday window closed"}
    if not beat:
        return {**base, "status": "missing", "note": "no intraday radar tick recorded this session"}
    expected = min(now, _at(day, INTRADAY_LAST_TICK_ET))
    last_ok = beat.get("last_ok_at")
    age = (expected - int(last_ok)) if last_ok else None
    if last_ok is None or age > INTRADAY_FRESH_S:
        note = ("no successful intraday tick this session" if last_ok is None
                else f"last successful intraday tick {age // 60} min before it was due")
        return {**base, **detail, "status": "stale", "age_s": age, "note": note}
    return {**base, **detail, "status": "ok", "age_s": age}


def workflow_component(workflow: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    wf = workflow() or {}
    issues = list(wf.get("issues") or wf.get("degraded_reasons") or [])
    return {"status": "ok" if wf.get("ok") and not issues else "degraded",
            "issues": issues, "workers": wf.get("workers"), "stale_leases": wf.get("stale_leases")}


def scan_component(report: Dict[str, Any], sess: Dict[str, Any], now: int) -> Dict[str, Any]:
    from core.squeeze_monitor import scan_coverage
    report = report or {}
    ts = report.get("ts")
    try:
        age = int(now - float(ts)) if ts else None
    except (TypeError, ValueError):
        age = None
    cov = scan_coverage(report)
    out = {**cov, "last_scan_ts": ts, "age_s": age}
    day = datetime.fromtimestamp(now, tz=ET).date()
    in_rth = (sess["trading"] and sess["date"] == day.isoformat()
              and _at(day, RTH[0]) <= now < _at(day, RTH[1]))
    if ts is None:
        return {**out, "status": "missing" if in_rth else "not_due", "note": "no completed scan recorded"}
    if in_rth and (age is None or age > SCAN_FRESH_S):
        return {**out, "status": "stale", "note": f"last completed scan older than {SCAN_FRESH_S // 60} min"}
    if cov.get("coverage_degraded"):
        return {**out, "status": "degraded",
                "note": "too few symbols had usable evidence; no alert is not evidence of no activity"}
    return {**out, "status": "ok"}


def slim(full: Dict[str, Any]) -> Dict[str, Any]:
    """The public view: per-component status and module-authored notes only (no worker
    counts, error text or provider detail -- those stay behind the admin cookie)."""
    comps = {name: {k: c.get(k) for k in ("status", "note") if c.get(k) is not None}
             for name, c in (full.get("components") or {}).items()}
    sess = {k: (full.get("session") or {}).get(k) for k in ("date", "trading", "early_close")}
    return {**{k: full.get(k) for k in ("ready", "status", "failing", "as_of", "as_of_et", "note")},
            "session": sess, "components": comps}


def _default_store():
    from core.db import db_conn
    from edge.store_pg import PostgresStore
    return PostgresStore(db_conn)


def _default_workflow() -> Dict[str, Any]:
    from core.agent_workflow import workflow_health
    return workflow_health()


def _default_scan() -> Dict[str, Any]:
    from core.squeeze_monitor import get_squeeze_status
    return get_squeeze_status()


def build_readiness(now: Optional[int] = None, *, store=None,
                    workflow: Optional[Callable[[], Dict[str, Any]]] = None,
                    scan_report: Optional[Callable[[], Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Readiness of today's due pipeline work. Never raises; never affects liveness."""
    now = int(time.time()) if now is None else int(now)
    day = datetime.fromtimestamp(now, tz=ET).date()
    components: Dict[str, Dict[str, Any]] = {}
    try:
        store = store if store is not None else _default_store()
    except Exception as exc:  # noqa: BLE001
        store, store_err = None, _err(exc)
    else:
        store_err = None
    try:
        sess = _session(day, store, now)
    except Exception as exc:  # noqa: BLE001
        sess = {"date": day.isoformat(), "trading": day.weekday() < 5, "early_close": False,
                "calendar_source": "weekday_fallback", "calendar_error": _err(exc)}

    def _run(name, fn):
        try:
            components[name] = fn()
        except Exception as exc:  # noqa: BLE001 - one unreadable component never hides the rest
            components[name] = {"status": "unknown", "error": _err(exc)}

    if store is None:
        components["edge_morning_card"] = {"status": "unknown", "error": store_err}
        components["intraday_ticks"] = {"status": "unknown", "error": store_err}
    else:
        _run("edge_morning_card", lambda: card_component(store, sess, now))
        _run("intraday_ticks", lambda: intraday_component(store, sess, now))
    _run("agent_workflow", lambda: workflow_component(workflow or _default_workflow))
    _run("scan_coverage", lambda: scan_component((scan_report or _default_scan)(), sess, now))

    failing = sorted(k for k, v in components.items() if v.get("status") not in _OK_STATES)
    return {
        "ready": not failing,
        "status": "ready" if not failing else "not_ready",
        "failing": failing,
        "as_of": now,
        "as_of_et": datetime.fromtimestamp(now, tz=ET).isoformat(),
        "session": sess,
        "components": components,
        "note": ("Readiness of today's due pipeline jobs. Separate from liveness: it never "
                 "changes the /health score or HTTP status."),
    }
