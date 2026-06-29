"""Super Ghost Adaptive Range Calibration Brain (PR #103).

PR #102 made Ghost measure price precision. This module closes the next loop:
turn repeated precision mistakes into bounded future target/stop/range
adjustments. It never bypasses risk/coverage gates and never claims certainty.

Flow:
    precision events -> precision profile -> range calibration profile
    -> build_super_ghost risk_plan raw/calibrated ranges

Nothing here trades. It only improves prediction measurement and explanation for
a human operator.
"""
from __future__ import annotations

import json
import logging
import math
import time
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("ghost.super_ghost_range_calibration")

HORIZONS = (1, 5, 20)
MIN_CALIBRATION_SAMPLES = 5
MIN_DIRECTION_WIN_RATE = 0.50
TARGET_MIN_MULT = 0.80
TARGET_MAX_MULT = 1.25
STOP_MIN_MULT = 0.80
STOP_MAX_MULT = 1.20
MIN_RANGE_WIDTH_PCT = 0.015
MAX_RANGE_WIDTH_PCT = 0.12


def _now() -> int:
    return int(time.time())


def _f(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        out = float(v)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _round(v: Any, nd: int = 4) -> Optional[float]:
    fv = _f(v)
    return round(fv, nd) if fv is not None else None


def _jsonb(v: Any) -> str:
    return json.dumps(v, default=str)


def _coerce_json(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return v


def ensure_range_calibration_tables(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS super_ghost_range_calibration_profiles (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            horizon_days INT NOT NULL,
            direction VARCHAR(10) NOT NULL,
            sample_count INT NOT NULL,
            avg_precision_score FLOAT,
            direction_win_rate FLOAT,
            target_move_multiplier FLOAT,
            stop_distance_multiplier FLOAT,
            range_width_pct FLOAT,
            calibration_status VARCHAR(32),
            primary_reason TEXT,
            updated_at BIGINT NOT NULL,
            payload_json JSONB,
            UNIQUE (symbol, horizon_days, direction)
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_sg_range_cal_symbol ON super_ghost_range_calibration_profiles (symbol, horizon_days, direction)"
    )


def derive_calibration_from_precision_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Create a bounded calibration profile from a precision profile.

    The profile is intentionally conservative: small samples only produce a
    visible cold-start block, and weak direction profiles dampen/avoid changes.
    """
    p = dict(profile or {})
    n = int(p.get("sample_count") or 0)
    avg_precision = _f(p.get("avg_precision_score"))
    win_rate = _f(p.get("direction_win_rate"))
    target_err = _f(p.get("avg_abs_target_error_pct")) or 0.0
    stop_err = _f(p.get("avg_abs_stop_error_pct")) or 0.0
    primary = str(p.get("primary_mistake_type") or "unknown")
    symbol = str(p.get("symbol") or "").upper()
    direction = str(p.get("direction") or "HOLD").upper()
    horizon = int(p.get("horizon_days") or 5)

    available = n >= MIN_CALIBRATION_SAMPLES
    target_mult = 1.0
    stop_mult = 1.0
    status = "cold_start" if not available else "learning"
    reason = "Range calibration is collecting enough resolved precision samples."

    if available:
        if win_rate is not None and win_rate < MIN_DIRECTION_WIN_RATE:
            status = "direction_unstable"
            reason = "Direction profile is not stable enough; keep raw target/stop ranges until directional edge improves."
        elif primary == "target_too_low":
            target_mult = _clamp(1.0 + (target_err / 100.0) * 0.35, 1.0, TARGET_MAX_MULT)
            status = "widen_target"
            reason = "Repeated target/high estimates have been too conservative; widen future target move modestly."
        elif primary in ("target_too_high", "no_follow_through"):
            target_mult = _clamp(1.0 - (target_err / 100.0) * 0.35, TARGET_MIN_MULT, 1.0)
            status = "tighten_target"
            reason = "Repeated target/high estimates have been too aggressive; tighten future target move."
        elif primary == "stop_too_wide":
            stop_mult = _clamp(1.0 - (stop_err / 100.0) * 0.35, STOP_MIN_MULT, 1.0)
            status = "tighten_stop"
            reason = "Actual lows have stayed above Ghost's stop; tighten downside range/risk estimate modestly."
        elif primary in ("stop_too_tight", "wrong_direction_or_stop_hit", "stop_too_tight_or_path_uncertain"):
            stop_mult = _clamp(1.0 + (stop_err / 100.0) * 0.35, 1.0, STOP_MAX_MULT)
            status = "widen_stop"
            reason = "Stop/invalid level has been too tight or path-uncertain; allow more volatility room."
        elif primary == "direction_right_low_precision":
            status = "widen_uncertainty_band"
            reason = "Direction has worked more than exact levels; widen displayed prediction bands while collecting more range evidence."
        elif avg_precision is not None and avg_precision >= 75 and win_rate is not None and win_rate >= 0.70:
            status = "stable"
            reason = "Precision and direction profile are stable; keep raw target/stop but publish tight confidence bands."
        else:
            reason = "No dominant range mistake yet; keep target/stop unchanged and continue measuring precision."

    # Lower precision creates wider visible confidence bands even when target/stop
    # multipliers stay neutral. A 60 score => ~4.2% band; very poor precision is
    # capped at 12% so the UI does not imply fake certainty.
    if avg_precision is None:
        range_width = 0.05
    else:
        range_width = _clamp(0.012 + ((100.0 - avg_precision) / 100.0) * 0.075, MIN_RANGE_WIDTH_PCT, MAX_RANGE_WIDTH_PCT)

    payload = {
        "available": available,
        "symbol": symbol,
        "horizon_days": horizon if horizon in HORIZONS else 5,
        "direction": direction,
        "sample_count": n,
        "avg_precision_score": round(avg_precision, 3) if avg_precision is not None else None,
        "direction_win_rate": round(win_rate, 4) if win_rate is not None else None,
        "target_move_multiplier": round(target_mult, 4),
        "stop_distance_multiplier": round(stop_mult, 4),
        "range_width_pct": round(range_width, 4),
        "calibration_status": status,
        "primary_mistake_type": primary,
        "primary_reason": reason,
        "source_precision_profile": p,
        "rules": {
            "min_samples": MIN_CALIBRATION_SAMPLES,
            "target_multiplier_bounds": [TARGET_MIN_MULT, TARGET_MAX_MULT],
            "stop_multiplier_bounds": [STOP_MIN_MULT, STOP_MAX_MULT],
            "range_width_bounds": [MIN_RANGE_WIDTH_PCT, MAX_RANGE_WIDTH_PCT],
        },
    }
    return payload


def _read_precision_profiles(cur, *, symbol: Optional[str], horizon: int, limit: int) -> List[Dict[str, Any]]:
    h = horizon if horizon in HORIZONS else 5
    params: List[Any] = [h]
    where = "horizon_days=%s"
    if symbol:
        where += " AND symbol=%s"
        params.append(symbol.upper())
    cur.execute(
        f"""
        SELECT symbol, horizon_days, direction, sample_count, avg_precision_score,
               direction_win_rate, avg_abs_target_error_pct, avg_abs_stop_error_pct,
               primary_mistake_type, primary_lesson, precision_status, updated_at, payload_json
        FROM super_ghost_precision_profiles
        WHERE {where}
        ORDER BY updated_at DESC, sample_count DESC
        LIMIT %s
        """,
        params + [max(1, min(5000, int(limit)))],
    )
    out: List[Dict[str, Any]] = []
    for r in cur.fetchall():
        payload = _coerce_json(r[12]) or {}
        d = dict(payload)
        d.update({
            "symbol": r[0],
            "horizon_days": r[1],
            "direction": r[2],
            "sample_count": r[3],
            "avg_precision_score": r[4],
            "direction_win_rate": r[5],
            "avg_abs_target_error_pct": r[6],
            "avg_abs_stop_error_pct": r[7],
            "primary_mistake_type": r[8],
            "primary_lesson": r[9],
            "precision_status": r[10],
            "updated_at": r[11],
        })
        out.append(d)
    return out


def rebuild_range_calibration(*, symbol: Optional[str] = None, horizon: int = 5, limit: int = 1000) -> Dict[str, Any]:
    h = horizon if horizon in HORIZONS else 5
    try:
        from core.db import db_conn
        from core.super_ghost_precision import ensure_precision_tables

        with db_conn() as conn:
            cur = conn.cursor()
            ensure_precision_tables(cur)
            ensure_range_calibration_tables(cur)
            precision_profiles = _read_precision_profiles(cur, symbol=symbol, horizon=h, limit=limit)
            now = _now()
            profiles = []
            for pp in precision_profiles:
                cal = derive_calibration_from_precision_profile(pp)
                cur.execute(
                    """
                    INSERT INTO super_ghost_range_calibration_profiles (
                        symbol, horizon_days, direction, sample_count, avg_precision_score,
                        direction_win_rate, target_move_multiplier, stop_distance_multiplier,
                        range_width_pct, calibration_status, primary_reason, updated_at, payload_json
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (symbol, horizon_days, direction) DO UPDATE SET
                        sample_count=EXCLUDED.sample_count,
                        avg_precision_score=EXCLUDED.avg_precision_score,
                        direction_win_rate=EXCLUDED.direction_win_rate,
                        target_move_multiplier=EXCLUDED.target_move_multiplier,
                        stop_distance_multiplier=EXCLUDED.stop_distance_multiplier,
                        range_width_pct=EXCLUDED.range_width_pct,
                        calibration_status=EXCLUDED.calibration_status,
                        primary_reason=EXCLUDED.primary_reason,
                        updated_at=EXCLUDED.updated_at,
                        payload_json=EXCLUDED.payload_json
                    """,
                    (
                        cal["symbol"], cal["horizon_days"], cal["direction"], cal["sample_count"], cal.get("avg_precision_score"),
                        cal.get("direction_win_rate"), cal.get("target_move_multiplier"), cal.get("stop_distance_multiplier"),
                        cal.get("range_width_pct"), cal.get("calibration_status"), cal.get("primary_reason"), now, _jsonb(cal),
                    ),
                )
                profiles.append(cal)
        return {"ok": True, "symbol": (symbol or "ALL").upper(), "horizon_days": h, "profiles_rebuilt": len(profiles), "profiles": profiles[:20]}
    except Exception as exc:
        LOGGER.warning("rebuild_range_calibration: %s", str(exc)[:180])
        return {"ok": False, "error": str(exc)[:180], "profiles_rebuilt": 0, "profiles": []}


def get_range_calibration_profile(symbol: str, direction: str, *, horizon: int = 5) -> Dict[str, Any]:
    sym = (symbol or "").upper()
    d = (direction or "HOLD").upper()
    h = horizon if horizon in HORIZONS else 5
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_range_calibration_tables(cur)
            cur.execute(
                """
                SELECT sample_count, avg_precision_score, direction_win_rate,
                       target_move_multiplier, stop_distance_multiplier, range_width_pct,
                       calibration_status, primary_reason, updated_at, payload_json
                FROM super_ghost_range_calibration_profiles
                WHERE symbol=%s AND horizon_days=%s AND direction=%s
                """,
                (sym, h, d),
            )
            r = cur.fetchone()
    except Exception as exc:
        return {"available": False, "symbol": sym, "direction": d, "horizon_days": h, "calibration_status": "unavailable", "error": str(exc)[:120]}
    if not r:
        return {"available": False, "symbol": sym, "direction": d, "horizon_days": h, "sample_count": 0, "calibration_status": "cold_start", "primary_reason": "No range calibration profile yet."}
    payload = _coerce_json(r[9]) or {}
    payload.update({
        "available": bool((r[0] or 0) >= MIN_CALIBRATION_SAMPLES),
        "symbol": sym,
        "direction": d,
        "horizon_days": h,
        "sample_count": int(r[0] or 0),
        "avg_precision_score": r[1],
        "direction_win_rate": r[2],
        "target_move_multiplier": r[3],
        "stop_distance_multiplier": r[4],
        "range_width_pct": r[5],
        "calibration_status": r[6],
        "primary_reason": r[7],
        "updated_at": r[8],
    })
    return payload


def apply_range_calibration_to_report(report: Dict[str, Any], profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = dict(report or {})
    pred = dict(out.get("prediction") or {})
    risk = dict(out.get("risk_plan") or {})
    profile = dict(profile or {})
    direction = str(pred.get("direction") or profile.get("direction") or "HOLD").upper()

    entry = _f(risk.get("entry"))
    target = _f(risk.get("target_price"))
    stop = _f(risk.get("stop_loss"))
    raw = {
        "entry": _round(entry),
        "target_price": _round(target),
        "stop_loss": _round(stop),
        "risk_reward_ratio": _round(risk.get("risk_reward_ratio")),
    }

    if not profile or not profile.get("available"):
        out["risk_plan"] = risk
        out["range_calibration"] = {
            "available": False,
            "applied": False,
            "status": profile.get("calibration_status") or "cold_start",
            "sample_count": int(profile.get("sample_count") or 0),
            "raw": raw,
            "message": "Range calibration is collecting precision outcomes; raw risk plan unchanged.",
        }
        return out

    if entry is None or target is None or stop is None or entry <= 0:
        out["risk_plan"] = risk
        out["range_calibration"] = {
            "available": True,
            "applied": False,
            "status": "invalid_raw_plan",
            "sample_count": int(profile.get("sample_count") or 0),
            "raw": raw,
            "message": "Raw entry/target/stop was incomplete; calibration profile recorded but not applied.",
        }
        return out

    if direction != "UP" or not (target > entry and stop < entry):
        out["risk_plan"] = risk
        out["range_calibration"] = {
            "available": True,
            "applied": False,
            "status": "direction_not_supported_for_price_adjustment",
            "sample_count": int(profile.get("sample_count") or 0),
            "raw": raw,
            "message": "Current Super Ghost target/stop plan is long-oriented; DOWN/HOLD calls publish raw plan until short-side range model is validated.",
        }
        return out

    target_mult = _f(profile.get("target_move_multiplier")) or 1.0
    stop_mult = _f(profile.get("stop_distance_multiplier")) or 1.0
    width_pct = _f(profile.get("range_width_pct")) or 0.05
    target_mult = _clamp(target_mult, TARGET_MIN_MULT, TARGET_MAX_MULT)
    stop_mult = _clamp(stop_mult, STOP_MIN_MULT, STOP_MAX_MULT)
    width_pct = _clamp(width_pct, MIN_RANGE_WIDTH_PCT, MAX_RANGE_WIDTH_PCT)

    target_move = target - entry
    stop_distance = entry - stop
    calibrated_target = entry + target_move * target_mult
    calibrated_stop = entry - stop_distance * stop_mult
    reward = calibrated_target - entry
    risk_amt = entry - calibrated_stop
    rr = reward / max(risk_amt, 0.0001)

    high_low = calibrated_target * (1.0 - width_pct)
    high_high = calibrated_target * (1.0 + width_pct)
    low_low = calibrated_stop * (1.0 - width_pct)
    low_high = calibrated_stop * (1.0 + width_pct)
    close_mid = entry + (calibrated_target - entry) * 0.55
    close_low = close_mid * (1.0 - width_pct)
    close_high = close_mid * (1.0 + width_pct)
    bull_case = calibrated_target * (1.0 + width_pct * 2.0)
    bear_case = calibrated_stop * (1.0 - width_pct * 2.0)

    risk.update({
        "target_price_raw": _round(target),
        "stop_loss_raw": _round(stop),
        "risk_reward_ratio_raw": _round(raw.get("risk_reward_ratio")),
        "target_price_calibrated": _round(calibrated_target),
        "stop_loss_calibrated": _round(calibrated_stop),
        "target_price": _round(calibrated_target),
        "stop_loss": _round(calibrated_stop),
        "risk_reward_ratio": _round(rr),
        "expected_high_range": [_round(high_low), _round(high_high)],
        "expected_low_range": [_round(low_low), _round(low_high)],
        "expected_close_range": [_round(close_low), _round(close_high)],
        "bull_case_price": _round(bull_case),
        "bear_case_price": _round(bear_case),
        "invalidation_level": _round(calibrated_stop),
        "range_calibrated": True,
    })
    out["risk_plan"] = risk
    out["range_calibration"] = {
        "available": True,
        "applied": True,
        "status": profile.get("calibration_status") or "learning",
        "sample_count": int(profile.get("sample_count") or 0),
        "avg_precision_score": profile.get("avg_precision_score"),
        "direction_win_rate": profile.get("direction_win_rate"),
        "primary_mistake_type": profile.get("primary_mistake_type"),
        "reason": profile.get("primary_reason"),
        "target_move_multiplier": round(target_mult, 4),
        "stop_distance_multiplier": round(stop_mult, 4),
        "range_width_pct": round(width_pct, 4),
        "raw": raw,
        "calibrated": {
            "target_price": _round(calibrated_target),
            "stop_loss": _round(calibrated_stop),
            "risk_reward_ratio": _round(rr),
            "expected_high_range": risk["expected_high_range"],
            "expected_low_range": risk["expected_low_range"],
            "expected_close_range": risk["expected_close_range"],
            "bull_case_price": risk["bull_case_price"],
            "bear_case_price": risk["bear_case_price"],
            "invalidation_level": risk["invalidation_level"],
        },
        "message": "Range calibration is bounded, evidence-based, and derived from resolved precision outcomes.",
    }
    return out


def range_calibration_summary(*, symbol: Optional[str] = None, horizon: int = 5, limit: int = 20) -> Dict[str, Any]:
    h = horizon if horizon in HORIZONS else 5
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_range_calibration_tables(cur)
            params: List[Any] = [h]
            where = "horizon_days=%s"
            if symbol:
                where += " AND symbol=%s"
                params.append(symbol.upper())
            cur.execute(
                f"""
                SELECT symbol, horizon_days, direction, sample_count, avg_precision_score,
                       direction_win_rate, target_move_multiplier, stop_distance_multiplier,
                       range_width_pct, calibration_status, primary_reason, updated_at, payload_json
                FROM super_ghost_range_calibration_profiles
                WHERE {where}
                ORDER BY updated_at DESC, sample_count DESC
                LIMIT %s
                """,
                params + [max(1, min(100, int(limit)))],
            )
            profiles = []
            for r in cur.fetchall():
                payload = _coerce_json(r[12]) or {}
                profiles.append({
                    "symbol": r[0], "horizon_days": r[1], "direction": r[2], "sample_count": r[3],
                    "avg_precision_score": r[4], "direction_win_rate": r[5],
                    "target_move_multiplier": r[6], "stop_distance_multiplier": r[7],
                    "range_width_pct": r[8], "calibration_status": r[9], "primary_reason": r[10],
                    "updated_at": r[11], "primary_mistake_type": payload.get("primary_mistake_type"),
                    "available": bool((r[3] or 0) >= MIN_CALIBRATION_SAMPLES),
                })
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:180], "profiles": []}
    return {
        "ok": True,
        "enabled": True,
        "symbol": (symbol or "ALL").upper(),
        "horizon_days": h,
        "min_samples": MIN_CALIBRATION_SAMPLES,
        "profiles": profiles,
        "primary_profile": profiles[0] if profiles else None,
        "note": "Range calibration turns repeated precision mistakes into bounded target/stop/range adjustments; it never bypasses risk gates.",
    }
