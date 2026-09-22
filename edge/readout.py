"""Read-only views of edge's state, sized for a phone or a Claude session.

Everything here reads; nothing writes. Each view says where its numbers come
from, and none of them shows a win rate without its interval.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from edge.ledger import Ledger

VIEWS = ("summary", "today", "experiments", "backtest", "misses", "probe", "universe")


def _latest(store, table: str) -> Optional[Dict[str, Any]]:
    rows = store.scan(table)
    if not rows:
        return None
    key = "day" if "day" in rows[0] else None
    return max(rows, key=lambda r: r.get(key) or "") if key else rows[-1]


def today(store, day: Optional[str] = None) -> Dict[str, Any]:
    card = store.get("edge_cards", day) if day else _latest(store, "edge_cards")
    if not card:
        return {"card": None, "note": "no shadow card recorded yet (first one is written 09:05-09:28 ET)"}
    compact = [{k: r.get(k) for k in ("symbol", "verdict", "reasons", "missing", "baseline_verdict",
                                      "ref_price", "catalyst")} for r in card.get("rows") or []]
    return {"day": card["day"], "forecasts": card.get("forecasts"),
            "baseline_forecasts": card.get("baseline_forecasts"),
            "candidates": card.get("candidates"), "priced": card.get("priced"),
            "health_banner": card.get("health_banner"), "coverage_note": card.get("coverage_note"),
            "rows": compact}


def experiments(store) -> Dict[str, Any]:
    lg = Ledger(store)
    out = {}
    for row in store.scan("experiments"):
        eid = row["experiment_id"]
        rep = lg.report(eid)
        out[eid] = {
            "forecasts": rep["forecasts"], "abstentions": rep["abstentions"], "excluded": rep["excluded"],
            "break_even": rep["break_even"],
            "records": {rec: {k: r.get(k) for k in ("by_outcome", "filled", "wins", "win_rate",
                                                     "win_rate_ci", "verdict")}
                        for rec, r in rep["records"].items()},
        }
    return out


def backtest(store) -> Optional[Dict[str, Any]]:
    rows = store.scan("edge_backtest")
    if not rows:
        return None
    b = max(rows, key=lambda r: r.get("completed_at") or 0)
    return {k: v for k, v in b.items() if k != "sessions_detail"}


def misses(store, day: Optional[str] = None) -> Optional[Dict[str, Any]]:
    m = store.get("edge_miss", day) if day else _latest(store, "edge_miss")
    if not m:
        return None
    rows = sorted(m.get("rows") or [], key=lambda r: -(r.get("move_pct") or 0))[:25]
    return {**{k: v for k, v in m.items() if k != "rows"}, "top_rows": rows}


def universe(store) -> Optional[Dict[str, Any]]:
    u = _latest(store, "edge_universe")
    if not u:
        return None
    return {k: u.get(k) for k in ("day", "count", "known_at", "source")} | {
        "added": (u.get("added") or [])[:50], "removed": (u.get("removed") or [])[:50]}


def view(store, name: str = "summary", day: Optional[str] = None) -> Dict[str, Any]:
    if name not in VIEWS:
        return {"error": f"view must be one of {VIEWS}"}
    if name == "today":
        return today(store, day)
    if name == "experiments":
        return experiments(store)
    if name == "backtest":
        return backtest(store) or {"note": "backtest has not run yet (it runs once, overnight)"}
    if name == "misses":
        return misses(store, day) or {"note": "no miss review yet (runs 07:00-09:04 ET for the prior session)"}
    if name == "probe":
        return store.get("edge_probe", "latest") or {"note": "no probe stored yet"}
    if name == "universe":
        return universe(store) or {"note": "no universe snapshot yet (06:00-07:00 ET)"}
    t = today(store)
    return {
        "today": {k: t.get(k) for k in ("day", "forecasts", "baseline_forecasts", "health_banner", "note")},
        "experiments": experiments(store),
        "backtest": (lambda b: b and {"window": b.get("window"), "experiments": b.get("experiments"),
                                      "limits": b.get("limits")})(backtest(store)),
        "latest_misses": (lambda m: m and {k: m.get(k) for k in (
            "day", "movers", "executable", "caught", "recall", "labels", "coverage_note")})(misses(store)),
        "note": "shadow and paper only; nothing here is a trade recommendation",
    }
