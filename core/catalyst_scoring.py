"""Universal all-stock context scoring for Ghost predictions.

This module turns the SPCE lesson into reusable deterministic signals:

- green premarket tape is not automatically bullish;
- improving EPS/revenue can be offset by guidance/timeline deterioration;
- structured news events should be scored point-in-time and brake-first until
  enough outcomes prove a positive edge.

Scores are directional in [-1, +1]. Positive means bullish context, negative
means bearish context, 0 means neutral/unknown. Missing data stays unknown.
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, Iterable, List, Optional

from core.quiet import note_suppressed


BEARISH_EVENT_WEIGHTS = {
    "going_concern": 1.00,
    "bankruptcy_risk": 1.00,
    "dilution_or_offering": 0.95,
    "fda_rejection": 0.95,
    "guidance_cut": 0.90,
    "timeline_delay": 0.86,
    "delisting_notice": 0.85,
    "short_report": 0.78,
    "earnings_miss": 0.72,
    "reverse_split": 0.70,
    "officer_change": 0.62,
    "analyst_downgrade": 0.55,
}

BULLISH_EVENT_WEIGHTS = {
    "mna_confirmed": 0.95,
    "fda_approval": 0.90,
    "guidance_raise": 0.82,
    "earnings_beat": 0.70,
    "contract_award": 0.70,
    "mna_rumor": 0.55,
    "analyst_upgrade": 0.50,
}

GUIDANCE_EVENTS = {"guidance_cut", "guidance_raise", "timeline_delay"}
CATALYST_EVENTS = set(BEARISH_EVENT_WEIGHTS) | set(BULLISH_EVENT_WEIGHTS)
TIMELINE_EVENTS = {"timeline_delay"}


def _f(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        out = float(v)
        if math.isfinite(out):
            return out
    except Exception:
        return None
    return None


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _event_effect(ev: Dict[str, Any]) -> float:
    et = str(ev.get("event_type") or "").strip().lower()
    if not et:
        return 0.0
    materiality = _f(ev.get("materiality"))
    if materiality is None:
        materiality = 0.6
    reliability = _f(ev.get("source_reliability"))
    if reliability is None:
        reliability = _f(ev.get("confidence"))
    if reliability is None:
        reliability = 0.65
    rumor_mult = 0.70 if str(ev.get("confirmation_status") or "").lower() == "rumor" else 1.0
    if et in BEARISH_EVENT_WEIGHTS:
        return -BEARISH_EVENT_WEIGHTS[et] * materiality * reliability * rumor_mult
    if et in BULLISH_EVENT_WEIGHTS:
        return BULLISH_EVENT_WEIGHTS[et] * materiality * reliability * rumor_mult
    direction = str(ev.get("direction_hint") or "").lower()
    if direction == "bearish":
        return -0.45 * materiality * reliability * rumor_mult
    if direction == "bullish":
        return 0.40 * materiality * reliability * rumor_mult
    return 0.0


def score_events(events: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Score point-in-time structured news events for any stock."""
    rows = [dict(e) for e in (events or []) if isinstance(e, dict)]
    effects: List[float] = []
    top: List[Dict[str, Any]] = []
    guidance_effects: List[float] = []
    catalyst_effects: List[float] = []
    timeline_events: List[Dict[str, Any]] = []
    bearish_material: List[Dict[str, Any]] = []
    bullish_material: List[Dict[str, Any]] = []

    for ev in rows:
        effect = _event_effect(ev)
        if effect == 0.0:
            continue
        effects.append(effect)
        et = str(ev.get("event_type") or "").lower()
        if et in GUIDANCE_EVENTS:
            guidance_effects.append(effect)
        if et in CATALYST_EVENTS:
            catalyst_effects.append(effect)
        if et in TIMELINE_EVENTS:
            timeline_events.append(ev)
        if effect < -0.20:
            bearish_material.append(ev)
        elif effect > 0.20:
            bullish_material.append(ev)
        top.append({
            "event_type": et,
            "direction_hint": ev.get("direction_hint"),
            "effect": round(effect, 3),
            "materiality": _f(ev.get("materiality")),
            "evidence": ev.get("evidence"),
            "asof_ts": ev.get("asof_ts"),
        })

    net = _clamp(sum(effects), -1.0, 1.0) if effects else 0.0
    guidance = _clamp(sum(guidance_effects), -1.0, 1.0) if guidance_effects else 0.0
    catalyst = _clamp(sum(catalyst_effects), -1.0, 1.0) if catalyst_effects else 0.0
    top.sort(key=lambda x: abs(float(x.get("effect") or 0.0)), reverse=True)
    return {
        "available": bool(rows),
        "event_count": len(rows),
        "score": round(net, 3),
        "guidance_momentum_score": round(guidance, 3),
        "catalyst_score": round(catalyst, 3),
        "timeline_delay_detected": bool(timeline_events),
        "bearish_material_events": len(bearish_material),
        "bullish_material_events": len(bullish_material),
        "top_events": top[:5],
    }


def fetch_event_context(symbol: str, *, asof_ts: Optional[int] = None, lookback_s: int = 7 * 86400) -> Dict[str, Any]:
    """Best-effort point-in-time event scoring. Failures return unavailable."""
    try:
        from core.news_events import recent_events_for_symbol
        events = recent_events_for_symbol(symbol, asof_ts=asof_ts, lookback_s=lookback_s)
    except Exception as exc:
        return {"available": False, "event_count": 0, "score": 0.0, "reason": str(exc)[:120]}
    scored = score_events(events)
    scored["events"] = events[:10]
    return scored


def score_headline_fallback(articles: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Fallback when stored structured events are unavailable.

    This intentionally stays weaker than stored events because raw headlines are
    less reliable and not deduped/point-in-time unless the caller provides that.
    """
    try:
        from core.news_events import classify_text
    except Exception:
        return {"available": False, "event_count": 0, "score": 0.0}
    events: List[Dict[str, Any]] = []
    for art in list(articles or [])[:20]:
        if not isinstance(art, dict):
            continue
        title = str(art.get("title") or art.get("headline") or "")
        summary = str(art.get("summary") or art.get("description") or "")
        for ev in classify_text(title, summary):
            ev = dict(ev)
            ev.setdefault("source_reliability", 0.55)
            ev.setdefault("asof_ts", art.get("published_at") or art.get("ts"))
            events.append(ev)
    scored = score_events(events)
    scored["source"] = "headline_classifier_fallback"
    scored["score"] = round(float(scored.get("score") or 0.0) * 0.70, 3)
    scored["guidance_momentum_score"] = round(float(scored.get("guidance_momentum_score") or 0.0) * 0.70, 3)
    scored["catalyst_score"] = round(float(scored.get("catalyst_score") or 0.0) * 0.70, 3)
    return scored


def score_premarket_quality(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Score extended-hours quality without treating green tape as proof.

    With only price/gap data, the score is conservative. It becomes bullish only
    for moderate, confirmed gaps. Extreme gaps are chase risk; red gaps are
    bearish, and unavailable volume/spread caps confidence.
    """
    if not isinstance(ctx, dict) or not ctx:
        return {"available": False, "score": 0.0, "reason": "extended-session context unavailable"}
    gap_pct = _f(ctx.get("gap_pct"))
    session = str(ctx.get("session") or "").lower()
    live = _f(ctx.get("live_price"))
    session_price = _f(ctx.get("session_price"))
    prev = _f(ctx.get("previous_close"))
    if gap_pct is None or prev is None or prev <= 0:
        return {"available": False, "score": 0.0, "reason": "prior close or gap unavailable", "session": session}

    score = 0.0
    flags: List[str] = []
    # Positive gaps need quality: modest > huge, and holding above prev close.
    if gap_pct >= 10.0:
        score += 0.05
        flags.append("large_gap_chase_risk")
    elif gap_pct >= 3.0:
        score += 0.28
        flags.append("constructive_gap")
    elif gap_pct >= 0.75:
        score += 0.16
        flags.append("small_positive_gap")
    elif gap_pct <= -6.0:
        score -= 0.45
        flags.append("large_negative_gap")
    elif gap_pct <= -1.5:
        score -= 0.25
        flags.append("negative_gap")

    if live is not None and session_price is not None and session_price > 0:
        hold_pct = (live - session_price) / session_price * 100.0
        if gap_pct > 0 and live < prev:
            score -= 0.35
            flags.append("failed_gap_back_below_prev_close")
        elif gap_pct > 0 and hold_pct < -1.0:
            score -= 0.15
            flags.append("fading_from_premarket_price")
        elif gap_pct > 0 and hold_pct >= -0.3:
            score += 0.08
            flags.append("holding_premarket_price")

    # No universal premarket volume/spread source here, so confidence is capped.
    score = _clamp(score, -1.0, 1.0)
    return {
        "available": True,
        "score": round(score, 3),
        "gap_pct": round(gap_pct, 3),
        "session": session,
        "flags": flags,
        "confidence": 0.45,
        "note": "Price-only premarket context; volume/spread unavailable, so this is brake-first.",
    }


def universal_context_brake(direction: str, event_ctx: Dict[str, Any], premarket_ctx: Optional[Dict[str, Any]] = None) -> float:
    """Return a confidence brake in [-0.12, 0.0] for context against a trade."""
    dir_mult = 1.0 if str(direction).upper() in ("UP", "BUY") else -1.0
    event_score = _f((event_ctx or {}).get("score")) or 0.0
    guidance_score = _f((event_ctx or {}).get("guidance_momentum_score")) or 0.0
    pm_score = _f((premarket_ctx or {}).get("score")) or 0.0
    against = -(event_score * dir_mult * 0.08 + guidance_score * dir_mult * 0.05 + pm_score * dir_mult * 0.03)
    if bool((event_ctx or {}).get("timeline_delay_detected")) and dir_mult > 0:
        against += 0.04
    brake = -_clamp(against, 0.0, 0.12)
    return round(brake, 3)


__all__ = [
    "score_events",
    "fetch_event_context",
    "score_headline_fallback",
    "score_premarket_quality",
    "universal_context_brake",
]
