"""core/catalyst_freshness.py — catalyst age/freshness + earnings surprise.

Encodes the SPCE lesson directly: a catalyst that is already stale, fully
priced in, or too far in the future must NOT drive a 1-2 week opportunity
score the way a fresh, developing catalyst does.

Pure, deterministic, no I/O. All inputs are passed in as dicts/values so the
logic is fully unit-testable and honest about what it can and cannot claim.

Catalyst freshness buckets (operator spec):
  FRESH           0-6 hours
  DEVELOPING      6-24 hours
  RECENT          1-3 days
  AGING           3-7 days
  STALE           7-14 days
  OLD             14-30 days
  ANCIENT         30+ days
  FUTURE          scheduled ahead (not yet occurred)

A catalyst's weight decays with age; a FUTURE catalyst is weighted by how soon
it lands (a 6-month-away catalyst contributes ~nothing to a 1-14 day score).
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional

# Age buckets in hours (upper bound inclusive).
AGE_BUCKETS = (
    (6, "fresh", "FRESH CATALYST"),
    (24, "developing", "DEVELOPING CATALYST"),
    (72, "recent", "RECENT CATALYST"),
    (168, "aging", "AGING CATALYST"),
    (336, "stale", "STALE CATALYST"),
    (720, "old", "OLD CATALYST"),
    (math.inf, "ancient", "ANCIENT CATALYST"),
)

# Freshness weight: how much a catalyst of a given age should count toward a
# 1-14 day opportunity. Fresh = 1.0, decays to ~0 by 30 days.
def freshness_weight(age_hours: float) -> float:
    """0..1 weight for a catalyst of a given age (hours since occurrence).

    Fresh (<=6h) = 1.0; decays smoothly to ~0.05 by 30 days. This is the
    single most important knob for the SPCE lesson: old news must not be
    weighted like fresh news.
    """
    if age_hours < 0:
        return 0.0
    if age_hours <= 6:
        return 1.0
    if age_hours <= 24:
        return 0.85
    if age_hours <= 72:
        return 0.65
    if age_hours <= 168:
        return 0.45
    if age_hours <= 336:
        return 0.25
    if age_hours <= 720:
        return 0.10
    return 0.05


def classify_age(age_hours: float) -> Dict[str, Any]:
    """Classify a catalyst's age into a bucket + label."""
    for upper, key, label in AGE_BUCKETS:
        if age_hours <= upper:
            return {"bucket": key, "label": label, "age_hours": round(age_hours, 1)}
    return {"bucket": "ancient", "label": "ANCIENT CATALYST", "age_hours": round(age_hours, 1)}


def classify_catalyst(
    *,
    asof_ts: Optional[float],
    now_ts: Optional[float] = None,
    scheduled_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """Classify a catalyst as FRESH / DEVELOPING / PRICED / STALE / FUTURE.

    - If scheduled_ts is in the future, it's a FUTURE catalyst (weighted by
      how soon it lands, not by age).
    - Otherwise age = now - asof_ts, and the freshness weight decays with age.
    """
    now = float(now_ts if now_ts is not None else time.time())

    # Future catalyst: scheduled ahead.
    if scheduled_ts is not None and float(scheduled_ts) > now:
        days_out = (float(scheduled_ts) - now) / 86400.0
        # A catalyst within 14 days matters; beyond ~90 days it's ~irrelevant
        # to a 1-2 week trade.
        if days_out <= 1:
            weight = 0.9
            state = "future_imminent"
        elif days_out <= 3:
            weight = 0.7
            state = "future_near"
        elif days_out <= 14:
            weight = 0.4
            state = "future_medium"
        elif days_out <= 30:
            weight = 0.15
            state = "future_far"
        else:
            weight = 0.03
            state = "future_distant"
        return {
            "state": state,
            "label": "FUTURE CATALYST",
            "days_out": round(days_out, 1),
            "weight": weight,
            "age_hours": None,
        }

    # Past catalyst: age-based.
    if asof_ts is None:
        return {"state": "unknown", "label": "UNKNOWN", "weight": 0.0, "age_hours": None}
    age_hours = max(0.0, (now - float(asof_ts)) / 3600.0)
    bucket = classify_age(age_hours)
    weight = freshness_weight(age_hours)
    # Map age bucket to the operator's freshness states.
    state = {
        "fresh": "fresh",
        "developing": "developing",
        "recent": "recent",
        "aging": "aging",
        "stale": "stale",
        "old": "old",
        "ancient": "ancient",
    }.get(bucket["bucket"], "stale")
    return {
        "state": state,
        "label": bucket["label"],
        "age_hours": round(age_hours, 1),
        "weight": weight,
        "days_out": None,
    }


def catalyst_timing_score(
    events: Optional[List[Dict[str, Any]]],
    *,
    now_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """0-100 score for "is there a catalyst that matters within 1-14 days?"

    Fresh, high-materiality catalysts score high; stale or far-future
    catalysts score low. This is the direct antidote to the SPCE mistake of
    letting a months-away catalyst dominate a short-term read.
    """
    now = float(now_ts if now_ts is not None else time.time())
    rows = [dict(e) for e in (events or []) if isinstance(e, dict)]
    if not rows:
        return {"score": 0.0, "available": False, "best": None}

    best_score = 0.0
    best = None
    for ev in rows:
        asof = ev.get("asof_ts") or ev.get("published_at") or ev.get("ts")
        scheduled = ev.get("scheduled_ts")
        cls = classify_catalyst(asof_ts=asof, now_ts=now, scheduled_ts=scheduled)
        materiality = _f(ev.get("materiality"), 0.6)
        reliability = _f(ev.get("source_reliability"), _f(ev.get("confidence"), 0.65))
        # Direction magnitude: how strong the catalyst is (bullish or bearish).
        effect = abs(_f(ev.get("effect"), 0.5))
        # Score = freshness weight * materiality * reliability * effect, scaled 0-100.
        score = cls["weight"] * materiality * reliability * effect * 100.0
        if score > best_score:
            best_score = score
            best = {
                "event_type": ev.get("event_type"),
                "state": cls["state"],
                "label": cls["label"],
                "age_hours": cls["age_hours"],
                "days_out": cls["days_out"],
                "weight": cls["weight"],
                "score": round(score, 1),
            }
    return {
        "score": round(min(100.0, best_score), 1),
        "available": True,
        "best": best,
    }


# ── Earnings surprise ──────────────────────────────────────────────────────
def earnings_surprise_pct(actual: Optional[float], expected: Optional[float]) -> Optional[float]:
    """Percent surprise: (actual - expected) / |expected| * 100."""
    a = _f(actual)
    e = _f(expected)
    if a is None or e is None or e == 0:
        return None
    return round((a - e) / abs(e) * 100.0, 2)


def score_earnings_surprise(
    *,
    eps_actual: Optional[float] = None,
    eps_expected: Optional[float] = None,
    revenue_actual: Optional[float] = None,
    revenue_expected: Optional[float] = None,
    guidance_change: Optional[float] = None,  # -1..1 (cut..raise)
) -> Dict[str, Any]:
    """0-100 earnings-surprise score from actual vs expected.

    A company can report a LOSS and still score high if the loss was smaller
    than expected (the surprise is relative, not absolute). Guidance change
    adds a directional bonus/penalty.
    """
    eps_surprise = earnings_surprise_pct(eps_actual, eps_expected)
    rev_surprise = earnings_surprise_pct(revenue_actual, revenue_expected)

    pts = 0.0
    if eps_surprise is not None:
        # Map surprise % to points: +10% surprise ≈ +30 pts, capped.
        pts += _clamp(50.0 + eps_surprise * 3.0, 0.0, 60.0)
    if rev_surprise is not None:
        pts += _clamp(50.0 + rev_surprise * 2.0, 0.0, 40.0)
    # Guidance: raise adds, cut subtracts.
    if guidance_change is not None:
        pts += _clamp(guidance_change * 20.0, -20.0, 20.0)

    return {
        "score": round(_clamp(pts, 0.0, 100.0), 1),
        "eps_surprise_pct": eps_surprise,
        "revenue_surprise_pct": rev_surprise,
        "guidance_change": guidance_change,
        "available": eps_surprise is not None or rev_surprise is not None,
    }


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        out = float(v)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
