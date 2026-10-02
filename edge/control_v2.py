"""Control arm v2 (control_arm_v2): the approval question, asked point-in-time.

control_arm_v1 (edge/control.py, frozen, hash 605453cd49128631) labels a name APPROVED if ANY
intraday forecast was recorded on it that session, and grades it from first_seen + 60 s / 300 s.
A name seen at 09:45 and approved at 13:00 is graded from 09:46 as approved: the label uses
information from three hours after the entry it grades (EDGE-10). v1 stays as it is -- frozen,
same numbers -- and is reported as EXPLORATORY (full-session approval, not point-in-time).

v2 grades every comparison from a DECISION TIME, using only what was known at its entry:
  * APPROVED comparison: decision time = issued_at of the symbol's earliest intraday forecast
    that session; entry reference at issued_at + 60 s ("auto") / + 300 s ("human"). The
    approval is counted only because that forecast's issued_at <= the comparison's entry time.
  * UNAPPROVED comparison: decision time = first_seen (the radar's detected_at); entry at
    first_seen + 60 s / + 300 s, counted only when NO intraday forecast on the symbol was issued
    at or before that entry time. A name approved later is still an unapproved comparison at
    first_seen (that was the state then) and an approved one from its approval.
  * both arms: the same reference rule, frozen levels, simulation, costs and feed as v1; a
    decision time outside the radar's approval window (09:45-14:30 ET) is not counted.
  * intervals clustered by session: whole sessions are resampled (edge/stats.py
    clustered_diff_ci). A decision bound is the LOWER of that and the independent-rows bound
    (Wilson / Newcombe), so clustering can only make a verdict harder, never easier.

Success and kill rules are v1's: >= 150 approved fills, the difference lower bound above 0, the
approved lower bound above the 37.5% break-even and the variant's after-cost break-even, at
alpha 0.05 / 4 variants. Unapproved comparisons are a labelled CONTROL, never a pick, never
traded, never written to `forecasts`.

The design below is frozen and hashed like v1: changing any of it is a new version, and a
stored design whose hash differs is refused.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from statistics import NormalDist
from typing import Any, Dict, List, Optional, Tuple

from edge import control as V1
from edge import stats
from edge.contracts import COUNTED, WIN, FrozenSpecError

APPROVED, UNAPPROVED = V1.APPROVED, V1.UNAPPROVED
DELAYS, COSTS_BPS, VARIANTS = V1.DELAYS, V1.COSTS_BPS, V1.VARIANTS
NO_REFERENCE, DATA_TRUNCATED = V1.NO_REFERENCE, V1.DATA_TRUNCATED
APPROVED_BY_ENTRY = "APPROVED_BY_ENTRY"      # unapproved comparison whose name was approved by its entry
OUTSIDE_WINDOW = "OUTSIDE_APPROVAL_WINDOW"   # decision time outside 09:45-14:30 ET
APPROVAL_WINDOW_ET = ("09:45", "14:30")
BOOT_ITERS, BOOT_SEED = 4000, 7
V1_LABEL = "exploratory (full-session approval, not point-in-time)"

DESIGN = {
    "version": "control_arm_v2",
    "supersedes": ("control_arm_v1, kept frozen and reported as exploratory: it labelled a name approved "
                   "on any forecast that session and graded it from first_seen, so the label used "
                   "information from after the entry it graded"),
    "hypothesis": ("the approval rules add value: among names the intraday radar detected, a comparison "
                   "entered at the moment of an intraday approval (APPROVED) wins more often than one "
                   "entered at the moment a name was seen and not yet approved (UNAPPROVED), at the same "
                   "frozen levels and the same delay from its decision time"),
    "population": "every edge_radar row of the session (first_seen = detected_at)",
    "decision_time": {APPROVED: "issued_at of the symbol's earliest intraday forecast that session",
                      UNAPPROVED: "first_seen"},
    "labels": {APPROVED: ("entered at decision time + delay; counted because the approving forecast's "
                          "issued_at <= the comparison's entry time"),
               UNAPPROVED: ("entered at first_seen + delay; counted only when no intraday forecast on the "
                            "symbol was issued at or before that entry time; a labelled control, never "
                            "shown as a pick, never paper-traded")},
    "approval_window_et": list(APPROVAL_WINDOW_ET),
    "levels_spec_hash": V1.CONTROL_SPEC.spec_hash(),
    "levels": {"trigger_mult": 1.002, "limit_mult": 1.01, "target_mult": 1.05, "stop_mult": 0.97,
               "size_usd": 1000.0, "entry_window_min": 20, "time_exit_et": "15:30",
               "simulation": "edge/resolver.py resolve_execution (stop-limit, conservative fill bar)"},
    "entry_delays_s": DELAYS,
    "reference": ("close of the last 1-minute bar that ended at or before decision time + delay; "
                  f"no reference when that bar ended more than {V1.MAX_REF_AGE_S} s earlier"),
    "cost_bps_per_side": list(COSTS_BPS),
    "variants": list(VARIANTS),
    "feed": "the day's live feed (edge/feeds.py); IEX and SIP analysed separately, never pooled",
    "win": "simulated outcome WIN (target before stop); filled = WIN, LOSS or TIME_EXIT",
    "break_even": 0.375,
    "min_approved_fills": 150,
    "alpha": 0.05,
    "tests": len(VARIANTS),
    "intervals": ("decision bounds are the lower of the independent-rows interval (Wilson; Newcombe for "
                  "the difference) and the session-clustered bootstrap interval, both at alpha 0.05 / 4"),
    "bootstrap": {"cluster": "session date", "iters": BOOT_ITERS, "seed": BOOT_SEED,
                  "resample": "whole sessions with replacement, the same draw for both arms"},
    "success": ("in one feed regime and variant, at >= 150 approved fills: the approved-minus-unapproved "
                "win-rate difference interval excludes 0 (lower bound > 0) AND the approved lower bound "
                "exceeds the 37.5% break-even and that variant's break-even after its costs; intervals at "
                "alpha 0.05 / 4 variants, session-clustered"),
    "kill": ("in a feed regime, at >= 150 approved fills in every variant, approved does not beat "
             "unapproved (difference lower bound <= 0) in any variant: the approval rules add nothing "
             "demonstrable and should be simplified"),
    "headline_variant": "human_25bps",
}
DESIGN_HASH = hashlib.sha256(json.dumps(DESIGN, sort_keys=True).encode()).hexdigest()[:16]
LABEL = ("control arm v2 (point-in-time): each comparison graded from its own decision time -- approval "
         "issued_at, or first_seen while unapproved; UNAPPROVED rows are a control, never a pick, never traded")


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


def _window(day: date) -> Tuple[int, int]:
    (h0, m0), (h1, m1) = (map(int, x.split(":")) for x in APPROVAL_WINDOW_ET)
    return V1._at(day, h0, m0), V1._at(day, h1, m1)


def approvals(store, ds: str) -> Dict[str, List[Tuple[int, str]]]:
    """{symbol: [(issued_at, experiment id)], earliest first} for every intraday forecast that session."""
    from edge import intraday as I
    ids = {s.experiment_id for s in I.INTRADAY_SPECS}
    out: Dict[str, List[Tuple[int, str]]] = {}
    for f in store.scan("forecasts", session_date=ds):
        if f.get("experiment_id") in ids and f.get("issued_at") is not None:
            out.setdefault(str(f["symbol"]).upper(), []).append((int(f["issued_at"]), f["experiment_id"]))
    return {s: sorted(set(v)) for s, v in out.items()}


def _blank(outcome: str, note: str) -> Dict[str, Any]:
    return {"outcome": outcome, "note": note}


def grade_row(symbol: str, day: date, first_seen: int, approved: List[Tuple[int, str]],
              bars: List[tuple]) -> Dict[str, Any]:
    """Both arms for one radar name. Pure: times and bars in, comparisons out."""
    lo, hi = _window(day)
    approved_at = approved[0][0] if approved else None
    arms: Dict[str, Any] = {}

    # UNAPPROVED: from first_seen, only while no approval had been issued by the entry.
    u = V1.grade_symbol(symbol, day, int(first_seen), bars)
    inside = lo <= int(first_seen) < hi
    for name, delay in DELAYS.items():
        entry = int(first_seen) + delay
        for c in COSTS_BPS:
            v = f"{name}_{c}bps"
            if not inside:
                u["variants"][v] = _blank(OUTSIDE_WINDOW, "first seen outside the approval window")
            elif approved_at is not None and approved_at <= entry:
                u["variants"][v] = _blank(APPROVED_BY_ENTRY, f"approved at {V1._hm(approved_at)}, by this "
                                                             f"entry ({V1._hm(entry)}): not an unapproved comparison")
    arms[UNAPPROVED] = {"decision_at": int(first_seen), "decision_et": V1._hm(first_seen), **u}

    # APPROVED: from the earliest approval; the approval counts because issued_at <= entry.
    if approved_at is not None:
        a = V1.grade_symbol(symbol, day, approved_at, bars)
        by = {name: sorted({e for t, e in approved if t <= approved_at + delay}) for name, delay in DELAYS.items()}
        if not lo <= approved_at < hi:
            for v in VARIANTS:
                a["variants"][v] = _blank(OUTSIDE_WINDOW, "approval issued outside the approval window")
        arms[APPROVED] = {"decision_at": approved_at, "decision_et": V1._hm(approved_at),
                          "approved_by": by, **a}
    return {"approved_at": approved_at, "approved_at_et": V1._hm(approved_at),
            "approvals": [{"issued_at": t, "experiment_id": e} for t, e in approved], "arms": arms}


# ------------------------------------------------------------------ the daily step
def grade_day(get, store, *, day: date, now: int) -> Dict[str, Any]:
    """Grade every radar name of `day` once, after the close (v1's schedule and idempotence)."""
    ds = day.isoformat()
    if now < V1._at(day, 16, 20):
        return {"status": "too_early"}
    prior = store.get("edge_control_v2", ds)
    if prior and prior.get("complete"):
        return {"status": "already_graded", "rows": len(prior.get("rows") or [])}
    register(store, now=now)
    base = {"day": ds, "graded_at": now, "design_version": DESIGN["version"], "design_hash": DESIGN_HASH,
            "resolver_version": V1.RESOLVER_VERSION,
            "label": LABEL}
    radar = store.scan("edge_radar", session_date=ds)
    if not radar:
        store.put("edge_control_v2", ds, {**base, "feed": None, "complete": True, "truncated": [], "rows": []})
        return {"status": "no_radar"}
    appr = approvals(store, ds)
    if prior:
        feed = prior["feed"]                              # one feed per day, start to finish
    else:
        v1 = store.get("edge_control", ds) or {}
        if v1.get("feed"):
            feed = v1["feed"]                             # the same feed as v1's rows that day
        else:
            from edge import feeds as FD
            feed = FD.live_feed(get, store, now=now)
    kept = V1.tag_kept_rows(prior, {r["symbol"]: r for r in (prior or {}).get("rows") or [] if r.get("arms")})
    todo = sorted({str(it["symbol"]).upper() for it in radar} - set(kept))
    bars, bad = V1._fetch(get, todo, day, feed) if todo else ({}, [])
    rows = []
    for it in sorted(radar, key=lambda r: (r.get("detected_at") or 0, r["symbol"])):
        sym = str(it["symbol"]).upper()
        if sym in kept:
            rows.append(kept[sym])
            continue
        row = {"symbol": sym, "first_seen": it.get("detected_at"), "first_seen_et": V1._hm(it.get("detected_at")),
               "detected_move_pct": it.get("detected_move_pct"), "radar_state": it.get("state"),
               "radar_blocker": (it.get("blocker") or {}).get("reasons"), "feed": feed}
        if sym in bad:
            row.update({"data": DATA_TRUNCATED, "arms": None})
        else:
            row.update(grade_row(sym, day, int(it["detected_at"]), appr.get(sym) or [], bars.get(sym) or []))
            row["resolver_version"] = V1.RESOLVER_VERSION
        rows.append(row)
    complete = not bad
    store.put("edge_control_v2", ds, {**base, "feed": feed, "complete": complete, "truncated": bad, "rows": rows})
    return {"status": "graded" if complete else "partial", "feed": feed, "rows": len(rows),
            "approved": sum(1 for r in rows if (r.get("arms") or {}).get(APPROVED)), "truncated": bad}


# ------------------------------------------------------------------ the readout
def _z() -> float:
    return NormalDist().inv_cdf(1 - DESIGN["alpha"] / DESIGN["tests"] / 2)


def _counted(arm: Dict[str, Any], v: str) -> Optional[Dict[str, Any]]:
    r = (arm.get("variants") or {}).get(v)
    if not r or r.get("outcome") in (APPROVED_BY_ENTRY, OUTSIDE_WINDOW):
        return None
    return r


def _tally(results: List[Dict[str, Any]]) -> Tuple[int, int]:
    filled = [r for r in results if r.get("outcome") in COUNTED]
    return sum(1 for r in filled if r["outcome"] == WIN), len(filled)


def evaluate(variant: str, a: Dict[str, Any], u: Dict[str, Any],
             by_day: Dict[str, Tuple[int, int, int, int]]) -> Dict[str, Any]:
    """The preregistered decision for one variant in one feed regime, session-clustered."""
    need = DESIGN["min_approved_fills"]
    cost = int(variant.rsplit("_", 1)[1].replace("bps", ""))
    be_cost = V1.break_even_after_costs(cost)
    bar = max(DESIGN["break_even"], be_cost)
    z = _z()
    alpha = DESIGN["alpha"] / DESIGN["tests"]
    d95 = stats.diff_ci(a["wins"], a["filled"], u["wins"], u["filled"])
    dz = stats.diff_ci(a["wins"], a["filled"], u["wins"], u["filled"], z=z)
    boot = stats.clustered_diff_cis(by_day, (DESIGN["alpha"], alpha), iters=BOOT_ITERS, seed=BOOT_SEED)
    c95, cz = boot[DESIGN["alpha"]], boot[alpha]
    wlo = stats.wilson(a["wins"], a["filled"], z=z)[0] if a["filled"] else None
    # The decision bound is the more conservative of the two: clustering never makes it easier.
    d_lo = min(dz[1], cz["difference"][0]) if dz and cz else None
    a_lo = min(wlo, cz["a"][0]) if wlo is not None and cz else wlo
    r4 = lambda xs: [round(x, 4) for x in xs]   # noqa: E731
    out = {"difference": round(d95[0], 4) if d95 else None,
           "difference_ci_95": r4(d95[1:]) if d95 else None,
           "difference_ci_95_clustered": r4(c95["difference"]) if c95 else None,
           "approved_ci_95_clustered": r4(c95["a"]) if c95 else None,
           "difference_low_decision": round(d_lo, 4) if d_lo is not None else None,
           "approved_low_decision": round(a_lo, 4) if a_lo is not None else None,
           "sessions_with_fills": sum(1 for x in by_day.values() if x[1] or x[3]),
           "break_even_after_costs": round(be_cost, 4)}
    if a["filled"] < need:
        out["decision"] = "TOO_FEW"
        out["why"] = f"too few approved fills ({a['filled']}/{need}); no conclusion either way"
    elif d_lo is None:
        out["decision"] = "TOO_FEW"
        out["why"] = "no unapproved fills to compare against"
    elif d_lo > 0 and a_lo is not None and a_lo > bar:
        out["decision"] = "SUCCESS"
        out["why"] = (f"approved beats unapproved (difference low {d_lo:.1%} > 0, session-clustered) and its "
                      f"low {a_lo:.1%} clears break-even {bar:.1%} after {cost} bps a side")
    elif d_lo <= 0:
        out["decision"] = "KILL"
        out["why"] = f"approved does not beat unapproved (difference low {d_lo:.1%} <= 0, session-clustered)"
    else:
        out["decision"] = "SELECTION_ONLY"
        out["why"] = (f"approved beats unapproved, but its low {a_lo:.1%} does not clear "
                      f"break-even {bar:.1%} after {cost} bps a side")
    return out


def summary(store) -> Dict[str, Any]:
    """Approved vs unapproved comparisons, per feed regime (never pooled) and variant, cumulative.

    Resolver versions are never pooled either (audit NEW-02): `regimes`, the headline and every
    decision read only rows graded by the current resolver; each older cohort is reported under
    `other_resolvers`, labelled legacy. The frozen design and its criteria are untouched."""
    days = sorted(store.scan("edge_control_v2"), key=lambda d: d.get("day") or "")
    cohorts = V1.resolver_cohorts(days, lambda r: r.get("arms"))
    current = V1.current_regime(cohorts)
    out = _regimes(cohorts.pop(V1.RESOLVER_VERSION, []))
    base = {"design_version": DESIGN["version"], "design_hash": DESIGN_HASH, "primary": True,
            "break_even": DESIGN["break_even"], "min_approved_fills": DESIGN["min_approved_fills"],
            "alpha_per_variant": round(DESIGN["alpha"] / DESIGN["tests"], 4),
            "hypothesis": DESIGN["hypothesis"], "success": DESIGN["success"], "kill": DESIGN["kill"],
            "intervals": DESIGN["intervals"], "days": len(days), "current_regime": current,
            "resolver_version": V1.RESOLVER_VERSION, "regimes": out,
            "headline": headline(out, current), "label": LABEL}
    if cohorts:
        base["other_resolvers"] = V1.legacy_cohorts(cohorts, _regimes)
    return base


def _regimes(pairs: List[Tuple[str, Dict[str, Any]]]) -> Dict[str, Any]:
    """{feed: per-variant comparison and decision} for one resolver cohort's (day, row) pairs."""
    regimes: Dict[str, Dict[str, Any]] = {}
    for day, r in pairs:
        g = regimes.setdefault(r.get("feed") or "iex", {"days": set(), "rows": []})
        g["days"].add(day)
        g["rows"].append((day, r))
    out: Dict[str, Any] = {}
    for feed, g in sorted(regimes.items()):
        per = {}
        for v in VARIANTS:
            ar, ur, by_day = [], [], {}
            for day, r in g["rows"]:
                a = _counted(r["arms"].get(APPROVED) or {}, v)
                u = _counted(r["arms"].get(UNAPPROVED) or {}, v)
                ka, na = _tally([a] if a else [])
                ku, nu = _tally([u] if u else [])
                x = by_day.get(day, (0, 0, 0, 0))
                by_day[day] = (x[0] + ka, x[1] + na, x[2] + ku, x[3] + nu)
                ar += [a] if a else []
                ur += [u] if u else []
            ra, ru = V1._rate(ar), V1._rate(ur)
            per[v] = {"approved": ra, "unapproved": ru, **evaluate(v, ra, ru, by_day)}
        decisions = [p["decision"] for p in per.values()]
        if "SUCCESS" in decisions:
            overall = "SUCCESS"
        elif all(x == "KILL" for x in decisions):
            overall = "KILL"
        elif "TOO_FEW" in decisions:
            overall = "TOO_FEW"
        else:
            overall = "UNDECIDED"
        rows = [r for _, r in g["rows"]]
        out[feed] = {"sessions": len(g["days"]), "first_day": min(g["days"]), "last_day": max(g["days"]),
                     "graded_names": len(rows),
                     "approved_names": sum(1 for r in rows if r["arms"].get(APPROVED)),
                     "decision": overall, "variants": per}
    return out


def headline(regimes: Dict[str, Any], current: Optional[str]) -> str:
    """One line for the evening report."""
    if not current:
        return "control arm v2 (point-in-time): nothing graded yet (runs after the close, 16:20-20:00 ET)"
    if current not in regimes:
        return (f"control arm v2 (point-in-time; {current.upper()}): nothing graded under "
                f"{V1.RESOLVER_VERSION} yet; older resolver cohorts are shown separately, labelled "
                "legacy, and decide nothing")
    g = regimes[current]
    v = DESIGN["headline_variant"]
    p = g["variants"][v]
    a, u = p["approved"], p["unapproved"]

    def rate(x):
        if not x["filled"]:
            return "no fills"
        lo, hi = x["wilson_95"]
        return f"{x['win_rate']:.0%} [{lo:.0%}-{hi:.0%}] of {x['filled']}"

    return (f"control arm v2 (point-in-time; {current.upper()}, {g['sessions']} sessions, {v}): approved "
            f"{rate(a)} vs unapproved {rate(u)}; break-even 37.5%; {g['decision']} -- {p['why']}")


def v1_exploratory(store) -> Dict[str, Any]:
    """v1, unchanged numbers, labelled for what it is."""
    s = V1.summary(store)
    out = {"label": V1_LABEL, "design_version": s["design_version"], "design_hash": s["design_hash"],
           "resolver_version": s["resolver_version"],
           # older resolver cohorts: counted, labelled, never in the regimes below (NEW-02)
           **({"other_resolvers": {v: {"label": c["label"], "days": c["days"],
                                       "regimes": {f: {"sessions": g["sessions"], "graded_names": g["graded_names"]}
                                                   for f, g in c["regimes"].items()}}
                                   for v, c in s["other_resolvers"].items()}}
              if s.get("other_resolvers") else {})}
    return {**out, "headline": f"{V1_LABEL}: {s['headline']}",
            "regimes": {f: {"sessions": g["sessions"], "decision": g["decision"],
                            "variants": {v: {"approved": p["approved"]["win_rate"],
                                             "approved_filled": p["approved"]["filled"],
                                             "unapproved": p["unapproved"]["win_rate"],
                                             "unapproved_filled": p["unapproved"]["filled"],
                                             "difference_ci_95": p["difference_ci_95"],
                                             "decision": p["decision"]}
                                         for v, p in g["variants"].items()}}
                        for f, g in s["regimes"].items()}}


def view(store, day: Optional[str] = None) -> Dict[str, Any]:
    """The readout: v2 is the primary result; v1 rides along, labelled exploratory."""
    s = summary(store)
    d = day or max((r.get("day") or "" for r in store.scan("edge_control_v2")), default=None)
    rec = store.get("edge_control_v2", d) if d else None
    if rec:
        def arm(r, k):
            x = (r.get("arms") or {}).get(k)
            if not x:
                return None
            return {"decision_et": x.get("decision_et"),
                    "outcomes": {v: (x.get("variants") or {}).get(v, {}).get("outcome") for v in VARIANTS}}

        rows = [{"symbol": r["symbol"], "first_seen_et": r.get("first_seen_et"),
                 "approved_at_et": r.get("approved_at_et"), "feed": r.get("feed"),
                 "approved_by": sorted({a["experiment_id"] for a in r.get("approvals") or []}),
                 "approved": arm(r, APPROVED),
                 "unapproved": ({"label": "UNAPPROVED (control, not a pick)", **arm(r, UNAPPROVED)}
                                if r.get("arms") else r.get("data"))}
                for r in rec.get("rows") or []]
        s["day"] = {"day": rec["day"], "feed": rec.get("feed"), "complete": rec.get("complete"),
                    "truncated": rec.get("truncated"), "names": len(rows),
                    "approved": sum(1 for r in rec.get("rows") or [] if (r.get("arms") or {}).get(APPROVED)),
                    "rows": rows[:60]}
    else:
        s["day"] = None
    s["exploratory_v1"] = v1_exploratory(store)
    return s
