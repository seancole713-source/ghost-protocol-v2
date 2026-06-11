"""Squeeze scorecard — setup / trigger / confirmation + heuristic probability targets.

Heuristic v1 (not ML-trained): weighted factors aligned with short-squeeze screeners.
Probabilities are calibrated estimates for radar display, not guaranteed forecasts.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def _clamp_pct(x: float, lo: float = 5.0, hi: float = 92.0) -> float:
    return max(lo, min(hi, x))


def score_setup(short_ctx: Optional[Dict[str, Any]]) -> float:
    """Fuel: short float, days-to-cover, squeeze-risk tag."""
    ctx = short_ctx or {}
    sf = float(ctx.get("short_float_pct") or 0)
    dtc = float(ctx.get("days_to_cover") or 0)
    risk = ctx.get("squeeze_risk") or ""
    pts = min(45.0, sf * 1.5) + min(35.0, dtc * 5.0)
    pts += {"extreme": 25.0, "high": 18.0, "medium": 10.0, "low": 4.0}.get(risk, 0.0)
    return round(min(100.0, pts), 1)


def score_trigger(peak_move_pct: float, current_move_pct: float, *, has_catalyst: bool = False) -> float:
    """Spark: intraday move (+ optional catalyst flag when wired)."""
    move = max(float(peak_move_pct or 0), float(current_move_pct or 0))
    pts = min(75.0, max(0.0, move) * 7.5)
    if has_catalyst:
        pts += 25.0
    return round(min(100.0, pts), 1)


def score_confirmation(rvol: float, above_vwap: Optional[bool]) -> float:
    """Participation: RVOL + price vs session VWAP."""
    pts = min(65.0, max(0.0, float(rvol) - 0.5) * 18.0)
    if above_vwap is True:
        pts += 35.0
    elif above_vwap is False:
        pts += 5.0
    else:
        pts += 12.0
    return round(min(100.0, pts), 1)


def squeeze_score(setup: float, trigger: float, confirmation: float) -> float:
    """Weighted composite 0–100."""
    total = 0.35 * setup + 0.25 * trigger + 0.40 * confirmation
    return round(min(100.0, max(0.0, total)), 1)


def probability_targets(
    *,
    squeeze_score_val: float,
    rvol: float,
    peak_move_pct: float,
    above_vwap: Optional[bool],
    kind: Optional[str] = None,
) -> Dict[str, float]:
    """
    Heuristic probability targets (%), not ML — for radar-facing trade planning.

    - p_continue_3pct_60m: odds of another +3% within ~60 minutes
    - p_vwap_hold: odds first pullback holds VWAP
    - p_close_above_prior_high: odds finish above prior session high
    - p_exhaustion_soon: odds of sharp mean-reversion / fade
    """
    base = squeeze_score_val / 100.0
    rvol_n = min(1.0, max(0.0, (float(rvol) - 0.5) / 2.5))
    move_n = min(1.0, max(0.0, float(peak_move_pct) / 15.0))
    vwap_b = 1.0 if above_vwap is True else (0.35 if above_vwap is False else 0.55)
    active_boost = 0.08 if kind == "squeeze_active" else 0.0

    p_cont = _clamp_pct(
        100.0 * (0.10 + 0.42 * base + 0.22 * rvol_n + 0.12 * move_n + 0.08 * vwap_b + active_boost),
    )
    p_vwap = _clamp_pct(100.0 * (0.08 + 0.38 * base + 0.28 * vwap_b + 0.10 * rvol_n))
    p_high = _clamp_pct(100.0 * (0.06 + 0.35 * base + 0.20 * move_n + 0.12 * rvol_n))
    p_exhaust = _clamp_pct(
        100.0 * (0.12 + 0.45 * (1.0 - base) + 0.25 * max(0.0, move_n - 0.6) + 0.10 * max(0.0, rvol_n - 0.7)),
        lo=8.0,
        hi=88.0,
    )
    return {
        "p_continue_3pct_60m": round(p_cont, 1),
        "p_vwap_hold": round(p_vwap, 1),
        "p_close_above_prior_high": round(p_high, 1),
        "p_exhaustion_soon": round(p_exhaust, 1),
    }


def compute_stop(
    price: float,
    *,
    vwap: Optional[float] = None,
    prior_close: Optional[float] = None,
) -> float:
    """Invalidation: below VWAP or prior close, with a small buffer."""
    anchors = [price * 0.975]
    if vwap and vwap > 0:
        anchors.append(vwap * 0.995)
    if prior_close and prior_close > 0:
        anchors.append(prior_close * 0.995)
    return round(min(anchors), 2)


def build_scorecard_row(
    symbol: str,
    metrics: Dict[str, Any],
    rvol: float,
    short_ctx: Optional[Dict[str, Any]] = None,
    *,
    kind: Optional[str] = None,
    has_catalyst: bool = False,
) -> Dict[str, Any]:
    """Full radar row: levels + scorecard + probability targets."""
    price = float(metrics["price"])
    session_high = float(metrics.get("session_high") or price)
    prior_close = float(metrics.get("prior_close") or 0)
    vwap = metrics.get("vwap")
    vwap_f = float(vwap) if vwap else None
    above_vwap: Optional[bool] = None
    if vwap_f and vwap_f > 0:
        above_vwap = price >= vwap_f

    setup = score_setup(short_ctx)
    trigger = score_trigger(metrics["peak_move_pct"], metrics["current_move_pct"], has_catalyst=has_catalyst)
    confirm = score_confirmation(rvol, above_vwap)
    total = squeeze_score(setup, trigger, confirm)
    probs = probability_targets(
        squeeze_score_val=total,
        rvol=rvol,
        peak_move_pct=float(metrics["peak_move_pct"]),
        above_vwap=above_vwap,
        kind=kind,
    )
    try:
        from core.squeeze_ml_v2 import blend_probabilities, model_info

        short_risk = (short_ctx or {}).get("squeeze_risk")
        probs = blend_probabilities(
            probs,
            setup_score=setup,
            trigger_score=trigger,
            confirm_score=confirm,
            rvol=rvol,
            peak_move_pct=float(metrics["peak_move_pct"]),
            above_vwap=above_vwap,
            short_risk=short_risk,
        )
        ml_meta = model_info()
    except Exception:
        ml_meta = {"model": "heuristic_v1"}
    from core.squeeze_monitor import squeeze_trade_levels

    trade_kind = kind or "squeeze_forming"
    buy, sell = squeeze_trade_levels(price, session_high, trade_kind)
    stop = compute_stop(price, vwap=vwap_f, prior_close=prior_close if prior_close > 0 else None)

    row: Dict[str, Any] = {
        "symbol": symbol.upper(),
        "buy": buy,
        "sell": sell,
        "stop": stop,
        "price": round(price, 2),
        "vwap": round(vwap_f, 2) if vwap_f else None,
        "above_vwap": above_vwap,
        "peak_move_pct": round(float(metrics["peak_move_pct"]), 2),
        "current_move_pct": round(float(metrics["current_move_pct"]), 2),
        "rvol": round(float(rvol), 2),
        "squeeze_score": total,
        "setup_score": setup,
        "trigger_score": trigger,
        "confirm_score": confirm,
        "probabilities": probs,
        "probability_model": ml_meta.get("model", "heuristic_v1"),
        "short_float_pct": (short_ctx or {}).get("short_float_pct"),
        "days_to_cover": (short_ctx or {}).get("days_to_cover"),
        "short_risk": (short_ctx or {}).get("squeeze_risk"),
    }
    if kind:
        row["kind"] = kind
    return row


def scorecard_legend() -> Dict[str, Any]:
    """API/cockpit copy for what the scores mean."""
    try:
        from core.squeeze_ml_v2 import model_info
        ml = model_info()
    except Exception:
        ml = {}
    return {
        "model": ml.get("model", "heuristic_v1"),
        "probability_blend": ml,
        "timezone": "America/Chicago",
        "session": {
            "premarket": "3:00 AM – 8:30 AM CT",
            "cash": "8:30 AM – 3:00 PM CT",
            "after_hours": "3:00 PM – 7:00 PM CT",
        },
        "components": {
            "setup": "Short % float, days-to-cover, squeeze-risk tag (fuel)",
            "trigger": "Intraday move vs prior close (spark)",
            "confirm": "Time-adjusted RVOL + price vs session VWAP (participation)",
        },
        "squeeze_score": "35% setup + 25% trigger + 40% confirmation (0–100)",
        "probabilities_note": "Heuristic estimates for planning — not ML-trained forecasts.",
        "probability_labels": {
            "p_continue_3pct_60m": "P(+3% next ~60 min)",
            "p_vwap_hold": "P(pullback holds VWAP)",
            "p_close_above_prior_high": "P(close above prior session high)",
            "p_exhaustion_soon": "P(sharp fade / mean reversion)",
        },
    }
