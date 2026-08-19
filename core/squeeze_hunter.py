"""core/squeeze_hunter.py — GHOST SQUEEZE HUNTER.

Find the next Hertz before Hertz happens.

Pure, deterministic scoring for short-squeeze and 1-14 day "explosion"
candidates. No network I/O here — data is passed in as dicts so the logic is
fully unit-testable and honest about what it can and cannot claim.

Three distinct concepts (per operator spec):
  - Squeeze FUEL: short interest, days-to-cover, float, borrow pressure,
    institutional ownership, short-interest change. (Can a squeeze happen?)
  - Squeeze TRIGGER: catalyst, earnings surprise, premarket gap, RVOL,
    call-volume, breakout. (Is something forcing shorts to reconsider?)
  - Squeeze CONFIRMATION: breakout + abnormal volume. (Is it happening now?)

A 7-stage lifecycle classifies where a name sits:
  Setup -> Ignition -> Confirmation -> Squeeze -> Expansion -> Exhaustion -> Reversal

Honesty contract:
  - High short interest ALONE can never produce a high score. The pressure
    score is hard-capped so no single factor dominates, and a low trigger +
    low confirmation caps the composite at "Watch" regardless of fuel.
  - The explosion projection is heuristic (not ML) and always carries the
    "not guaranteed to double" disclaimer.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from typing import Any, Dict, Optional

LOGGER = logging.getLogger("ghost.squeeze_hunter")

# ── Pressure bands (operator spec §2) ──────────────────────────────────────
PRESSURE_BANDS = (
    (30, "low", "Low squeeze potential"),
    (50, "watch", "Watch"),
    (70, "elevated", "Elevated"),
    (85, "high", "High"),
    (101, "extreme", "Extreme"),
)

# ── 7-stage lifecycle (operator spec §8) ───────────────────────────────────
STAGES = (
    "setup",
    "ignition",
    "confirmation",
    "squeeze",
    "expansion",
    "exhaustion",
    "reversal",
)

STAGE_LABELS = {
    "setup": "Stage 1 — Setup",
    "ignition": "Stage 2 — Ignition",
    "confirmation": "Stage 3 — Confirmation",
    "squeeze": "Stage 4 — Squeeze",
    "expansion": "Stage 5 — Expansion",
    "exhaustion": "Stage 6 — Exhaustion",
    "reversal": "Stage 7 — Reversal",
}

STAGE_DESCRIPTIONS = {
    "setup": "Short interest + catalyst approaching; no move yet.",
    "ignition": "Price begins moving + volume increases.",
    "confirmation": "Breakout + abnormal volume.",
    "squeeze": "Short covering + momentum acceleration.",
    "expansion": "Retail/institutional momentum joins.",
    "exhaustion": "Parabolic price + declining momentum + huge volume.",
    "reversal": "Profit taking / short re-entry / liquidity collapse.",
}

# ── Explosion factor weights (operator spec §6) ────────────────────────────
EXPLOSION_FACTORS = {
    "short_squeeze_potential": 0.20,
    "catalyst": 0.18,
    "earnings_surprise": 0.15,
    "relative_volume": 0.15,
    "technical_breakout": 0.12,
    "options_activity": 0.08,
    "float_structure": 0.07,
    "market_environment": 0.05,
}


def _f(v: Any) -> float:
    try:
        out = float(v)
        return out if out == out and out not in (float("inf"), float("-inf")) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


# ── FUEL ───────────────────────────────────────────────────────────────────
def score_fuel(short_ctx: Optional[Dict[str, Any]]) -> float:
    """Squeeze fuel (0-100): can a squeeze happen?

    Short interest is capped at 40 pts so high SI ALONE can never dominate.
    The remaining points require days-to-cover, float structure, institutional
    ownership, short-interest change, and (optional) borrow/utilization.
    """
    ctx = short_ctx or {}
    sf = _f(ctx.get("short_float_pct"))          # % of float
    dtc = _f(ctx.get("days_to_cover"))
    float_shares = _f(ctx.get("float_shares"))
    inst = _f(ctx.get("institutional_ownership_pct"))
    si_change = _f(ctx.get("short_interest_change_pct"))  # MoM % change
    borrow = _f(ctx.get("borrow_fee_pct"))       # optional (paid source)
    util = _f(ctx.get("utilization_pct"))        # optional (paid source)

    # Short interest: 1 pt per % of float, capped at 40.
    si_pts = min(40.0, sf)
    # Days to cover: capped at 25.
    dtc_pts = min(25.0, dtc * 4.0)
    # Float structure: smaller float = more squeeze-prone (cap 15).
    float_pts = 0.0
    if float_shares > 0:
        if float_shares < 10_000_000:
            float_pts = 15.0
        elif float_shares < 50_000_000:
            float_pts = 10.0
        elif float_shares < 200_000_000:
            float_pts = 5.0
    # Institutional ownership: lower = more retail-driven (cap 10).
    inst_pts = 0.0
    if inst > 0:
        if inst < 20:
            inst_pts = 10.0
        elif inst < 40:
            inst_pts = 7.0
        elif inst < 60:
            inst_pts = 4.0
    # Short-interest change: rising SI = building pressure (cap 10).
    si_change_pts = min(10.0, max(0.0, si_change * 0.2))
    # Borrow fee + utilization (optional, only if data present) (cap 20).
    borrow_pts = 0.0
    if borrow > 0:
        borrow_pts += min(10.0, borrow * 0.2)
    if util > 0:
        borrow_pts += min(10.0, util * 0.1)

    total = si_pts + dtc_pts + float_pts + inst_pts + si_change_pts + borrow_pts
    return round(min(100.0, total), 1)


# ── TRIGGER ────────────────────────────────────────────────────────────────
def score_trigger(trigger_ctx: Optional[Dict[str, Any]]) -> float:
    """Squeeze trigger (0-100): is something forcing shorts to reconsider?

    Catalyst, earnings surprise, premarket gap, RVOL, call-volume, breakout.
    """
    ctx = trigger_ctx or {}
    catalyst = _f(ctx.get("catalyst_score"))       # 0-100
    earnings = _f(ctx.get("earnings_surprise"))    # 0-100
    premarket_gap = _f(ctx.get("premarket_gap_pct"))
    rvol = _f(ctx.get("rvol"))
    call_volume = _f(ctx.get("call_volume_score"))  # 0-100
    breakout = _f(ctx.get("breakout_pct"))
    # Catalyst freshness: a stale/far-future catalyst must not count like a
    # fresh one. When a timing score is present, it scales the catalyst term.
    timing = _f(ctx.get("catalyst_timing_score"))

    pts = 0.0
    # Catalyst term: freshness-weighted when timing is available.
    if timing > 0:
        pts += min(30.0, timing * 0.3)
    else:
        pts += min(30.0, catalyst * 0.3)
    pts += min(25.0, earnings * 0.25)
    pts += min(15.0, max(0.0, premarket_gap) * 1.5)
    pts += min(15.0, max(0.0, rvol - 1.0) * 3.0)
    pts += min(10.0, call_volume * 0.1)
    pts += min(5.0, max(0.0, breakout) * 0.5)
    return round(min(100.0, pts), 1)


# ── CONFIRMATION ──────────────────────────────────────────────────────────
def score_confirmation(confirm_ctx: Optional[Dict[str, Any]]) -> float:
    """Squeeze confirmation (0-100): is it happening now?

    Breakout above resistance + abnormal volume + price vs VWAP.
    """
    ctx = confirm_ctx or {}
    breakout = _f(ctx.get("breakout_pct"))
    rvol = _f(ctx.get("rvol"))
    above_vwap = ctx.get("above_vwap")

    pts = 0.0
    pts += min(50.0, max(0.0, breakout) * 5.0)
    pts += min(35.0, max(0.0, rvol - 1.0) * 7.0)
    if above_vwap is True:
        pts += 15.0
    return round(min(100.0, pts), 1)


# ── PRESSURE SCORE ────────────────────────────────────────────────────────
def squeeze_pressure_score(fuel: float, trigger: float, confirmation: float) -> float:
    """Composite 0-100 with a hard cap enforcing multiple conditions.

    Fuel alone can never exceed "Watch" (50): if both trigger and confirmation
    are weak, the composite is capped at 50 regardless of how high fuel is.
    """
    total = 0.40 * fuel + 0.30 * trigger + 0.30 * confirmation
    if trigger < 30 and confirmation < 30:
        total = min(total, 50.0)
    return round(min(100.0, max(0.0, total)), 1)


def pressure_band(score: float) -> Dict[str, str]:
    """Map a 0-100 pressure score to its band + label."""
    s = _f(score)
    for threshold, key, label in PRESSURE_BANDS:
        if s <= threshold:
            return {"band": key, "label": label}
    return {"band": "extreme", "label": "Extreme"}


# ── 7-STAGE LIFECYCLE ─────────────────────────────────────────────────────
def classify_stage(
    *,
    fuel: float,
    trigger: float,
    confirmation: float,
    move_pct: float,
    rvol: float,
    breakout_pct: float,
    momentum_declining: bool = False,
    price_parabolic: bool = False,
    huge_volume: bool = False,
) -> Dict[str, Any]:
    """Classify which of the 7 squeeze stages a name is currently in.

    Rule-based and deterministic. Order matters: later stages are checked
    first so a name that has already exhausted is not mislabeled as setup.
    """
    move = _f(move_pct)
    rv = _f(rvol)
    brk = _f(breakout_pct)
    fuel_s = _f(fuel)
    trigger_s = _f(trigger)
    confirm_s = _f(confirmation)

    # Reversal: profit taking / short re-entry / liquidity collapse.
    if momentum_declining and move < 0:
        return _stage("reversal")
    # Exhaustion: parabolic + declining momentum + huge volume.
    if price_parabolic and momentum_declining and huge_volume:
        return _stage("exhaustion")
    # Expansion: sustained momentum, high volume, already well above resistance.
    if move >= 20 and rv >= 3.0 and brk >= 10:
        return _stage("expansion")
    # Squeeze: short covering + momentum acceleration. Requires short-interest
    # FUEL — momentum alone is NOT a squeeze (spec §5).
    if fuel_s >= 40 and brk >= 5 and rv >= 3.0 and move >= 8:
        return _stage("squeeze")
    # Confirmation: breakout + abnormal volume (confirmation score present).
    if confirm_s >= 40 and brk >= 2 and rv >= 2.0:
        return _stage("confirmation")
    # Ignition: price beginning to move + volume increasing (trigger present).
    if trigger_s >= 30 and move >= 2 and rv >= 1.5:
        return _stage("ignition")
    # Setup: fuel plus an independent catalyst/trigger. Fuel alone is only a
    # watchlist observation, never a qualified setup.
    if fuel_s >= 40 and trigger_s >= 15:
        return _stage("setup")
    # Default: not enough fuel or movement to be a squeeze candidate.
    return {
        "stage": "none",
        "label": "No squeeze setup",
        "description": "Insufficient fuel or movement to classify.",
    }


def _stage(key: str) -> Dict[str, str]:
    return {
        "stage": key,
        "label": STAGE_LABELS.get(key, key),
        "description": STAGE_DESCRIPTIONS.get(key, ""),
    }


# ── EXPLOSION RADAR ───────────────────────────────────────────────────────
def explosion_score(factors: Optional[Dict[str, Any]]) -> float:
    """Weighted 0-100 explosion score from the factor breakdown (spec §6)."""
    f = factors or {}
    total = 0.0
    for key, weight in EXPLOSION_FACTORS.items():
        total += weight * _f(f.get(key))
    return round(min(100.0, total), 1)


def explosion_projection(score: float) -> Dict[str, Any]:
    """Heuristic (NOT statistically calibrated) probability projection.

    These are fixed linear formulas with NO outcome data, holdout validation,
    sample count, or confidence interval. They are directional heuristics for
    planning only — they must not be read as calibrated probabilities.
    """
    s = _f(score) / 100.0
    p20 = _clamp((0.10 + 0.60 * s) * 100.0, 5.0, 90.0)
    p50 = _clamp((0.05 + 0.45 * s) * 100.0, 3.0, 70.0)
    p100 = _clamp((0.02 + 0.25 * s) * 100.0, 1.0, 40.0)
    pneg20 = _clamp((0.30 - 0.15 * s) * 100.0, 8.0, 40.0)
    return {
        "p_plus_20_pct": round(p20, 1),
        "p_plus_50_pct": round(p50, 1),
        "p_plus_100_pct": round(p100, 1),
        "p_minus_20_pct": round(pneg20, 1),
        "calibrated": False,
        "display_as_probability": False,
        "publication_status": "research_only_unpublished",
        "disclaimer": (
            "This is NOT guaranteed to double. High short interest can produce "
            "violent moves in either direction, and squeeze conditions can "
            "disappear rapidly. These percentages are heuristic estimates, NOT "
            "statistically calibrated probabilities — they have no outcome data, "
            "holdout validation, or confidence interval behind them."
        ),
    }


def public_hunter_report(report: Dict[str, Any]) -> Dict[str, Any]:
    """Remove unpublished heuristic percentages from a public Hunter response."""
    public = dict(report or {})
    projection = dict(public.get("projection") or {})
    for key in tuple(projection):
        if key.startswith("p_") and key.endswith("_pct"):
            projection.pop(key, None)
    projection.update({
        "calibrated": False,
        "display_as_probability": False,
        "publication_status": "withheld_pending_calibration",
        "message": "Outcome percentages are withheld until holdout calibration is proven.",
    })
    public["projection"] = projection
    return public


def build_explosion_report(
    *,
    symbol: str,
    short_ctx: Optional[Dict[str, Any]] = None,
    trigger_ctx: Optional[Dict[str, Any]] = None,
    confirm_ctx: Optional[Dict[str, Any]] = None,
    factors: Optional[Dict[str, Any]] = None,
    move_pct: float = 0.0,
    rvol: float = 0.0,
    breakout_pct: float = 0.0,
    momentum_declining: bool = False,
    price_parabolic: bool = False,
    huge_volume: bool = False,
) -> Dict[str, Any]:
    """Full explosion report: fuel/trigger/confirmation + pressure + stage +
    explosion score + projection. Pure — no I/O."""
    fuel = score_fuel(short_ctx)
    trigger = score_trigger(trigger_ctx)
    confirmation = score_confirmation(confirm_ctx)
    pressure = squeeze_pressure_score(fuel, trigger, confirmation)
    band = pressure_band(pressure)
    stage = classify_stage(
        fuel=fuel,
        trigger=trigger,
        confirmation=confirmation,
        move_pct=move_pct,
        rvol=rvol,
        breakout_pct=breakout_pct,
        momentum_declining=momentum_declining,
        price_parabolic=price_parabolic,
        huge_volume=huge_volume,
    )
    exp_score = explosion_score(factors)
    projection = explosion_projection(exp_score)
    return {
        "symbol": (symbol or "").upper(),
        "fuel_score": fuel,
        "trigger_score": trigger,
        "confirmation_score": confirmation,
        "squeeze_pressure_score": pressure,
        "pressure_band": band["band"],
        "pressure_label": band["label"],
        "stage": stage["stage"],
        "stage_label": stage["label"],
        "stage_description": stage["description"],
        "explosion_score": exp_score,
        "factors": factors or {},
        "projection": projection,
    }


# ── FETCH LAYER (best-effort, free data only) ─────────────────────────────
_REFERENCE_MAX_AGE_S = 20 * 60
_REFERENCE_FUTURE_SKEW_S = 60
_REFERENCE_ISSUANCE_SKEW_S = 5 * 60
HUNTER_PLAN_VERSION = "1"


def validate_reference_quote(
    session: Optional[Dict[str, Any]],
    *,
    issued_ts: Optional[int] = None,
) -> Dict[str, Any]:
    """Validate a prospective outcome anchor without substituting request time."""
    sess = session or {}
    now = int(issued_ts or time.time())
    reasons = []
    try:
        price = float(sess.get("price"))
        if not math.isfinite(price) or price <= 0:
            raise ValueError
    except (TypeError, ValueError):
        price = None
        reasons.append("invalid_price")
    try:
        observed_at = int(sess.get("price_as_of_ts"))
    except (TypeError, ValueError):
        observed_at = None
        reasons.append("missing_price_timestamp")
    if observed_at is not None:
        age = now - observed_at
        if age < -_REFERENCE_FUTURE_SKEW_S:
            reasons.append("future_price_timestamp")
        elif age > _REFERENCE_MAX_AGE_S:
            reasons.append("stale_price")
        if abs(age) > _REFERENCE_ISSUANCE_SKEW_S:
            reasons.append("issuance_price_skew")
    if bool(sess.get("data_stale")):
        reasons.append("provider_marked_stale")
    market_date = str(sess.get("market_date") or "")
    session_name = str(sess.get("session") or "").lower()
    if not market_date:
        reasons.append("missing_market_date")
    if session_name not in {"rth", "afterhours"}:
        reasons.append("incompatible_session")
    return {
        "valid": not reasons,
        "reasons": reasons,
        "price": price if not reasons else None,
        "price_as_of_ts": observed_at if not reasons else None,
        "issued_ts": now,
        "session": session_name or None,
        "market_date": market_date or None,
        "cache_age_s": sess.get("cache_age_s"),
    }


def build_hunter_planning_levels(
    symbol: str,
    reference_validation: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build planning-only entry/target/stop levels from confirmed quote evidence.

    The entry is the validated point-in-time quote, not a promised fill. Missing,
    stale, untimestamped, or session-incompatible evidence fails closed: no
    target or stop is emitted. Geometry reuses the canonical stock TP/SL helper
    so Hunter does not invent a second risk formula.
    """
    validation = reference_validation or {}
    reasons = list(validation.get("reasons") or [])
    unavailable = {
        "status": "unavailable",
        "evidence_status": "UNVERIFIED",
        "plan_version": HUNTER_PLAN_VERSION,
        "purpose": "planning_only",
        "entry_price": None,
        "target_price": None,
        "stop_price": None,
        "target_pct": None,
        "stop_pct": None,
        "as_of_ts": validation.get("price_as_of_ts"),
        "session": validation.get("session"),
        "market_date": validation.get("market_date"),
        "reasons": reasons or ["unverified_reference_quote"],
        "disclaimer": (
            "Planning only — not a fired Ghost pick or guaranteed fill. "
            "No price can guarantee the best profit."
        ),
    }
    if validation.get("valid") is not True:
        return unavailable

    try:
        entry = float(validation.get("price"))
        if not math.isfinite(entry) or entry <= 0:
            return {**unavailable, "reasons": ["invalid_price"]}
        from core.tp_sl_resolve import tp_sl_prices_from_vol
        from core.vol_targets import base_vol_pct, stop_pct_from_vol

        vol_pct = float(base_vol_pct(symbol, "stock"))
        target, stop = tp_sl_prices_from_vol(entry, vol_pct, "UP")
        stop_pct = float(stop_pct_from_vol(vol_pct))
        if not all(math.isfinite(v) for v in (target, stop)) or not stop < entry < target:
            return {**unavailable, "reasons": ["invalid_plan_geometry"]}
    except Exception:
        return {**unavailable, "reasons": ["planning_geometry_unavailable"]}

    return {
        **unavailable,
        "status": "available",
        "evidence_status": "CONFIRMED",
        "entry_price": round(entry, 4),
        "target_price": round(target, 4),
        "stop_price": round(stop, 4),
        "target_pct": round(vol_pct * 100.0, 2),
        "stop_pct": round(stop_pct * 100.0, 2),
        "reasons": [],
    }


def _catalyst_to_trigger(catalyst_ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Map catalyst_scoring output to a 0-100 catalyst + guidance score.

    Unavailable catalyst data maps to 0 (NOT 50): missing evidence must not
    fabricate a neutral-positive signal. Guidance is reported under its own
    honest key (guidance_score), NOT mislabeled as earnings_surprise — the
    real earnings surprise is computed separately from actual-vs-expected EPS.
    """
    c = catalyst_ctx or {}
    if not c.get("available"):
        return {
            "catalyst_score": 0.0,
            "guidance_score": 0.0,
            "catalyst_available": False,
        }
    catalyst_score = _f(c.get("catalyst_score"))  # -1..1
    guidance = _f(c.get("guidance_momentum_score"))  # -1..1
    # Map -1..1 to 0..100 (50 = neutral).
    catalyst_0_100 = _clamp(50.0 + catalyst_score * 50.0)
    guidance_0_100 = _clamp(50.0 + guidance * 50.0)
    return {
        "catalyst_score": catalyst_0_100,
        "guidance_score": guidance_0_100,
        "catalyst_available": True,
    }


def _options_to_trigger(flow: Dict[str, Any]) -> Dict[str, Any]:
    """Map options flow to a 0-100 call-volume score."""
    f = flow or {}
    pcr = f.get("put_call_volume_ratio")
    cv = _f(f.get("total_call_volume"))
    if not f.get("available"):
        return {"call_volume_score": 0.0, "options_available": False}
    # Call-heavy (low PCR) + high call volume = bullish options activity.
    score = 0.0
    if pcr is not None:
        if pcr < 0.7:
            score += 60.0
        elif pcr < 1.0:
            score += 40.0
        elif pcr < 1.2:
            score += 20.0
    if cv > 0:
        score += min(40.0, cv / 10000.0)
    return {"call_volume_score": _clamp(score), "options_available": True}


def fetch_explosion_report(
    symbol: str,
    *,
    persist: bool = False,
    issued_ts: Optional[int] = None,
    cached_only: bool = False,
    market_metrics: Optional[Dict[str, Any]] = None,
    market_regime: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble a full explosion report for one symbol from free data sources.

    Best-effort: every source is optional and failures degrade to neutral
    (0/None) rather than raising. This is read-only intelligence — it never
    fires a pick or loosens any gate.

    `persist` defaults to False so public GET traffic does NOT write calibration
    samples. Only a preregistered scheduler (one evaluation per symbol/scoring
    version/market-time slot) should set persist=True; otherwise repeated page
    loads would inflate sample size and invalidate Wilson bounds.

    `cached_only` is used by the scheduled board refresh. It reads already
    warmed short data and batched market bars, and deliberately skips per-symbol
    yfinance/options calls. Public GET traffic never invokes this fetch path.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"ok": False, "error": "symbol required"}

    # Fuel: interactive/sampler requests may fetch; board refreshes only consume
    # the already-warmed cache so one scan cannot stampede external providers.
    short_ctx: Dict[str, Any] = {}
    try:
        if cached_only:
            from core.squeeze_monitor import _cached_short_context

            short_ctx = _cached_short_context(sym) or {}
        else:
            from core.squeeze_monitor import _short_context

            short_ctx = _short_context(sym) or {}
    except Exception:
        short_ctx = {}

    # Trigger: catalyst + earnings + premarket gap + options.
    trigger_ctx: Dict[str, Any] = {}
    try:
        from core.catalyst_scoring import fetch_event_context
        cat = fetch_event_context(sym)
        trigger_ctx.update(_catalyst_to_trigger(cat))
        # Catalyst freshness: age-weight the catalyst so stale/far-future
        # catalysts don't dominate a 1-14 day read (SPCE lesson).
        try:
            from core.catalyst_freshness import catalyst_timing_score
            timing = catalyst_timing_score(cat.get("events") or [])
            trigger_ctx["catalyst_timing_score"] = timing.get("score", 0.0)
            trigger_ctx["catalyst_timing"] = timing.get("best")
        except Exception:
            trigger_ctx["catalyst_timing_score"] = 0.0
    except Exception:
        trigger_ctx.update({"catalyst_score": 0.0, "earnings_surprise": 0.0, "catalyst_available": False, "catalyst_timing_score": 0.0})

    # Earnings and options are vendor-heavy. The scheduled board stays
    # conservative when they are not already part of a persisted evidence feed.
    if cached_only:
        trigger_ctx.update({"earnings_surprise": 0.0, "earnings_available": False})
    else:
        try:
            from core.earnings_surprise import earnings_surprise_to_trigger

            trigger_ctx.update(earnings_surprise_to_trigger(sym))
        except Exception:
            trigger_ctx.update({"earnings_surprise": 0.0, "earnings_available": False})

    if cached_only:
        trigger_ctx.update({"call_volume_score": 0.0, "options_available": False})
    else:
        try:
            from core.options_flow import probe_options_flow

            flow = probe_options_flow(sym)
            trigger_ctx.update(_options_to_trigger(flow))
        except Exception:
            trigger_ctx.update({"call_volume_score": 0.0, "options_available": False})

    # Confirmation + move/RVOL. Scheduled snapshots pass metrics built from a
    # handful of batched Alpaca bar requests. Interactive/sampler requests use
    # the full point-in-time session helper.
    confirm_ctx: Dict[str, Any] = {}
    move_pct = 0.0
    rvol = 0.0
    breakout_pct = 0.0
    reference_price = None
    reference_price_ts = None
    reference_validation = validate_reference_quote({}, issued_ts=issued_ts)
    if market_metrics is not None:
        metrics = market_metrics or {}
        rvol = _f(metrics.get("rvol"))
        move_pct = _f(metrics.get("peak_move_pct") or metrics.get("current_move_pct"))
        breakout_pct = max(0.0, _f(metrics.get("current_move_pct")))
        price = _f(metrics.get("price"))
        vwap = metrics.get("vwap")
        if vwap is not None and price:
            confirm_ctx["above_vwap"] = price >= _f(vwap)
        confirm_ctx.update({"rvol": rvol, "breakout_pct": breakout_pct})
        session_name = str(metrics.get("session") or "").lower()
        trigger_ctx["premarket_gap_pct"] = (
            _f(metrics.get("current_move_pct")) if session_name == "premarket" else 0.0
        )
        reference_validation = {
            **reference_validation,
            "reasons": ["snapshot_quote_not_independently_verified"],
            "session": session_name or None,
            "market_date": metrics.get("market_date"),
        }
    else:
        try:
            from core.squeeze_monitor import get_squeeze_picks

            board = get_squeeze_picks() or {}
            picks = board.get("picks") or []
            pick = next((p for p in picks if (p.get("symbol") or "").upper() == sym), None)
            if pick:
                rvol = _f(pick.get("rvol"))
                vwap = pick.get("vwap")
                price = _f(pick.get("price"))
                if vwap is not None and price:
                    confirm_ctx["above_vwap"] = price >= _f(vwap)
                move_pct = _f(pick.get("peak_move_pct"))
        except Exception:
            pass

        try:
            from core.prices import get_intraday_session

            sess = get_intraday_session(sym) or {}
            validation_ts = int(issued_ts or time.time())
            reference_validation = validate_reference_quote(sess, issued_ts=validation_ts)
            price = _f(sess.get("price"))
            prev = _f(sess.get("previous_close"))
            if price and prev and prev > 0:
                if move_pct == 0.0:
                    move_pct = (price - prev) / prev * 100.0
                breakout_pct = max(0.0, (price - prev) / prev * 100.0)
            reference_price = reference_validation.get("price")
            reference_price_ts = reference_validation.get("price_as_of_ts")
            confirm_ctx.update({"rvol": rvol, "breakout_pct": breakout_pct})
            trigger_ctx["premarket_gap_pct"] = (
                _f(sess.get("change_pct"))
                if str(sess.get("session") or "").lower() == "premarket"
                else 0.0
            )
        except Exception:
            reference_validation = validate_reference_quote({}, issued_ts=issued_ts)
            trigger_ctx["premarket_gap_pct"] = 0.0

    # score_trigger() reads rvol and breakout_pct from trigger_ctx — place them
    # there too, or the trigger score's RVOL/breakout terms are always 0.
    trigger_ctx["rvol"] = rvol
    trigger_ctx["breakout_pct"] = breakout_pct

    # Factors for the explosion score.
    fuel = score_fuel(short_ctx)
    env_score = market_environment_score(
        market_regime if cached_only else _fetch_market_regime(),
    )
    factors = {
        "short_squeeze_potential": fuel,
        "catalyst": _f(trigger_ctx.get("catalyst_score")),
        "earnings_surprise": _f(trigger_ctx.get("earnings_surprise")),
        "relative_volume": _clamp(max(0.0, rvol - 1.0) * 25.0),
        "technical_breakout": _clamp(max(0.0, breakout_pct) * 10.0),
        "options_activity": _f(trigger_ctx.get("call_volume_score")),
        "float_structure": _float_structure_score(short_ctx),
        "market_environment": env_score,
    }

    report = build_explosion_report(
        symbol=sym,
        short_ctx=short_ctx,
        trigger_ctx=trigger_ctx,
        confirm_ctx=confirm_ctx,
        factors=factors,
        move_pct=move_pct,
        rvol=rvol,
        breakout_pct=breakout_pct,
    )
    report["ok"] = True
    report["short_ctx"] = short_ctx
    report["trigger_ctx"] = trigger_ctx
    report["confirm_ctx"] = confirm_ctx
    evidence = {
        "short_interest": any(
            short_ctx.get(key) is not None
            for key in ("short_float_pct", "days_to_cover")
        ),
        "catalyst": trigger_ctx.get("catalyst_available") is True,
        "earnings": trigger_ctx.get("earnings_available") is True,
        "options": trigger_ctx.get("options_available") is True,
        "market_metrics": bool(market_metrics) or any(
            _f(confirm_ctx.get(key)) > 0 for key in ("rvol", "breakout_pct")
        ),
        "market_regime": env_score is not None,
    }
    available_count = sum(1 for value in evidence.values() if value)
    report["evidence_coverage"] = {
        "available": available_count,
        "total": len(evidence),
        "ratio": round(available_count / len(evidence), 3),
        "sources": evidence,
    }
    report["qualified"] = report.get("stage") != "none"
    report["reference_price"] = reference_price
    report["reference_price_ts"] = reference_price_ts
    report["reference_validation"] = reference_validation
    report["planning_levels"] = build_hunter_planning_levels(sym, reference_validation)

    # Persist a point-in-time audit trail ONLY when explicitly requested by a
    # preregistered scheduler. Public GET traffic must stay read-only so it
    # cannot inflate the calibration sample with correlated near-duplicates.
    report["evaluation_id"] = None
    report["persistence"] = {"status": "not_requested", "evaluation_id": None}
    if persist:
        try:
            from core.squeeze_hunter_ledger import persist_hunter_evaluation
            persistence = persist_hunter_evaluation(
                symbol=sym,
                report=report,
                short_ctx=short_ctx,
                trigger_ctx=trigger_ctx,
                confirm_ctx=confirm_ctx,
                reference_price=reference_price,
                reference_price_ts=reference_price_ts,
                session_date=reference_validation.get("market_date"),
                issued_ts=issued_ts,
                feature_available_ts=reference_price_ts,
            )
        except Exception:
            persistence = {"status": "database_unavailable", "evaluation_id": None}
        report["persistence"] = persistence
        report["evaluation_id"] = persistence.get("evaluation_id")
    return report


def _float_structure_score(short_ctx: Dict[str, Any]) -> float:
    """0-100 float-structure factor: smaller float = more squeeze-prone."""
    fs = _f((short_ctx or {}).get("float_shares"))
    if fs <= 0:
        return 0.0
    if fs < 10_000_000:
        return 100.0
    if fs < 50_000_000:
        return 70.0
    if fs < 200_000_000:
        return 40.0
    return 10.0


def market_environment_score(regime: Optional[Dict[str, Any]] = None) -> Optional[float]:
    """0-100 market-environment factor from the broad regime.

    Risk-on / calm tape is more favorable for explosive moves; risk-off /
    high-volatility tape is less. Unknown evidence stays unknown and contributes
    zero to the conservative composite instead of receiving phantom credit.
    """
    r = regime or {}
    label = str(r.get("label") or r.get("risk_state") or "").lower()
    if not label or label == "unknown":
        return None
    if label in ("calm_risk_on", "risk_on"):
        return 80.0
    if label == "mixed":
        return 55.0
    if label == "risk_off":
        return 35.0
    if label == "risk_off_high_volatility":
        return 20.0
    return None


def _fetch_market_regime() -> Optional[Dict[str, Any]]:
    """Best-effort broad market regime from VIX (free, real signal).

    VIX is the cheapest honest proxy for risk-on/risk-off: high VIX = risk-off
    (unfavorable for explosive moves), low VIX = calm risk-on.
    """
    try:
        from core.prices import get_vix
        vix = get_vix()
        if vix is None:
            return None
        vix = _f(vix)
        if vix >= 25:
            return {"label": "risk_off_high_volatility", "risk_state": "risk_off", "vix": vix}
        if vix < 15:
            return {"label": "calm_risk_on", "risk_state": "risk_on", "vix": vix}
        if vix < 20:
            return {"label": "risk_on", "risk_state": "risk_on", "vix": vix}
        return {"label": "mixed", "risk_state": "neutral", "vix": vix}
    except Exception:
        return None


_HUNTER_SNAPSHOT_TTL_S = int(os.getenv("HUNTER_SNAPSHOT_TTL_S", "1800"))
_HUNTER_SNAPSHOT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "squeeze_hunter_snapshot.json",
)
_HUNTER_SNAPSHOT_LOCK = threading.Lock()
_hunter_snapshot: Dict[str, Any] = {}


def _batched_market_context(symbols: list[str]) -> Dict[str, Dict[str, Any]]:
    """Build market context with bounded Alpaca batch calls, never per-symbol I/O."""
    try:
        from core.market_hours import is_us_premarket, is_us_rth, session_hm
        from core.squeeze_monitor import (
            batched_market_metrics,
            compute_rvol,
            rth_elapsed_fraction,
        )

        batch = batched_market_metrics(symbols)
        elapsed = rth_elapsed_fraction()
        now_ct = session_hm()[0]
        session_name = "rth" if is_us_rth() else (
            "premarket" if is_us_premarket() else "afterhours"
        )
        out: Dict[str, Dict[str, Any]] = {}
        for symbol in symbols:
            metrics = batch.get(symbol)
            if not metrics:
                continue
            out[symbol] = {
                **metrics,
                "rvol": compute_rvol(
                    metrics["session_volume"], metrics["avg_daily_volume"], elapsed,
                ),
                "session": session_name,
                "market_date": now_ct.date().isoformat(),
            }
        return out
    except Exception as exc:
        LOGGER.warning("Hunter batch market context unavailable: %s", str(exc)[:120])
        return {}


def scan_watchlist(symbols: Optional[list] = None, limit: int = 20) -> Dict[str, Any]:
    """Build a conservative Hunter board without per-symbol vendor calls.

    This function is for the scheduler only. The public endpoint reads the last
    completed snapshot through :func:`get_hunter_snapshot`.
    """
    started = time.time()
    if symbols is None:
        try:
            from config.symbols import get_edge_set

            # Match the actively monitored research universe. The larger UI
            # watchlist contains symbols whose short context is intentionally
            # not prewarmed, which would make rankings mostly unknown data.
            symbols = sorted(get_edge_set())
        except Exception:
            symbols = []
    symbols = [str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()]
    regime = _fetch_market_regime()
    env_score = market_environment_score(regime)
    market_context = _batched_market_context(symbols)

    rows: list = []
    errors: list = []
    for sym in symbols:
        try:
            rep = fetch_explosion_report(
                sym,
                cached_only=True,
                market_metrics=market_context.get(sym, {}),
                market_regime=regime,
            )
            rows.append(public_hunter_report(rep))
        except Exception as exc:
            errors.append({"symbol": sym, "error": str(exc)[:120]})

    rows.sort(key=lambda r: _f(r.get("explosion_score")), reverse=True)
    qualified = [row for row in rows if row.get("qualified") is True]
    observations = [row for row in rows if row.get("qualified") is not True]
    short_coverage = sum(
        1 for row in rows
        if ((row.get("evidence_coverage") or {}).get("sources") or {}).get("short_interest")
    )
    confirmed_quotes = sum(
        1 for row in rows
        if (row.get("planning_levels") or {}).get("evidence_status") == "CONFIRMED"
    )
    return {
        "ok": True,
        "status": "ready",
        "scanned": len(rows),
        "returned": min(limit, len(qualified)) + min(limit, len(observations)),
        "qualified_count": len(qualified),
        "errors": errors,
        "market_environment_score": env_score,
        "regime": regime,
        "candidates": qualified[:limit],
        "watchlist": observations[:limit],
        "_rows": rows,
        "data_health": {
            "market_metrics": len(market_context),
            "short_interest": short_coverage,
            "confirmed_quotes": confirmed_quotes,
            "symbols": len(rows),
            "degraded": bool(
                rows and (
                    len(market_context) < len(rows) * 0.8
                    or short_coverage < len(rows) * 0.5
                )
            ),
        },
        "duration_ms": int((time.time() - started) * 1000),
    }


def _persist_hunter_snapshot(snapshot: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(_HUNTER_SNAPSHOT_PATH), exist_ok=True)
        tmp = f"{_HUNTER_SNAPSHOT_PATH}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, separators=(",", ":"))
        os.replace(tmp, _HUNTER_SNAPSHOT_PATH)
    except Exception as exc:
        LOGGER.debug("Hunter snapshot persist failed: %s", str(exc)[:120])


def _load_hunter_snapshot() -> None:
    global _hunter_snapshot
    if _hunter_snapshot or not os.path.isfile(_HUNTER_SNAPSHOT_PATH):
        return
    try:
        with open(_HUNTER_SNAPSHOT_PATH, encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict) and loaded.get("generated_at_ts"):
            _hunter_snapshot = loaded
    except Exception as exc:
        LOGGER.debug("Hunter snapshot load failed: %s", str(exc)[:120])


def get_hunter_snapshot(limit: int = 20) -> Dict[str, Any]:
    """Return the last complete board immediately; never call a market provider."""
    _load_hunter_snapshot()
    if not _hunter_snapshot:
        return {
            "ok": False,
            "status": "warming",
            "message": "Hunter board is warming; no completed snapshot is available yet.",
            "candidates": [],
            "watchlist": [],
        }
    payload = json.loads(json.dumps({
        key: value for key, value in _hunter_snapshot.items() if key != "_rows"
    }))
    age_s = max(0, int(time.time()) - int(payload.get("generated_at_ts") or 0))
    payload["snapshot_age_s"] = age_s
    payload["snapshot_stale"] = age_s > _HUNTER_SNAPSHOT_TTL_S
    payload["candidates"] = list(payload.get("candidates") or [])[:limit]
    payload["watchlist"] = list(payload.get("watchlist") or [])[:limit]
    return payload


def get_hunter_symbol_snapshot(symbol: str) -> Dict[str, Any]:
    """Return one symbol from the completed board without provider I/O."""
    sym = (symbol or "").strip().upper()
    _load_hunter_snapshot()
    rows = list(_hunter_snapshot.get("_rows") or [])
    if not rows:
        rows = list(_hunter_snapshot.get("candidates") or []) + list(
            _hunter_snapshot.get("watchlist") or [],
        )
    row = next((item for item in rows if item.get("symbol") == sym), None)
    if row is None:
        return {
            "ok": False,
            "status": "not_in_snapshot" if _hunter_snapshot else "warming",
            "symbol": sym,
            "error": "No completed Hunter snapshot is available for this symbol.",
        }
    result = json.loads(json.dumps(row))
    generated = int(_hunter_snapshot.get("generated_at_ts") or 0)
    result["generated_at_ts"] = generated or None
    result["snapshot_age_s"] = max(0, int(time.time()) - generated) if generated else None
    result["snapshot_stale"] = bool(
        generated and int(time.time()) - generated > _HUNTER_SNAPSHOT_TTL_S
    )
    return result


def refresh_hunter_snapshot(*, symbols: Optional[list] = None, limit: int = 20) -> Dict[str, Any]:
    """Single-flight scheduled refresh for the public Hunter board."""
    global _hunter_snapshot
    if symbols is None:
        try:
            from core.market_hours import is_us_extended_hours, session_hm

            if not is_us_extended_hours():
                current = get_hunter_snapshot(limit=limit)
                if current.get("ok"):
                    current["refresh_skipped"] = "market_closed"
                    return current
                return {
                    "ok": True,
                    "status": "waiting_for_market",
                    "message": "Hunter refresh waits for US extended market hours.",
                    "candidates": [],
                    "watchlist": [],
                }
            now_ct = session_hm()[0]
            hm = now_ct.hour * 60 + now_ct.minute
            if 15 * 60 + 5 <= hm < 16 * 60:
                current = get_hunter_snapshot(limit=limit)
                if current.get("ok"):
                    current["refresh_skipped"] = "calibration_issuance_window"
                    return current
        except Exception:
            pass
    if not _HUNTER_SNAPSHOT_LOCK.acquire(blocking=False):
        current = get_hunter_snapshot(limit=limit)
        current["refresh_in_progress"] = True
        return current
    try:
        snapshot = scan_watchlist(symbols=symbols, limit=limit)
        snapshot["generated_at_ts"] = int(time.time())
        snapshot["snapshot_age_s"] = 0
        snapshot["snapshot_stale"] = False
        _hunter_snapshot = snapshot
        _persist_hunter_snapshot(snapshot)
        return get_hunter_snapshot(limit=limit)
    finally:
        _HUNTER_SNAPSHOT_LOCK.release()
