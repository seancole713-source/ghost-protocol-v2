"""core/news_defense.py — defensive news tripwire (PR #134). SHIPPED DARK.

Uses fresh high-materiality bearish events to protect ACTIVE picks — never to
fire new ones. Defense first, offense only after shadow proof (merged plan §5).

Flags:
  NEWS_DEFENSE_ENABLED  (default 0)     — master switch, ships OFF
  NEWS_DEFENSE_MODE     (default warn)  — warn: log + ghost_state note only
                                          withdraw: mark the pick WITHDRAWN
Thresholds:
  NEWS_DEFENSE_MIN_MATERIALITY (default 0.85)
  NEWS_DEFENSE_MAX_EVENT_AGE_S (default 21600 = 6h)

decide_defense() is a pure function so the policy is unit-testable without a DB.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List

LOGGER = logging.getLogger("ghost.news_defense")

_BEARISH_ACTIONABLE = {
    "going_concern", "bankruptcy_risk", "dilution_or_offering",
    "delisting_notice", "fda_rejection", "guidance_cut", "short_report",
}


def defense_enabled() -> bool:
    return (os.getenv("NEWS_DEFENSE_ENABLED", "0") or "0").strip().lower() in ("1", "on", "true", "yes")


def defense_mode() -> str:
    mode = (os.getenv("NEWS_DEFENSE_MODE", "warn") or "warn").strip().lower()
    return mode if mode in ("warn", "withdraw") else "warn"


def _min_materiality() -> float:
    return float(os.getenv("NEWS_DEFENSE_MIN_MATERIALITY", "0.85"))


def _max_event_age_s() -> int:
    return int(os.getenv("NEWS_DEFENSE_MAX_EVENT_AGE_S", "21600"))


def decide_defense(active_picks: List[Dict[str, Any]],
                   events_by_symbol: Dict[str, List[Dict[str, Any]]],
                   now_ts: int | None = None) -> List[Dict[str, Any]]:
    """Pure policy: which active picks are threatened by which fresh events.

    A pick is threatened when a bearish event of an actionable type, with
    materiality >= threshold, landed AFTER the pick was made and within the
    freshness window. Direction matters: only UP picks are threatened by
    bearish events (DOWN picks would be helped, not hurt).
    """
    now = int(now_ts or time.time())
    min_mat = _min_materiality()
    max_age = _max_event_age_s()
    actions = []
    for pick in active_picks:
        sym = (pick.get("symbol") or "").upper()
        if (pick.get("direction") or "").upper() != "UP":
            continue
        predicted_at = int(pick.get("predicted_at") or 0)
        for ev in events_by_symbol.get(sym, []):
            if ev.get("event_type") not in _BEARISH_ACTIONABLE:
                continue
            if (ev.get("direction_hint") or "") != "bearish":
                continue
            if float(ev.get("materiality") or 0) < min_mat:
                continue
            asof = int(ev.get("asof_ts") or 0)
            if asof <= predicted_at:        # event predates the pick — the
                continue                    # model already saw that tape
            if now - asof > max_age:
                continue
            actions.append({
                "pick_id": pick.get("id"), "symbol": sym,
                "event_type": ev.get("event_type"),
                "materiality": ev.get("materiality"),
                "event_asof_ts": asof,
                "reason": f"fresh {ev.get('event_type')} (materiality {ev.get('materiality')}) after entry",
            })
            break  # one threat per pick is enough
    return actions


def run_defense_check() -> Dict[str, Any]:
    """Scheduled entry point. No-op unless NEWS_DEFENSE_ENABLED=1."""
    if not defense_enabled():
        return {"ok": True, "skipped": "NEWS_DEFENSE_ENABLED=0"}
    from core.db import db_conn, ensure_ghost_state
    from core.news_events import recent_events_for_symbol
    mode = defense_mode()
    now = int(time.time())
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT id, symbol, direction, predicted_at FROM predictions
                   WHERE outcome IS NULL AND expires_at > %s""", (now,))
            picks = [dict(zip(("id", "symbol", "direction", "predicted_at"), r))
                     for r in cur.fetchall()]
            events = {p["symbol"].upper(): recent_events_for_symbol(
                p["symbol"], asof_ts=now, lookback_s=_max_event_age_s(), cur=cur)
                for p in picks}
            actions = decide_defense(picks, events, now_ts=now)
            for act in actions:
                LOGGER.warning("[news_defense] %s pick %s threatened: %s",
                               act["symbol"], act["pick_id"], act["reason"])
                if mode == "withdraw":
                    cur.execute(
                        """UPDATE predictions SET outcome='WITHDRAWN'
                           WHERE id=%s AND outcome IS NULL""", (act["pick_id"],))
            ensure_ghost_state(cur)
            cur.execute(
                "INSERT INTO ghost_state(key,val) VALUES('news_defense_last', %s) "
                "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                (json.dumps({"ts": now, "mode": mode, "checked": len(picks),
                             "actions": actions}, default=str),))
            conn.commit()
        return {"ok": True, "mode": mode, "checked": len(picks), "actions": actions}
    except Exception as exc:
        LOGGER.warning("news defense failed: %s", str(exc)[:140])
        return {"ok": False, "error": str(exc)[:140]}
