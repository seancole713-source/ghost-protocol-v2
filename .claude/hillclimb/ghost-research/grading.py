"""Programmatic grading for the research eval. No model judge: every check is code.

Inputs are what the real worker produced for one case: its stored record, the
author's raw JSON (for citation dates, which the stored record drops), and
edge.research_worker.verdict() -- the exact function the card reads.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DAY_S = 24 * 3600


def cutoff_epoch(cutoff_et: str) -> int:
    return int(datetime.strptime(cutoff_et, "%Y-%m-%d %H:%M").replace(tzinfo=ET).timestamp())


def _iso_to_epoch(s: Any) -> Optional[int]:
    """A full timestamp -> epoch. A naive one is read as US/Eastern, not the grading machine's zone."""
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return int((d if d.tzinfo else d.replace(tzinfo=ET)).timestamp())


def _date_only(s: Any) -> Optional[date]:
    """'2026-10-01' carries a day but no time: it must be judged as a whole day, never as midnight."""
    try:
        return date.fromisoformat(str(s)) if len(str(s)) == 10 else None
    except ValueError:
        return None


def in_window(published_at: Any, cut: int) -> Optional[bool]:
    """Inside [cutoff-24h, cutoff]? None when undated. A date-only stamp passes when that ET day overlaps
    the window (it could have been published inside it); its day being wholly outside fails."""
    d = _date_only(published_at)
    if d is not None:
        lo = datetime.fromtimestamp(cut - DAY_S, tz=ET).date()
        hi = datetime.fromtimestamp(cut, tz=ET).date()
        return lo <= d <= hi
    ts = _iso_to_epoch(published_at)
    return None if ts is None else (cut - DAY_S <= ts <= cut)


def author_citations(author_raw: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for c in (author_raw or {}).get("claims") or []:
        if not isinstance(c, dict):
            continue
        for x in c.get("citations") or []:
            if isinstance(x, dict):
                out.append({"url": str(x.get("url") or ""), "published_at": x.get("published_at")})
    return out


def usable_claims(rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    from edge import research as RS
    return [c for c in rec.get("claims") or [] if not RS.is_quarantined(c.get("status"))]


def grade_case(case: Dict[str, Any], *, rec: Dict[str, Any], verdict: Dict[str, Any],
               author_raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """-> {"grade": {metric: 0/1}, "meta": {...}}. `catalyst_correct` counts an UNKNOWN verdict (research
    finished but nothing could be concluded) as wrong, and records it as outcome "unknown", so it is
    never mistaken for a correct "no catalyst". Runs that could not look at all never reach here."""
    cut = cutoff_epoch(case["cutoff_et"])
    want = case["expected"]["catalyst"]
    got = verdict.get("catalyst")
    if got is None:
        outcome = "unknown"
    elif want is True:
        outcome = "tp" if got else "fn"
    else:
        outcome = "fp" if got else "tn"
    cites = author_citations(author_raw)
    judged = [(c, in_window(c["published_at"], cut)) for c in cites]
    dated = [c for c, ok in judged if ok is not None]
    bad = [c["url"] for c, ok in judged if ok is False]
    use = usable_claims(rec)
    haystack = " ".join(c.get("statement", "") + " " + " ".join(c.get("urls") or []) for c in use).lower()
    hit = [t for t in case.get("trap_terms") or [] if t.lower() in haystack]
    grade = {"catalyst_correct": int(outcome in ("tp", "tn")), "pit_ok": int(not bad), "trap_ok": int(not hit)}
    want_d, got_d = case["expected"].get("dilution"), verdict.get("dilutive")
    d_outcome = None
    if want_d is not None:
        # Unknown (None) is its own outcome and counts as wrong: "could not check" is never "no dilution".
        d_outcome = "unknown" if got_d is None else ("tp" if got_d else "fn") if want_d else ("fp" if got_d else "tn")
        grade["dilution_correct"] = int(d_outcome in ("tp", "tn"))
    return {
        "grade": grade,
        "meta": {"outcome": outcome, "verdict": {k: verdict.get(k) for k in ("catalyst", "dilutive", "headline", "not_researched")},
                 "out_of_window_cites": bad, "citations": cites, "undated_cites": len(cites) - len(dated),
                 "trap_hits": hit, "n_claims": len(rec.get("claims") or []), "n_usable": len(use),
                 "dilution_outcome": d_outcome, "dilution_reported": got_d},
    }


def wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - h) / d, (c + h) / d)


def summarize(rows: List[Dict[str, Any]], errors: int = 0) -> Dict[str, Any]:
    """Headline from raw rows: confusion matrix on catalyst, majority-class baseline, intervals."""
    ok = [r for r in rows if r.get("status") == "ok"]
    o = [r["meta"]["outcome"] for r in ok]
    tp, fn, tn, fp, unk = (o.count(x) for x in ("tp", "fn", "tn", "fp", "unknown"))
    pos, neg = tp + fn, tn + fp
    rate = lambda a, b: (a / b if b else None)  # noqa: E731
    n = len(ok)
    acc = sum(r["grade"]["catalyst_correct"] for r in ok)
    exp_pos = sum(1 for r in ok if r["meta"].get("expected_catalyst") is True)
    return {
        "n_rows": n, "errors": errors, "tp": tp, "fn": fn, "tn": tn, "fp": fp, "unknown": unk,
        "precision": rate(tp, tp + fp), "recall": rate(tp, pos + sum(
            1 for r in ok if r["meta"]["outcome"] == "unknown" and r["meta"].get("expected_catalyst") is True)),
        "specificity": rate(tn, neg + sum(
            1 for r in ok if r["meta"]["outcome"] == "unknown" and r["meta"].get("expected_catalyst") is False)),
        "catalyst_accuracy": rate(acc, n), "accuracy_ci95": wilson(acc, n) if n else None,
        "majority_baseline": (max(exp_pos, n - exp_pos) / n) if n else None,
        "dilution": {k: sum(1 for r in ok if r["meta"].get("dilution_outcome") == k)
                     for k in ("tp", "fn", "tn", "fp", "unknown")},
        "pit_ok": rate(sum(r["grade"]["pit_ok"] for r in ok), n),
        "trap_ok": rate(sum(r["grade"]["trap_ok"] for r in ok), n),
    }
