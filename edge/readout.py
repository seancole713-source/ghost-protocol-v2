"""Read-only views of edge's state, sized for a phone or a Claude session.

Everything here reads; nothing writes. Each view says where its numbers come
from, and none of them shows a win rate without its interval.
"""
from __future__ import annotations

import time
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

from edge.ledger import Ledger
from edge.resolver import RESOLVER_VERSION

VIEWS = ("summary", "today", "experiments", "backtest", "misses", "probe", "universe",
         "research", "radar", "paper", "models", "scorecard", "notes", "top10", "control")


def _latest(store, table: str) -> Optional[Dict[str, Any]]:
    rows = store.scan(table)
    if not rows:
        return None
    key = "day" if "day" in rows[0] else None
    return max(rows, key=lambda r: r.get(key) or "") if key else rows[-1]


CARD_DUE_ET = (9, 28)  # the shadow card is written 09:05-09:28 ET


def current_session(store=None, now: Optional[int] = None) -> str:
    """Today's ET session date if it trades, else the most recent trading day."""
    from edge import calendar as CAL
    from edge.contracts import ET
    now = int(time.time()) if now is None else int(now)
    d: date = datetime.fromtimestamp(now, tz=ET).date()
    for _ in range(14):
        if CAL.session(d, store, now)["trading"]:
            return d.isoformat()
        d -= timedelta(days=1)
    return datetime.fromtimestamp(now, tz=ET).date().isoformat()


def _stale_note(requested: str, served: str, now: int) -> str:
    from edge.contracts import ET
    et = datetime.fromtimestamp(now, tz=ET)
    head = f"no card for {requested}; serving the latest card ({served}) -- it is NOT today's"
    if et.date().isoformat() == requested and (et.hour, et.minute) < CARD_DUE_ET:
        return head + " (today's card is written 09:05-09:28 ET and is not due yet)"
    return head + " (today's card was due by 09:28 ET and is missing)"


def today(store, day: Optional[str] = None, now: Optional[int] = None) -> Dict[str, Any]:
    """The shadow card. Never silently serves another session as today's: the result always
    carries requested_session (the asked-for day, else today's ET session) and served_session,
    and stale=True with a note when they differ. (audit I02)"""
    now = int(time.time()) if now is None else int(now)
    requested = day or current_session(store, now)
    card = store.get("edge_cards", day) if day else _latest(store, "edge_cards")
    if not card:
        note = (f"no shadow card recorded for {day}" if day else
                "no shadow card recorded yet (first one is written 09:05-09:28 ET)")
        return {"requested_session": requested, "served_session": None, "stale": False,
                "card": None, "note": note}
    served = card.get("day")
    stale = bool(served != requested)
    compact = [{k: r.get(k) for k in ("symbol", "verdict", "reasons", "missing", "baseline_verdict",
                                      "ref_price", "catalyst")} for r in card.get("rows") or []]
    return {"requested_session": requested, "served_session": served, "stale": stale,
            **({"note": _stale_note(requested, served, now)} if stale else {}),
            "day": card["day"], "forecasts": card.get("forecasts"),
            "baseline_forecasts": card.get("baseline_forecasts"),
            "candidates": card.get("candidates"), "priced": card.get("priced"),
            "health_banner": card.get("health_banner"), "health_note": card.get("health_note"),
            "coverage_note": card.get("coverage_note"),
            # Where the candidates came from: today's own premarket scan (counts and its
            # top gappers) vs the movers screener, and any source that failed.
            "premarket_scan": card.get("premarket_scan"),
            "premarket_scan_top": card.get("premarket_scan_top"),
            "movers_stale": card.get("movers_stale"),
            "source_errors": card.get("source_errors"),
            "premarket_coverage": (store.get("edge_pm_coverage", card["day"]) or {}).get("samples"),
            "top10": card.get("top10"),
            "rows": compact}


def _by_day(store, eid: str, regime: Optional[str] = None,
            resolver: str = RESOLVER_VERSION) -> Dict[str, list]:
    """Filled simulated outcomes per session -- only the report's feed regime (never IEX + SIP)
    and only one resolver cohort, the current one by default (never resolver_v1 + v2, NEW-02).
    This is the day-clustered sample promotion judges, so it is the same cohort as the headline."""
    out: Dict[str, list] = {}
    lg, cards = Ledger(store), {}
    for f in store.scan("forecasts", experiment_id=eid):
        if regime is not None and lg.feed_of(f, cards) != regime:
            continue
        if lg.cohort_of(f) != resolver:
            continue
        o = store.get("outcomes", f"{f['forecast_id']}|simulated")
        if o and o.get("outcome") in ("WIN", "LOSS", "TIME_EXIT"):
            out.setdefault(f["session_date"], []).append(o["outcome"] == "WIN")
    return out


def _compact(r: Dict[str, Any]) -> Dict[str, Any]:
    return {k: r.get(k) for k in ("by_outcome", "filled", "wins", "win_rate", "win_rate_ci", "verdict")}


def experiments(store) -> Dict[str, Any]:
    from edge import promotion
    from edge.pipeline import BASE_EID
    lg = Ledger(store)
    reports = {row["experiment_id"]: lg.report(row["experiment_id"]) for row in store.scan("experiments")}
    candidates = [e for e in reports if e != BASE_EID]
    out = {}
    for eid, rep in reports.items():
        out[eid] = {
            "forecasts": rep["forecasts"], "abstentions": rep["abstentions"], "excluded": rep["excluded"],
            "feed_regime": rep.get("feed_regime"), "forecasts_in_regime": rep.get("forecasts_in_regime"),
            "resolver_version": rep.get("resolver_version"), "forecasts_in_cohort": rep.get("forecasts_in_cohort"),
            "break_even": rep["break_even"],
            "records": {rec: _compact(r) for rec, r in rep["records"].items()},
        }
        if rep.get("other_resolvers"):
            # Older resolver cohorts stay visible, labelled -- never in the headline above.
            out[eid]["other_resolvers"] = {
                v: {"label": c["label"], "forecasts": c["forecasts"],
                    "records": {rec: _compact(r) for rec, r in c["records"].items()}}
                for v, c in rep["other_resolvers"].items()}
        if eid != BASE_EID and BASE_EID in reports:
            by_day = _by_day(store, eid, rep.get("feed_regime"), rep.get("resolver_version") or RESOLVER_VERSION)
            out[eid]["retirement"] = promotion.retirement(rep)
            out[eid]["promotion"] = promotion.evaluate(rep, baseline=reports[BASE_EID], sessions=len(by_day),
                                                       by_day=by_day, n_candidates=max(1, len(candidates)))
    return out


def backtest(store) -> Optional[Dict[str, Any]]:
    rows = store.scan("edge_backtest")
    if not rows:
        return None
    b = max(rows, key=lambda r: r.get("completed_at") or 0)
    out = {k: v for k, v in b.items() if k != "sessions_detail"}
    ps = store.scan("edge_backtest_postsplit")
    if ps:
        p = max(ps, key=lambda r: r.get("completed_at") or 0)
        out["post_split_momentum"] = {k: v for k, v in p.items() if k != "trades"}
    from edge import backtest_second_day as SD
    sd = store.scan(SD.TABLE)
    if sd:
        s = max(sd, key=lambda r: r.get("completed_at") or 0)
        out["second_day_open"] = {k: v for k, v in s.items() if k != "trades"}
    else:
        prog = store.get(SD.PROGRESS_TABLE, SD.VERSION)
        if prog:
            out["second_day_open"] = {"status": "in_progress", "version": SD.VERSION,
                                      **{k: prog.get(k) for k in ("start", "end", "done_through", "ticks")}}
    return out


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


def _day_of(store, table: str, day: Optional[str], field: str = "day") -> Optional[str]:
    if day:
        return day
    rows = store.scan(table)
    return max((r.get(field) or "" for r in rows), default=None) or None


def research(store, day: Optional[str] = None) -> Dict[str, Any]:
    d = _day_of(store, "edge_research", day)
    if not d:
        return {"note": "no research yet (08:30-09:28 ET on trading days, when EDGE_RESEARCH_ENABLED)"}
    recs = sorted(store.scan("edge_research", day=d), key=lambda r: r["made_at"])
    return {"day": d, "spent_usd": (store.get("edge_research_budget", d) or {}).get("spent_usd"),
            "symbols": [{"symbol": r["symbol"], "reviewer": r.get("reviewer"), "cost_usd": r.get("cost_usd"),
                         "review": r.get("review"), "unknowns": (r.get("unknowns") or [])[:5],
                         "claims": [{k: c.get(k) for k in ("kind", "statement", "status", "problems")}
                                    for c in r.get("claims") or []]} for r in recs]}


def _clock(key: str, ts: Optional[int]) -> Dict[str, Optional[str]]:
    """An epoch as the operator reads it: Central (where they are) and Eastern (the market)."""
    if not ts:
        return {f"{key}_ct": None, f"{key}_et": None}
    from datetime import datetime
    from zoneinfo import ZoneInfo
    t = datetime.fromtimestamp(int(ts), tz=ZoneInfo("UTC"))
    return {f"{key}_ct": t.astimezone(ZoneInfo("America/Chicago")).strftime("%H:%M CT"),
            f"{key}_et": t.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M ET")}


def radar(store, day: Optional[str] = None) -> Dict[str, Any]:
    d = _day_of(store, "edge_radar", day, "session_date")
    if not d:
        return {"note": "no radar activity yet (09:45-14:30 ET on trading days)"}
    items = store.scan("edge_radar", session_date=d)
    out = []
    for it in sorted(items, key=lambda r: -(r.get("detected_move_pct") or 0)):
        last = (it.get("history") or [{}])[-1]
        b = it.get("blocker") or {}
        out.append({"symbol": it["symbol"], "state": it.get("state"), "strategy": it.get("strategy"),
                    "detected_move_pct": it.get("detected_move_pct"), "last_reason": last.get("reason"),
                    "detected_at": it.get("detected_at"), **_clock("detected_at", it.get("detected_at")),
                    "last_reasons": b.get("reasons") or [], "last_reasons_strategy": b.get("strategy"),
                    "last_reasons_at": b.get("at"), **_clock("last_reasons_at", b.get("at")),
                    "catalyst": (it.get("catalyst") or {}).get("headline"),
                    "news_seen": it.get("news_seen")})
    return {"day": d, "states": dict(Counter(i["state"] for i in out)),
            "items": out[:40]}


def paper(store, day: Optional[str] = None) -> Dict[str, Any]:
    d = _day_of(store, "forecasts", day, "session_date")
    if not d:
        return {"note": "no forecasts yet"}
    from datetime import datetime
    from edge.contracts import COUNTED, ET
    from edge.ledger import entry_filled_after_window, outside_rule_reason

    def _hm(ts):
        return datetime.fromtimestamp(int(ts), tz=ET).strftime("%H:%M:%S ET") if ts else None

    kept = (store.get("edge_paper_orders", d) or {}).get("orders") or []
    broker = {o.get("client_order_id"): o for o in kept}
    exit_keys = ("type", "status", "qty", "filled_qty", "filled_avg_price", "filled_at", "canceled_at")

    def _exits(fid: str, entry: Dict[str, Any], p: Dict[str, Any]) -> list:
        """Every exit the broker recorded: the bracket's target/stop legs, the 15:30 time-exit
        sells (-tx, -tx2...) and any last-tick position close (audit 2026-09-25 U63)."""
        closes = {str(x.get("order_id")) for x in p.get("tx_closes") or [] if x.get("order_id")}
        out = [{"role": "stop" if "stop" in str(g.get("type") or "") else "target",
                **{k: g.get(k) for k in exit_keys}} for g in entry.get("legs") or []]
        for o in kept:
            cid = str(o.get("client_order_id") or "")
            if cid.startswith(f"{fid}-px"):          # protective OCO for a partly filled entry
                out += [{"role": "protective_" + ("stop" if "stop" in str(g.get("type") or "") else "target"),
                         **{k: g.get(k) for k in exit_keys}} for g in [o] + list(o.get("legs") or [])]
            elif cid.startswith(f"{fid}-pf"):        # flatten when that protection failed
                out.append({"role": "flatten", **{k: o.get(k) for k in exit_keys}})
            elif cid.startswith(f"{fid}-tx") or str(o.get("id")) in closes:
                out.append({"role": "time_exit" if cid.startswith(f"{fid}-tx") else "position_close",
                            **{k: o.get(k) for k in exit_keys}})
        return out

    rows = []
    for f in sorted(store.scan("forecasts", session_date=d), key=lambda r: r["issued_at"]):
        p = store.get("edge_paper", f"{f['forecast_id']}|paper") or {}
        act = store.get("outcomes", f"{f['forecast_id']}|actual") or {}
        sim = store.get("outcomes", f"{f['forecast_id']}|simulated") or {}
        entry = broker.get(f"{f['forecast_id']}-entry") or {}
        # One judge for both columns, on the broker's own fill time: a late fill never reads as counted.
        late = entry_filled_after_window(store, f, act)
        why_outside = outside_rule_reason(store, f, act)
        outside = bool(why_outside) or (late and act.get("outcome") in COUNTED)
        rows.append({"experiment": f["experiment_id"], "symbol": f["symbol"],
                     "issued": _hm(f.get("issued_at")), "entry_window_ends": _hm(f.get("entry_expiry")),
                     "why": (f.get("evidence") or {}),
                     "entry_trigger": f.get("entry_trigger"), "entry_limit": f.get("entry_limit"),
                     "target": f.get("target"), "stop": f.get("stop"),
                     "shares": f.get("shares"), "paper_state": p.get("state"), "paper_message": p.get("message"),
                     "simulated": sim.get("outcome"), "simulated_note": sim.get("note"),
                     "actual": act.get("outcome"), "actual_pnl_usd": act.get("pnl_usd"),
                     "actual_counted": bool(act.get("outcome") in COUNTED and not outside and not late),
                     "broker_entry": {k: entry.get(k) for k in ("status", "submitted_at", "filled_at",
                                                                "filled_avg_price", "canceled_at")} if entry else None,
                     "broker_exits": _exits(f["forecast_id"], entry, p),
                     "filled_after_window": late,
                     "note": (f"broker fill outside the rule ({why_outside or 'entry filled after the entry window'}): "
                              "shown, not counted" if outside else None)})
    return {"day": d, "orders": rows, "note": "Alpaca PAPER account only; no real money"}


def models(store) -> Dict[str, Any]:
    cur = store.get("edge_models", "current")
    last = store.get("edge_models", "last_attempt")
    ev = (store.get("edge_models", cur["model_sha"]) or {}).get("evaluation") if cur else None
    return {"current": cur, "current_evaluation": ev,
            "last_attempt": {k: v for k, v in (last or {}).items() if k != "artifact"} or None,
            "note": "a model forecasts only if it QUALIFIED out of sample; otherwise none is used"}


def view(store, name: str = "summary", day: Optional[str] = None, kind: Optional[str] = None,
         now: Optional[int] = None) -> Dict[str, Any]:
    """One named view. Every string is passed through shared.redaction on the way out: stored
    provider errors (source_errors, notes, skipped days) can embed a request URL whose query
    carries an API key (Polygon's apiKey=), and this is what MCP and the Railway log read."""
    from shared.redaction import redact_obj
    return redact_obj(_view(store, name, day, kind, now))


def _view(store, name: str, day: Optional[str], kind: Optional[str],
          now: Optional[int] = None) -> Dict[str, Any]:
    if name not in VIEWS:
        return {"error": f"view must be one of {VIEWS}"}
    if name == "research":
        return research(store, day)
    if name == "radar":
        return radar(store, day)
    if name == "paper":
        return paper(store, day)
    if name == "models":
        return models(store)
    if name == "scorecard":
        from edge import scorecard as SC
        return SC.scorecard(store)
    if name == "top10":
        from edge import top10 as T10
        d = day or T10.latest_day(store)
        return (T10.with_outcomes(store, d) if d else None) or {
            "note": "no Top 10 yet (built with the card, 09:05-09:28 ET)"}
    if name == "control":
        # v2 (point-in-time) is the primary result; v1 rides along labelled exploratory.
        from edge import control_v2 as CA2
        return CA2.view(store, day)
    if name == "notes":
        from edge import agent_notes as AN
        return AN.recent(store, day=day, kind=kind)
    if name == "today":
        return today(store, day, now)
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
    from edge import control_v2 as CA2, scorecard as SC
    t, sc = today(store, None, now), SC.scorecard(store)
    return {
        "today": {k: t.get(k) for k in ("requested_session", "served_session", "stale", "day", "forecasts",
                                        "baseline_forecasts", "health_banner", "note")},
        "experiments": experiments(store),
        "backtest": (lambda b: b and {"window": b.get("window"), "experiments": b.get("experiments"),
                                      "limits": b.get("limits")})(backtest(store)),
        "latest_misses": (lambda m: m and {k: m.get(k) for k in (
            "day", "movers", "executable", "caught", "recall", "labels", "coverage_note")})(misses(store)),
        "ai_scorecard": {"sessions": sc.get("sessions"),
                         **{k: (sc.get(k) or {}).get("verdict") for k in ("keyword_catalyst", "ai_research", "model")}},
        "control_arm": CA2.summary(store)["headline"],
        "control_arm_v1_exploratory": CA2.v1_exploratory(store)["headline"],
        "note": "shadow and paper only; nothing here is a trade recommendation",
    }


# What the scheduled agents read when the Ghost connector is not in their tool list:
# Railway logs. Each stage's views are logged ONCE per day, right after the tick
# that produced them, as `EDGE_VIEW <name> <day> <json>`.
_STAGES = (
    ("morning", lambda o: (o.get("card") or {}).get("status") == "issued", ("today", "paper", "research", "top10")),
    ("misses", lambda o: (o.get("miss_review") or {}).get("status") not in (None, "error"), ("misses",)),
    ("radar", lambda o: (o.get("radar_close") or {}).get("status") == "closed", ("radar",)),
    ("evening", lambda o: (o.get("card_graded") or {}).get("status") == "graded",
     ("paper", "experiments", "scorecard", "top10", "control")),
)


def views_to_log(store, out: Dict[str, Any], *, now: int, limit: int = 20000) -> list:
    """[(name, day, json)] for every stage this tick completed and has not logged yet today."""
    import json
    day = out.get("day")
    if not day:
        return []
    lines = []
    for stage, done, names in _STAGES:
        key = f"{day}|{stage}"
        if not done(out) or store.get("edge_view_logged", key):
            continue
        for n in names:
            v = view(store, n, None if n == "misses" else day)
            lines.append((n, day, json.dumps(v, default=str, separators=(",", ":"))[:limit]))
        store.put("edge_view_logged", key, {"day": day, "stage": stage, "at": now})
    return lines
