"""Prediction precision scoring helpers (PR #102).

Ghost has historically used WIN/LOSS to mean target/stop or directional truth.
That is necessary, but it is not enough: a target can be crossed while the
predicted high/low levels are still materially off. This module separates:

- direction / target-stop result: did the thesis work?
- price precision: how close were predicted open, low/stop, high/target levels?
- mistake type + lesson: what should Ghost learn next?

The helpers are pure and side-effect free so squeeze outcomes, Super Ghost
ledger scoring, API endpoints, and UI tests can all use the same definition.
Nothing here is financial advice or auto-trading logic; it is measurement only.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional


PRICE_PRECISION_TARGET = 60.0
TARGET_TOO_LOW_PCT = 4.0
TARGET_TOO_HIGH_PCT = -4.0
STOP_TOO_WIDE_PCT = 4.0
STOP_TOO_TIGHT_PCT = -1.0


def _f(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        out = float(v)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _round(v: Optional[float], places: int = 3) -> Optional[float]:
    return round(v, places) if v is not None and math.isfinite(v) else None


def _pct_error(predicted: Optional[float], actual: Optional[float]) -> Optional[float]:
    """Signed pct error: positive means actual printed above prediction."""
    p = _f(predicted)
    a = _f(actual)
    if p is None or a is None or p == 0:
        return None
    return (a - p) / abs(p) * 100.0


def _component_score(abs_error_pct: Optional[float]) -> Optional[float]:
    """Strict but smooth price-precision score for one price level.

    0% error => 100, 1% => 90, 4% => 60, 8% => 20, >=10% => 0.
    This intentionally refuses to call a directionally-correct prediction
    "precise" when the actual high/low/open is far from Ghost's level.
    """
    if abs_error_pct is None:
        return None
    return round(max(0.0, 100.0 - abs(float(abs_error_pct)) * 10.0), 1)


def precision_grade(score: Optional[float]) -> str:
    if score is None:
        return "—"
    if score >= 90:
        return "A"
    if score >= 75:
        return "B"
    if score >= 60:
        return "C"
    if score >= 45:
        return "D"
    return "F"


def _weighted_score(parts: Dict[str, Optional[float]]) -> Optional[float]:
    weights = {"open": 0.20, "low": 0.30, "high": 0.35, "close": 0.15}
    total = 0.0
    denom = 0.0
    for k, score in parts.items():
        if score is None:
            continue
        w = weights.get(k, 0.0)
        total += float(score) * w
        denom += w
    if denom <= 0:
        return None
    return round(total / denom, 1)


def _target_stop_result(
    direction: str,
    target: Optional[float],
    stop: Optional[float],
    live_low: Optional[float],
    live_high: Optional[float],
) -> Dict[str, Any]:
    d = (direction or "HOLD").upper()
    tgt = _f(target)
    stp = _f(stop)
    lo = _f(live_low)
    hi = _f(live_high)

    if d not in ("UP", "DOWN") or (lo is None and hi is None):
        return {"result": "PENDING", "hit_target": None, "hit_stop": None}

    if d == "DOWN":
        hit_target = bool(tgt is not None and lo is not None and lo <= tgt)
        hit_stop = bool(stp is not None and hi is not None and hi >= stp)
    else:
        hit_target = bool(tgt is not None and hi is not None and hi >= tgt)
        hit_stop = bool(stp is not None and lo is not None and lo <= stp)

    if hit_target and not hit_stop:
        result = "WIN"
    elif hit_stop and not hit_target:
        result = "LOSS"
    elif hit_target and hit_stop:
        result = "MIXED"
    else:
        result = "NEUTRAL"
    return {"result": result, "hit_target": hit_target, "hit_stop": hit_stop}


def _direction_from_close(direction: str, entry: Optional[float], close: Optional[float]) -> Optional[bool]:
    d = (direction or "HOLD").upper()
    e = _f(entry)
    c = _f(close)
    if e is None or c is None:
        return None
    ret = (c - e) / e * 100.0 if e else None
    if ret is None:
        return None
    if d == "UP":
        return ret > 0
    if d == "DOWN":
        return ret < 0
    return abs(ret) < 3.0


def _classify_mistake(
    *,
    direction: str,
    target_stop_result: str,
    target_error_pct: Optional[float],
    stop_error_pct: Optional[float],
    close_direction_correct: Optional[bool],
    precision_score: Optional[float],
) -> Dict[str, str]:
    d = (direction or "HOLD").upper()
    result = target_stop_result or "PENDING"

    if result == "PENDING":
        return {"mistake_type": "awaiting_truth", "lesson": "Live market truth is not complete yet; wait for enough OHLC data before learning."}
    if d == "HOLD":
        if close_direction_correct is True:
            return {"mistake_type": "good_skip", "lesson": "NO EDGE/HOLD was appropriate; realized move stayed muted."}
        return {"mistake_type": "missed_move", "lesson": "Ghost skipped a meaningful move; review catalysts, momentum, and feature coverage."}
    if result == "LOSS":
        return {"mistake_type": "wrong_direction_or_stop_hit", "lesson": "Stop/invalid level was hit before target; reduce confidence for similar setups until evidence improves."}
    if result == "MIXED":
        return {"mistake_type": "stop_too_tight_or_path_uncertain", "lesson": "Both target and stop printed in the same session window; Ghost needs finer intraday path/order evidence before counting this cleanly."}
    if target_error_pct is not None and target_error_pct >= TARGET_TOO_LOW_PCT:
        return {"mistake_type": "target_too_low", "lesson": "Direction worked, but the target/high estimate was too conservative; widen targets only after this repeats with evidence."}
    if target_error_pct is not None and target_error_pct <= TARGET_TOO_HIGH_PCT:
        return {"mistake_type": "target_too_high", "lesson": "Ghost expected more move than the market delivered; tighten targets or require stronger catalysts."}
    if stop_error_pct is not None and stop_error_pct >= STOP_TOO_WIDE_PCT:
        return {"mistake_type": "stop_too_wide", "lesson": "Actual low stayed well above the stop; risk estimate may be too loose for this setup."}
    if stop_error_pct is not None and stop_error_pct <= STOP_TOO_TIGHT_PCT and result != "LOSS":
        return {"mistake_type": "stop_too_tight", "lesson": "Price pressed through/near the stop while thesis survived; stop placement may need more volatility room."}
    if precision_score is not None and precision_score < PRICE_PRECISION_TARGET:
        return {"mistake_type": "direction_right_low_precision", "lesson": "Ghost got the thesis directionally right but price levels were not close enough; improve range calibration."}
    if result == "WIN":
        return {"mistake_type": "precise_direction_win", "lesson": "Target/stop thesis worked and price levels were within acceptable tolerance."}
    if result == "NEUTRAL":
        return {"mistake_type": "no_follow_through", "lesson": "Neither target nor stop was hit; keep collecting data and avoid promoting this as a clean win."}
    return {"mistake_type": "uncategorized", "lesson": "Outcome needs manual review; no automatic learning adjustment applied."}


def score_trade_precision(
    *,
    direction: str,
    entry: Any = None,
    target: Any = None,
    stop: Any = None,
    live_open: Any = None,
    live_low: Any = None,
    live_high: Any = None,
    live_close: Any = None,
) -> Dict[str, Any]:
    """Score a prediction against realized/live OHLC.

    For UP calls, target maps to predicted high and stop maps to predicted low.
    For DOWN calls, target maps to predicted low and stop maps to predicted high.
    WIN/LOSS remains target/stop truth. Precision is separate and price-level
    based, so a row can be `WIN` but still have a weak precision score.
    """
    d = (direction or "HOLD").upper()
    e = _f(entry)
    tgt = _f(target)
    stp = _f(stop)
    o = _f(live_open)
    lo = _f(live_low)
    hi = _f(live_high)
    c = _f(live_close)

    if d == "DOWN":
        predicted_low = tgt
        predicted_high = stp
        target_actual = lo
        stop_actual = hi
        raw_target_error = _pct_error(tgt, lo)
        target_error = -raw_target_error if raw_target_error is not None else None  # positive => target too conservative/downside went farther
        raw_stop_error = _pct_error(stp, hi)
        stop_error = raw_stop_error
    else:
        predicted_low = stp
        predicted_high = tgt
        target_actual = hi
        stop_actual = lo
        target_error = _pct_error(tgt, hi)
        stop_error = _pct_error(stp, lo)

    open_err = _pct_error(e, o)
    low_err = _pct_error(predicted_low, lo)
    high_err = _pct_error(predicted_high, hi)
    close_err = _pct_error(e, c)
    component_scores = {
        "open": _component_score(abs(open_err)) if open_err is not None else None,
        "low": _component_score(abs(low_err)) if low_err is not None else None,
        "high": _component_score(abs(high_err)) if high_err is not None else None,
        # Close is only used as a weak fallback/extra component; entry is not a close prediction.
        "close": _component_score(abs(close_err)) if close_err is not None else None,
    }
    price_score = _weighted_score(component_scores)
    target_stop = _target_stop_result(d, tgt, stp, lo, hi)
    close_correct = _direction_from_close(d, e, c)
    cls = _classify_mistake(
        direction=d,
        target_stop_result=target_stop["result"],
        target_error_pct=target_error,
        stop_error_pct=stop_error,
        close_direction_correct=close_correct,
        precision_score=price_score,
    )
    direction_score = 100 if target_stop["result"] == "WIN" else 0 if target_stop["result"] == "LOSS" else 50 if target_stop["result"] in ("NEUTRAL", "MIXED") else None
    overall_score = None
    if price_score is not None and direction_score is not None:
        overall_score = round(price_score * 0.75 + direction_score * 0.25, 1)

    return {
        "direction": d,
        "target_stop_result": target_stop["result"],
        "direction_result": target_stop["result"],
        "direction_score": direction_score,
        "hit_target": target_stop["hit_target"],
        "hit_stop": target_stop["hit_stop"],
        "precision_score": price_score,
        "precision_grade": precision_grade(price_score),
        "overall_score": overall_score,
        "component_scores": component_scores,
        "errors_pct": {
            "open": _round(open_err),
            "low": _round(low_err),
            "high": _round(high_err),
            "close_vs_entry": _round(close_err),
            "target": _round(target_error),
            "stop": _round(stop_error),
        },
        "predicted": {
            "open": _round(e, 4),
            "low": _round(predicted_low, 4),
            "high": _round(predicted_high, 4),
            "target": _round(tgt, 4),
            "stop": _round(stp, 4),
        },
        "actual": {
            "open": _round(o, 4),
            "low": _round(lo, 4),
            "high": _round(hi, 4),
            "close": _round(c, 4),
            "target_actual": _round(target_actual, 4),
            "stop_actual": _round(stop_actual, 4),
        },
        "mistake_type": cls["mistake_type"],
        "lesson": cls["lesson"],
        "note": "WIN/LOSS is target-stop truth; precision_score separately grades how close Ghost's open/low/high levels were.",
    }
