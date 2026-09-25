"""The observe-all control arm (control_arm_v1): do the approval rules pick better stocks?

Audit 2026-09-25 (C1, "first experiment capable of demonstrating genuine predictive
improvement"). Each intraday strategy gets at most ~3 fills a day, so judging a strategy
against ZERO takes many months. This asks a different, faster question: among the names the
intraday radar noticed, did the ones an experiment APPROVED (issued a forecast on) do better
than the ones none approved -- at the same frozen levels, from the same moment?

After the close, EVERY name the radar detected that day (edge_radar rows) is graded:
  * entry reference = the price at first-seen + 60 s ("auto") and + 300 s ("human"): the
    close of the last 1-minute bar that ENDED by then (none if that bar is > 5 min old);
  * the intraday specs' frozen levels: buy stop at ref x 1.002 (limit x 1.01), +5% target,
    -3% stop, $1,000, entry window 20 minutes from the reference time, time exit 15:30 ET;
  * the existing stop-limit simulation (edge/resolver.py resolve_execution) at 10 and 25 bps
    a side -- four variants per name;
  * on the day's own live feed (edge/feeds.py live_feed), recorded on every row. IEX and SIP
    rows are different regimes and are never pooled.

Each row is labelled APPROVED (some intraday experiment recorded a forecast on the name that
day) or UNAPPROVED. Unapproved rows are a labelled CONTROL: they live only in edge_control,
never in the forecasts table, so no card, paper order or experiment record can read them.

The design below is frozen and hashed like promotion_v1: changing any of it is a new version,
and a stored design whose hash differs is refused.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date, datetime
from statistics import NormalDist
from typing import Any, Dict, List, Optional, Tuple

from edge import stats
from edge.contracts import COUNTED, ET, GAP_AND_GO_V1, WIN, ContractError, FrozenSpecError, issue_intraday
from edge.resolver import resolve_execution

# The levels, copied (not referenced) so a later change to an intraday spec cannot move the
# control silently. tests/test_edge_control.py checks they still equal the intraday specs'.
CONTROL_SPEC = replace(
    GAP_AND_GO_V1, name="control_arm", version=1,
    description="observe-all control: every radar name graded at the intraday specs' frozen levels",
    trigger_mult=1.002, limit_mult=1.01, target_mult=1.05, stop_mult=0.97,
    entry_expiry_et="14:50", time_exit_et="15:30", size_usd=1000.0, max_per_day=10_000,
    eligibility={"entry_window_min": 20, "population": "every edge_radar name of the day"},
)

DELAYS = {"auto": 60, "human": 300}
COSTS_BPS = (10, 25)
VARIANTS = tuple(f"{d}_{c}bps" for d in DELAYS for c in COSTS_BPS)
MAX_REF_AGE_S = 300        # a reference bar that ended more than 5 minutes earlier is no price
CHUNK = 10                 # symbols per minute-bar request (never keep a truncated answer)
MAX_PAGES = 10
NO_REFERENCE, DATA_TRUNCATED = "NO_REFERENCE", "DATA_TRUNCATED"
APPROVED, UNAPPROVED = "APPROVED", "UNAPPROVED"

DESIGN = {
    "version": "control_arm_v1",
    "hypothesis": ("the approval rules add value: among names the intraday radar detected, those that "
                   "got an intraday forecast (APPROVED) win more often than those that did not "
                   "(UNAPPROVED), at the same frozen levels from the same moment"),
    "population": "every edge_radar row of the session (first_seen = detected_at)",
    "labels": {APPROVED: "a forecast from any intraday experiment on that symbol that session",
               UNAPPROVED: "never a forecast; a labelled control, never shown as a pick, never paper-traded"},
    "levels_spec_hash": CONTROL_SPEC.spec_hash(),
    "levels": {"trigger_mult": 1.002, "limit_mult": 1.01, "target_mult": 1.05, "stop_mult": 0.97,
               "size_usd": 1000.0, "entry_window_min": 20, "time_exit_et": "15:30",
               "simulation": "edge/resolver.py resolve_execution (stop-limit, conservative fill bar)"},
    "entry_delays_s": DELAYS,
    "reference": ("close of the last 1-minute bar that ended at or before first_seen + delay; "
                  f"no reference when that bar ended more than {MAX_REF_AGE_S} s earlier"),
    "cost_bps_per_side": list(COSTS_BPS),
    "variants": list(VARIANTS),
    "feed": "the day's live feed (edge/feeds.py); IEX and SIP analysed separately, never pooled",
    "win": "simulated outcome WIN (target before stop); filled = WIN, LOSS or TIME_EXIT",
    "break_even": 0.375,
    "min_approved_fills": 150,
    "alpha": 0.05,
    "tests": len(VARIANTS),           # Bonferroni: every variant tried is counted
    "success": ("in one feed regime and variant, at >= 150 approved fills: the approved-minus-unapproved "
                "win-rate difference interval excludes 0 (lower bound > 0) AND the approved Wilson lower "
                "bound exceeds the 37.5% break-even and that variant's break-even after its costs; "
                "intervals at alpha 0.05 / 4 variants"),
    "kill": ("in a feed regime, at >= 150 approved fills in every variant, approved does not beat "
             "unapproved (difference lower bound <= 0) in any variant: the approval rules add nothing "
             "demonstrable and should be simplified"),
    "headline_variant": "human_25bps",
}
DESIGN_HASH = hashlib.sha256(json.dumps(DESIGN, sort_keys=True).encode()).hexdigest()[:16]
LABEL = ("control arm: every radar name graded at the intraday specs' frozen levels, approved or "
         "not; UNAPPROVED rows are a control, never a pick, never traded")


# ------------------------------------------------------------------ helpers
def _at(day: date, hh: int, mm: int) -> int:
    from edge.intraday import _at as at
    return at(day, hh, mm)


def _hm(ts: Optional[int]) -> Optional[str]:
    return datetime.fromtimestamp(int(ts), tz=ET).strftime("%H:%M:%S ET") if ts else None


def register(store, *, now: int) -> Dict[str, Any]:
    """Store the design and its hash once; a different design under the same version is refused."""
    row = store.get("edge_control_design", DESIGN["version"])
    if row:
        if row.get("design_hash") != DESIGN_HASH:
            raise FrozenSpecError(f"{DESIGN['version']} is frozen (hash {row.get('design_hash')}); "
                                  "a changed design is a new version")
        return row
    row = {"version": DESIGN["version"], "design_hash": DESIGN_HASH,
           "design": json.dumps(DESIGN, sort_keys=True), "registered_at": int(now)}
    store.put("edge_control_design", DESIGN["version"], row)
    return row


def break_even_after_costs(cost_bps: float, spec=CONTROL_SPEC) -> float:
    """Win rate at which a filled trade breaks even once each side pays `cost_bps`."""
    c = cost_bps / 10_000
    net = (1 - c) / (1 + c)
    gain = spec.target_mult * net - 1
    loss = 1 - spec.stop_mult * net
    return loss / (gain + loss)


def reference(bars: List[tuple], at: int) -> Optional[Tuple[float, int]]:
    """(price, bar_start) at `at`: the close of the last bar that ended by then, if fresh."""
    done = [b for b in bars if b[0] + 60 <= at]
    if not done:
        return None
    last = max(done, key=lambda b: b[0])
    if at - (last[0] + 60) > MAX_REF_AGE_S:
        return None
    return last[4], last[0]


def grade_symbol(symbol: str, day: date, first_seen: int, bars: List[tuple]) -> Dict[str, Any]:
    """Every variant's simulated outcome for one name. Pure: bars in, results out."""
    refs, variants = {}, {}
    for name, delay in DELAYS.items():
        at = int(first_seen) + delay
        ref = reference(bars, at)
        if ref is None:
            refs[name] = {"at": at, "price": None, "note": "no 1-minute bar in the 5 minutes before"}
            for c in COSTS_BPS:
                variants[f"{name}_{c}bps"] = {"outcome": NO_REFERENCE}
            continue
        try:
            # issue_intraday opens the window 60 s after issuance: issued at `at - 60`, the entry
            # window runs from the reference time for 20 minutes, the time exit is 15:30 ET.
            f = issue_intraday(CONTROL_SPEC, symbol=symbol, session_date=day, entry_ref=ref[0],
                               issued_at=at - 60)
        except ContractError as exc:
            refs[name] = {"at": at, "price": ref[0], "bar": ref[1], "note": str(exc)}
            for c in COSTS_BPS:
                variants[f"{name}_{c}bps"] = {"outcome": NO_REFERENCE, "note": str(exc)}
            continue
        refs[name] = {"at": at, "price": ref[0], "bar": ref[1], "trigger": f.entry_trigger,
                      "limit": f.entry_limit, "target": f.target, "stop": f.stop, "shares": f.shares,
                      "entry_expiry": f.entry_expiry}
        for c in COSTS_BPS:
            r = resolve_execution(f, bars, cost_bps_per_side=c)
            variants[f"{name}_{c}bps"] = {"outcome": r.outcome, "pnl_usd": r.pnl_usd, "pnl_pct": r.pnl_pct,
                                          "entry_fill": r.entry_fill, "exit_price": r.exit_price,
                                          "ambiguous": r.ambiguous, "note": r.note}
    return {"refs": refs, "variants": variants}


def _approvals(store, ds: str) -> Dict[str, List[str]]:
    """{symbol: [experiment ids]} for every intraday forecast recorded that session."""
    from edge import intraday as I
    ids = {s.experiment_id for s in I.INTRADAY_SPECS}
    out: Dict[str, List[str]] = {}
    for f in store.scan("forecasts", session_date=ds):
        if f.get("experiment_id") in ids:
            out.setdefault(str(f["symbol"]).upper(), []).append(f["experiment_id"])
    return {s: sorted(set(v)) for s, v in out.items()}


def _fetch(get, syms: List[str], day: date, feed: str) -> Tuple[Dict[str, List[tuple]], List[str]]:
    """({symbol: bars}, [truncated symbols]). A truncated chunk is re-asked one symbol at a
    time; a symbol still truncated is left out -- never graded on a partial day."""
    from edge.intraday import _bars, _iso
    from edge.providers import alpaca as A
    start, end = _iso(_at(day, 9, 30)), _iso(_at(day, 16, 0))

    def ask(group):
        return A.bars_pages(get, group, timeframe="1Min", start=start, end=end, feed=feed, max_pages=MAX_PAGES)

    out, bad = {}, []
    for i in range(0, len(syms), CHUNK):
        chunk = syms[i:i + CHUNK]
        rows, ok = ask(chunk)
        if ok:
            out.update({s: _bars(rows.get(s) or []) for s in chunk})
            continue
        for s in chunk:
            one, ok1 = ask([s])
            if ok1:
                out[s] = _bars(one.get(s) or [])
            else:
                bad.append(s)
    return out, bad


# ------------------------------------------------------------------ the daily step
def grade_day(get, store, *, day: date, now: int) -> Dict[str, Any]:
    """Grade every radar name of `day` once, after the close. Idempotent: a complete day is
    never re-graded; a day left incomplete by truncated data re-grades only what is missing."""
    ds = day.isoformat()
    if now < _at(day, 16, 20):
        return {"status": "too_early"}
    prior = store.get("edge_control", ds)
    if prior and prior.get("complete"):
        return {"status": "already_graded", "rows": len(prior.get("rows") or [])}
    register(store, now=now)
    radar = store.scan("edge_radar", session_date=ds)
    if not radar:            # e.g. an early close: nothing detected, nothing to grade
        store.put("edge_control", ds, {"day": ds, "graded_at": now, "feed": None, "design_version": DESIGN["version"],
                                       "design_hash": DESIGN_HASH, "complete": True, "truncated": [],
                                       "rows": [], "label": LABEL})
        return {"status": "no_radar"}
    approvals = _approvals(store, ds)
    if prior:
        feed = prior["feed"]                       # a day is graded on one feed, start to finish
    else:
        from edge import feeds as FD
        feed = FD.live_feed(get, store, now=now)
    kept = {r["symbol"]: r for r in (prior or {}).get("rows") or [] if r.get("variants")}
    todo = sorted({str(it["symbol"]).upper() for it in radar} - set(kept))
    bars, bad = _fetch(get, todo, day, feed) if todo else ({}, [])
    rows = []
    for it in sorted(radar, key=lambda r: (r.get("detected_at") or 0, r["symbol"])):
        sym = str(it["symbol"]).upper()
        if sym in kept:
            rows.append(kept[sym])
            continue
        by = approvals.get(sym) or []
        row = {"symbol": sym, "first_seen": it.get("detected_at"), "first_seen_et": _hm(it.get("detected_at")),
               "detected_move_pct": it.get("detected_move_pct"), "radar_state": it.get("state"),
               "radar_blocker": (it.get("blocker") or {}).get("reasons"),
               "feed": feed, "approved": bool(by), "label": APPROVED if by else UNAPPROVED,
               "approved_by": by}
        if sym in bad:
            row.update({"data": DATA_TRUNCATED, "variants": None})
        else:
            row.update(grade_symbol(sym, day, int(it["detected_at"]), bars.get(sym) or []))
        rows.append(row)
    complete = not bad
    store.put("edge_control", ds, {"day": ds, "graded_at": now, "feed": feed, "design_version": DESIGN["version"],
                                   "design_hash": DESIGN_HASH, "complete": complete, "truncated": bad,
                                   "rows": rows, "label": LABEL})
    return {"status": "graded" if complete else "partial", "feed": feed, "rows": len(rows),
            "approved": sum(1 for r in rows if r["approved"]), "truncated": bad}


# ------------------------------------------------------------------ the readout
def _z() -> float:
    return NormalDist().inv_cdf(1 - DESIGN["alpha"] / DESIGN["tests"] / 2)


def _rate(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Wilson 95% interval for display; the decision uses the Bonferroni-corrected interval."""
    filled = [r for r in results if r.get("outcome") in COUNTED]
    n, k = len(filled), sum(1 for r in filled if r["outcome"] == WIN)
    lo, hi = stats.wilson(k, n) if n else (None, None)
    exp = stats.expectancy([r.get("pnl_usd") or 0.0 for r in filled])
    return {"rows": len(results), "filled": n, "wins": k, "win_rate": round(k / n, 4) if n else None,
            "wilson_95": [round(lo, 4), round(hi, 4)] if n else None,
            "no_fill": sum(1 for r in results if r.get("outcome") == "NO_FILL"),
            "expectancy_usd": round(exp["mean"], 2) if exp["mean"] is not None else None,
            "verdict": stats.break_even_verdict(k, n, DESIGN["break_even"], style="ledger")}


def evaluate(variant: str, a: Dict[str, Any], u: Dict[str, Any]) -> Dict[str, Any]:
    """The preregistered decision for one variant in one feed regime."""
    need = DESIGN["min_approved_fills"]
    cost = int(variant.rsplit("_", 1)[1].replace("bps", ""))
    be_cost = break_even_after_costs(cost)
    bar = max(DESIGN["break_even"], be_cost)
    z = _z()
    d95 = stats.diff_ci(a["wins"], a["filled"], u["wins"], u["filled"])
    dz = stats.diff_ci(a["wins"], a["filled"], u["wins"], u["filled"], z=z)
    alo = stats.wilson(a["wins"], a["filled"], z=z)[0] if a["filled"] else None
    out = {"difference": round(d95[0], 4) if d95 else None,
           "difference_ci_95": [round(d95[1], 4), round(d95[2], 4)] if d95 else None,
           "difference_ci_decision": [round(dz[1], 4), round(dz[2], 4)] if dz else None,
           "approved_low_decision": round(alo, 4) if alo is not None else None,
           "break_even_after_costs": round(be_cost, 4)}
    if a["filled"] < need:
        out["decision"] = "TOO_FEW"
        out["why"] = f"too few approved fills ({a['filled']}/{need}); no conclusion either way"
    elif dz is None:
        out["decision"] = "TOO_FEW"
        out["why"] = "no unapproved fills to compare against"
    elif dz[1] > 0 and alo is not None and alo > bar:
        out["decision"] = "SUCCESS"
        out["why"] = (f"approved beats unapproved (difference low {dz[1]:.1%} > 0) and its Wilson low "
                      f"{alo:.1%} clears break-even {bar:.1%} after {cost} bps a side")
    elif dz[1] <= 0:
        out["decision"] = "KILL"
        out["why"] = f"approved does not beat unapproved (difference low {dz[1]:.1%} <= 0)"
    else:
        out["decision"] = "SELECTION_ONLY"
        out["why"] = (f"approved beats unapproved, but its Wilson low {alo:.1%} does not clear "
                      f"break-even {bar:.1%} after {cost} bps a side")
    return out


def summary(store) -> Dict[str, Any]:
    """Approved vs unapproved, per feed regime (never pooled) and per variant, cumulative."""
    days = sorted(store.scan("edge_control"), key=lambda d: d.get("day") or "")
    regimes: Dict[str, Dict[str, Any]] = {}
    for d in days:
        for r in d.get("rows") or []:
            if not r.get("variants"):
                continue
            g = regimes.setdefault(r.get("feed") or "iex", {"days": set(), "rows": []})
            g["days"].add(d["day"])
            g["rows"].append(r)
    out: Dict[str, Any] = {}
    for feed, g in sorted(regimes.items()):
        per = {}
        for v in VARIANTS:
            a = _rate([r["variants"][v] for r in g["rows"] if r["approved"]])
            u = _rate([r["variants"][v] for r in g["rows"] if not r["approved"]])
            per[v] = {"approved": a, "unapproved": u, **evaluate(v, a, u)}
        decisions = [p["decision"] for p in per.values()]
        if "SUCCESS" in decisions:
            overall = "SUCCESS"
        elif all(x == "KILL" for x in decisions):
            overall = "KILL"
        elif "TOO_FEW" in decisions:
            overall = "TOO_FEW"
        else:
            overall = "UNDECIDED"
        out[feed] = {"sessions": len(g["days"]), "first_day": min(g["days"]), "last_day": max(g["days"]),
                     "graded_names": len(g["rows"]), "approved_names": sum(1 for r in g["rows"] if r["approved"]),
                     "decision": overall, "variants": per}
    current = "sip" if "sip" in out else ("iex" if "iex" in out else None)
    return {"design_version": DESIGN["version"], "design_hash": DESIGN_HASH,
            "break_even": DESIGN["break_even"], "min_approved_fills": DESIGN["min_approved_fills"],
            "alpha_per_variant": round(DESIGN["alpha"] / DESIGN["tests"], 4),
            "hypothesis": DESIGN["hypothesis"], "success": DESIGN["success"], "kill": DESIGN["kill"],
            "days": len(days), "current_regime": current, "regimes": out,
            "headline": headline(out, current), "label": LABEL}


def headline(regimes: Dict[str, Any], current: Optional[str]) -> str:
    """One line for the evening report."""
    if not current:
        return "control arm: nothing graded yet (runs after the close, 16:20-20:00 ET)"
    g = regimes[current]
    v = DESIGN["headline_variant"]
    p = g["variants"][v]
    a, u = p["approved"], p["unapproved"]

    def rate(x):
        if not x["filled"]:
            return "no fills"
        lo, hi = x["wilson_95"]
        return f"{x['win_rate']:.0%} [{lo:.0%}-{hi:.0%}] of {x['filled']}"

    return (f"control arm ({current.upper()}, {g['sessions']} sessions, {v}): approved {rate(a)} vs "
            f"unapproved {rate(u)}; break-even 37.5%; {g['decision']} -- {p['why']}")


def view(store, day: Optional[str] = None) -> Dict[str, Any]:
    """The readout: the cumulative summary, and one day's rows (latest by default), compact."""
    s = summary(store)
    d = day or max((r.get("day") or "" for r in store.scan("edge_control")), default=None)
    rec = store.get("edge_control", d) if d else None
    if rec:
        rows = [{"symbol": r["symbol"], "label": r["label"] if r["approved"] else "UNAPPROVED (control, not a pick)",
                 "approved_by": r.get("approved_by"), "first_seen_et": r.get("first_seen_et"), "feed": r.get("feed"),
                 "outcomes": {v: (r["variants"] or {}).get(v, {}).get("outcome") for v in VARIANTS}
                 if r.get("variants") else r.get("data"),
                 "pnl_usd_25bps": {k: (r["variants"] or {}).get(f"{k}_25bps", {}).get("pnl_usd") for k in DELAYS}
                 if r.get("variants") else None}
                for r in rec.get("rows") or []]
        s["day"] = {"day": rec["day"], "feed": rec.get("feed"), "complete": rec.get("complete"),
                    "truncated": rec.get("truncated"), "names": len(rows),
                    "approved": sum(1 for r in rec.get("rows") or [] if r.get("approved")), "rows": rows[:60]}
    else:
        s["day"] = None
    return s
