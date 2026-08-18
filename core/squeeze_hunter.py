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

from typing import Any, Dict, Optional

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
    # Setup: fuel present, no move yet.
    if fuel_s >= 40:
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
        "disclaimer": (
            "This is NOT guaranteed to double. High short interest can produce "
            "violent moves in either direction, and squeeze conditions can "
            "disappear rapidly. These percentages are heuristic estimates, NOT "
            "statistically calibrated probabilities — they have no outcome data, "
            "holdout validation, or confidence interval behind them."
        ),
    }


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


def fetch_explosion_report(symbol: str) -> Dict[str, Any]:
    """Assemble a full explosion report for one symbol from free data sources.

    Best-effort: every source is optional and failures degrade to neutral
    (0/None) rather than raising. This is read-only intelligence — it never
    fires a pick or loosens any gate.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"ok": False, "error": "symbol required"}

    # Fuel: short context (yfinance + finviz fallback).
    short_ctx: Dict[str, Any] = {}
    try:
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

    # Earnings surprise: actual vs expected (relative, not absolute sign).
    try:
        from core.earnings_surprise import earnings_surprise_to_trigger
        earn = earnings_surprise_to_trigger(sym)
        trigger_ctx.update(earn)
    except Exception:
        trigger_ctx.update({"earnings_surprise": 0.0, "earnings_available": False})

    try:
        from core.prices import get_extended_session
        sess = get_extended_session(sym) or {}
        # Only treat gap_pct as a PREMARKET gap when we are actually in the
        # premarket session. During RTH/after-hours, gap_pct is the session
        # move vs prior close and would double-count price movement.
        if str(sess.get("session") or "").lower() == "premarket":
            trigger_ctx["premarket_gap_pct"] = _f(sess.get("gap_pct"))
        else:
            trigger_ctx["premarket_gap_pct"] = 0.0
    except Exception:
        trigger_ctx["premarket_gap_pct"] = 0.0

    try:
        from core.options_flow import probe_options_flow
        flow = probe_options_flow(sym)
        trigger_ctx.update(_options_to_trigger(flow))
    except Exception:
        trigger_ctx.update({"call_volume_score": 0.0, "options_available": False})

    # Confirmation + move/rvol: prefer the squeeze radar's live metrics
    # (rvol, vwap, above_vwap) when the symbol is an active radar pick; fall
    # back to the intraday session for move/breakout vs prior close.
    confirm_ctx: Dict[str, Any] = {}
    move_pct = 0.0
    rvol = 0.0
    breakout_pct = 0.0
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
            confirm_ctx["rvol"] = rvol
    except Exception:
        pass

    try:
        from core.prices import get_intraday_session
        sess = get_intraday_session(sym) or {}
        price = _f(sess.get("price"))
        prev = _f(sess.get("previous_close"))
        if price and prev and prev > 0:
            if move_pct == 0.0:
                move_pct = (price - prev) / prev * 100.0
            # Breakout: price above prior close (a real reference), NOT today's
            # high (which is ~0 by construction and never signals a breakout).
            breakout_pct = max(0.0, (price - prev) / prev * 100.0)
        confirm_ctx["breakout_pct"] = breakout_pct
        if "rvol" not in confirm_ctx:
            confirm_ctx["rvol"] = rvol
    except Exception:
        pass

    # score_trigger() reads rvol and breakout_pct from trigger_ctx — place them
    # there too, or the trigger score's RVOL/breakout terms are always 0.
    trigger_ctx["rvol"] = rvol
    trigger_ctx["breakout_pct"] = breakout_pct

    # Factors for the explosion score.
    fuel = score_fuel(short_ctx)
    env_score = market_environment_score(_fetch_market_regime())
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


def market_environment_score(regime: Optional[Dict[str, Any]] = None) -> float:
    """0-100 market-environment factor from the broad regime.

    Risk-on / calm tape is more favorable for explosive moves; risk-off /
    high-volatility tape is less. Unknown regime = neutral 50.
    """
    r = regime or {}
    label = str(r.get("label") or r.get("risk_state") or "").lower()
    if not label or label == "unknown":
        return 50.0
    if label in ("calm_risk_on", "risk_on"):
        return 80.0
    if label == "mixed":
        return 55.0
    if label == "risk_off":
        return 35.0
    if label == "risk_off_high_volatility":
        return 20.0
    return 50.0


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


def scan_watchlist(symbols: Optional[list] = None, limit: int = 20) -> Dict[str, Any]:
    """Score the whole watchlist and return top explosion candidates.

    Read-only intelligence. Each symbol is fetched best-effort; failures
    degrade to a low/neutral report rather than raising. Sorted by explosion
    score descending. This is NOT a full-market scan — it uses the configured
    watchlist (104 symbols) to stay within rate limits.
    """
    if symbols is None:
        try:
            from config.symbols import watchlist_symbols
            symbols = sorted(watchlist_symbols())
        except Exception:
            symbols = []
    regime = _fetch_market_regime()
    env_score = market_environment_score(regime)

    rows: list = []
    errors: list = []
    for sym in symbols:
        try:
            rep = fetch_explosion_report(sym)
            # Override the neutral market-environment factor with the real one.
            if rep.get("ok"):
                factors = dict(rep.get("factors") or {})
                factors["market_environment"] = env_score
                rep["factors"] = factors
                rep["explosion_score"] = explosion_score(factors)
                rep["projection"] = explosion_projection(rep["explosion_score"])
            rows.append(rep)
        except Exception as exc:
            errors.append({"symbol": sym, "error": str(exc)[:120]})

    rows.sort(key=lambda r: _f(r.get("explosion_score")), reverse=True)
    return {
        "ok": True,
        "scanned": len(rows),
        "errors": errors,
        "market_environment_score": env_score,
        "regime": regime,
        "candidates": rows[:limit],
    }
