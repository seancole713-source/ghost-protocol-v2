"""The AI scorecard: does the research actually add edge?

Two forecasts a day per experiment is far too few to judge a filter. But every
morning card holds up to 50 candidates, each already stamped (before the open)
with what every judge said about it -- the keyword tagger (verdict), the
no-catalyst baseline (baseline_verdict), Claude-plus-reviewer research
(verified_verdict) and the model (model_prob). So after the close, every PRICED
card row is graded under the same frozen Gap-and-Go levels with the same
resolver: what WOULD have happened had it been traded. That is a counterfactual
and is labelled as one -- it never enters the forward ledger or any
experiment's record.

The comparisons are graded as the ORDER would have traded (resolve_execution: the
stop-limit entry, the conservative fill bar and 10 bps a side), and read against the
break-even AFTER those costs -- not the frictionless forecast outcome against the
no-cost 37.5% (audit 2026-09-25 U12). The forecast outcome is kept on every row
("outcome") for reference. Rows graded before the execution grade existed carry no
"execution" and are counted, never pooled.

Then the question is asked directly, on the same stocks: among the gap-qualified
names, did the ones research APPROVED do better than the ones it REJECTED?
Every rate carries its Wilson interval and a sample-size verdict; nothing reads
"works" until both sides have enough decided trades.

Research quality is scored beside it: claims made, how many the reviewer
quarantined and why, which reviewer family did it, and what it cost.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any, Dict, List, Optional

from edge import resolver as RV
from edge.contracts import ContractError, issue
from edge.research_worker import not_researched_reason
from edge.stats import wilson

DECIDED = (RV.WIN, RV.LOSS, RV.TIME_EXIT)
MIN_PER_SIDE = 30          # below this a comparison is reported, never judged
COST_BPS = 10.0            # per side, the simulated-execution record's stated placeholder
BASIS = (f"simulated execution: the stop-limit order as written, conservative fill bar, "
         f"{COST_BPS:g} bps a side (edge/resolver.py resolve_execution)")
COUNTERFACTUAL = ("counterfactual: every priced card candidate graded under the frozen Gap-and-Go levels "
                  "as if traded; never part of any experiment's record")


def grade_card(get, store, *, day: date, now: int) -> Dict[str, Any]:
    """Grade every priced row of the day's card once, after the close."""
    from edge import pipeline as P
    ds = day.isoformat()
    if now < P._at(day, 16, 20):
        return {"status": "too_early"}
    if store.get("edge_card_outcomes", ds):
        return {"status": "already_graded"}
    card = store.get("edge_cards", ds)
    if not card:
        return {"status": "no_card"}
    rows = [r for r in card.get("rows") or [] if r.get("ref_price")]
    bars = P.A.bars_multi(get, sorted({r["symbol"] for r in rows}), timeframe="1Min",
                          start=P._iso(P._at(day, 9, 30)), end=P._iso(P._at(day, 16, 0))) if rows else {}
    out = []
    for r in rows:
        try:
            f = issue(P.SPEC, symbol=r["symbol"], session_date=day, entry_ref=r["ref_price"],
                      issued_at=card["issued_at"])
        except ContractError as exc:
            out.append({"symbol": r["symbol"], "outcome": None, "note": str(exc)})
            continue
        mb = P._minute_bars(bars.get(r["symbol"]) or [])
        m = RV.resolve_market(f, mb)
        x = RV.resolve_execution(f, mb, cost_bps_per_side=COST_BPS)
        out.append({"symbol": r["symbol"], "outcome": m.outcome, "note": m.note,
                    "execution": x.outcome, "execution_note": x.note, "execution_pnl_usd": x.pnl_usd,
                    "auto": r.get("verdict"), "baseline": r.get("baseline_verdict"),
                    "research": r.get("verified_verdict"), "research_status": r.get("research_status"),
                    "model_prob": r.get("model_prob"),
                    "catalyst": r.get("catalyst")})
    store.put("edge_card_outcomes", ds, {"day": ds, "graded_at": now, "rows": out, "label": COUNTERFACTUAL,
                                         "basis": BASIS})
    return {"status": "graded", "rows": len(out),
            "outcomes": dict(Counter(o["outcome"] for o in out if o["outcome"])),
            "execution": dict(Counter(o["execution"] for o in out if o.get("execution")))}


def _rate(rows: List[Dict[str, Any]], key: str = "execution") -> Dict[str, Any]:
    decided = [r for r in rows if r.get(key) in DECIDED]
    wins = sum(1 for r in decided if r[key] == RV.WIN)
    n = len(decided)
    lo, hi = wilson(wins, n) if n else (None, None)
    pnl = [r["execution_pnl_usd"] for r in decided
           if key == "execution" and isinstance(r.get("execution_pnl_usd"), (int, float))]
    return {"candidates": len(rows), "decided": n, "wins": wins,
            "win_rate": round(wins / n, 4) if n else None,
            "win_rate_ci": [round(lo, 4), round(hi, 4)] if n else None,
            "no_fill": sum(1 for r in rows if r.get(key) == RV.NO_FILL),
            **({"avg_pnl_usd": round(sum(pnl) / len(pnl), 2)} if pnl else {})}


def _compare(name: str, yes: List[Dict[str, Any]], no: List[Dict[str, Any]]) -> Dict[str, Any]:
    a, b = _rate(yes), _rate(no)
    if min(a["decided"], b["decided"]) < MIN_PER_SIDE:
        verdict = (f"not enough data: needs >= {MIN_PER_SIDE} decided trades on each side "
                   f"(have {a['decided']} approved, {b['decided']} rejected)")
    elif a["win_rate_ci"][0] > b["win_rate_ci"][1]:
        verdict = f"{name} ADDS edge: approved names beat rejected ones with non-overlapping intervals"
    elif a["win_rate_ci"][1] < b["win_rate_ci"][0]:
        verdict = f"{name} HURTS: approved names did worse than the ones it rejected"
    else:
        verdict = f"no demonstrated difference yet: {name}'s intervals overlap"
    return {"approved": a, "rejected": b, "verdict": verdict}


def _not_researched(store, day: str, row: Dict[str, Any]) -> bool:
    """A row the research never actually looked at (tool failure, unusable output, no record).
    Its verified verdict is not research's judgement, so it is neither approved nor rejected."""
    if row.get("research_status") == "not_researched":
        return True
    return bool(not_researched_reason(store.get("edge_research", f"{day}|{str(row.get('symbol') or '').upper()}")))


def research_quality(store) -> Dict[str, Any]:
    recs = store.scan("edge_research")
    by_rev: Dict[str, Dict[str, Any]] = {}
    problems: Counter = Counter()
    for rec in recs:
        s = by_rev.setdefault(rec.get("reviewer") or "unknown",
                              {"symbols": 0, "claims": 0, "quarantined": 0, "cost_usd": 0.0,
                               "dilution_flags": 0, "wrong_entity": 0, "not_researched": 0})
        s["symbols"] += 1
        s["not_researched"] += 1 if not_researched_reason(rec) else 0
        s["cost_usd"] = round(s["cost_usd"] + (rec.get("cost_usd") or 0.0), 4)
        s["dilution_flags"] += 1 if (rec.get("review") or {}).get("dilution_found") else 0
        s["wrong_entity"] += 1 if (rec.get("review") or {}).get("entity_ok") is False else 0
        for c in rec.get("claims") or []:
            s["claims"] += 1
            if c.get("status") == "QUARANTINED":
                s["quarantined"] += 1
                problems.update(str(p)[:80] for p in c.get("problems") or [])
    for s in by_rev.values():
        s["quarantine_rate"] = round(s["quarantined"] / s["claims"], 4) if s["claims"] else None
    return {"days": len({r.get("day") for r in recs}), "by_reviewer": by_rev,
            "top_quarantine_reasons": problems.most_common(8)}


def break_even_after_costs() -> float:
    """Gap-and-Go v1's break-even win rate once each side pays COST_BPS (40.0% at 10 bps)."""
    from edge import control as CA
    from edge.pipeline import SPEC
    return round(CA.break_even_after_costs(COST_BPS, spec=SPEC), 4)


def scorecard(store) -> Dict[str, Any]:
    days = sorted(store.scan("edge_card_outcomes"), key=lambda d: d["day"])
    # Graded as the order would have traded (U12); legacy rows with only a forecast outcome are
    # counted and left out -- two bases are never pooled.
    legacy = sum(1 for d in days for r in d.get("rows") or [] if r.get("outcome") and not r.get("execution"))
    rows = [r for d in days for r in d.get("rows") or [] if r.get("execution")]
    gaps = [r for r in rows if r.get("baseline") == "ELIGIBLE"]    # gap-qualified, liquid, priced
    # AI research is judged only on rows it actually looked at: a failed search is neither side.
    gap_days = [(d["day"], r) for d in days for r in d.get("rows") or []
                if r.get("execution") and r.get("baseline") == "ELIGIBLE"]
    researched = [r for dd, r in gap_days if not _not_researched(store, dd, r)]
    out: Dict[str, Any] = {
        "label": COUNTERFACTUAL, "sessions": len(days), "basis": BASIS,
        "rows_without_execution_grade": legacy,
        "window": [days[0]["day"], days[-1]["day"]] if days else None,
        "base_rate_all_gappers": _rate(gaps),
        "keyword_catalyst": _compare("the keyword catalyst filter",
                                     [r for r in gaps if r.get("auto") == "ELIGIBLE"],
                                     [r for r in gaps if r.get("auto") == "REJECTED"]),
        "ai_research": {**_compare("AI research",
                                   [r for r in researched if r.get("research") == "ELIGIBLE"],
                                   [r for r in researched if r.get("research") == "REJECTED"]),
                        "not_researched": len(gap_days) - len(researched)},
        "research_quality": research_quality(store),
        "break_even": break_even_after_costs(),
        "break_even_before_costs": 0.375,
    }
    top10 = store.scan("edge_top10")
    top10_days = {t["day"] for t in top10 if t.get("day")}
    picks = {(t["day"], x["symbol"]) for t in top10 for x in t.get("list") or []}
    if picks:
        # Only sessions that HAD a Top 10: a day before the list existed is not "not picked".
        tagged = [(d["day"], r) for d in days if d["day"] in top10_days
                  for r in d.get("rows") or [] if r.get("execution")]
        out["top10"] = {**_compare("the Top 10 quality ranking",
                                   [r for dd, r in tagged if (dd, r["symbol"]) in picks],
                                   [r for dd, r in tagged if (dd, r["symbol"]) not in picks]),
                        "sessions": len({dd for dd, _ in tagged})}
    scored = [r for r in gaps if r.get("model_prob") is not None]
    if scored:
        out["model"] = _compare("the model (prob >= 0.40)",
                                [r for r in scored if r["model_prob"] >= 0.40],
                                [r for r in scored if r["model_prob"] < 0.40])
    if not days:
        out["note"] = "no graded cards yet: the first is graded after 16:20 ET on the first card day"
    return out


def latest_outcomes(store, day: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if day:
        return store.get("edge_card_outcomes", day)
    rows = store.scan("edge_card_outcomes")
    return max(rows, key=lambda r: r["day"]) if rows else None
