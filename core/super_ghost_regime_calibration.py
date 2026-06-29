"""Super Ghost Regime-Specific Calibration Brain (PR #104).

PR #103 created one adaptive range calibration profile per symbol/direction.
PR #104 adds market-regime and setup-style slices so Ghost can learn that a
range adjustment that works in a squeeze/risk-on tape may be wrong in a
risk-off/high-volatility tape.

Examples:
- risk_on + squeeze_momentum may support wider upside targets.
- risk_off_high_volatility may require wider uncertainty bands and/or tighter
  promotion discipline.
- earnings_guidance setups can behave differently from pure technical setups.

This is still prediction intelligence only: no trading, no guarantees, no
promotion without evidence.
"""
from __future__ import annotations

import json
import logging
import math
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core.super_ghost_precision import profile_from_precision_events, score_ledger_row
from core.super_ghost_range_calibration import (
    MAX_RANGE_WIDTH_PCT,
    MIN_RANGE_WIDTH_PCT,
    derive_calibration_from_precision_profile,
)

LOGGER = logging.getLogger("ghost.super_ghost_regime_calibration")
HORIZONS = (1, 5, 20)
MIN_REGIME_SAMPLES = 5


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


def _round(v: Any, nd: int = 4) -> Optional[float]:
    fv = _f(v)
    return round(fv, nd) if fv is not None else None


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


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


def ensure_regime_calibration_tables(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS super_ghost_regime_calibration_profiles (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            horizon_days INT NOT NULL,
            direction VARCHAR(10) NOT NULL,
            regime_bucket VARCHAR(64) NOT NULL,
            setup_bucket VARCHAR(64) NOT NULL,
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
            UNIQUE (symbol, horizon_days, direction, regime_bucket, setup_bucket)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sg_regime_cal_lookup
        ON super_ghost_regime_calibration_profiles (symbol, horizon_days, direction, regime_bucket, setup_bucket)
        """
    )


def _market_regime_json(row_or_report: Dict[str, Any]) -> Dict[str, Any]:
    mr = row_or_report.get("market_regime") or row_or_report.get("market_regime_json") or {}
    mr = _coerce_json(mr) or {}
    return mr if isinstance(mr, dict) else {}


def regime_bucket(row_or_report: Dict[str, Any]) -> str:
    mr = _market_regime_json(row_or_report)
    label = str(row_or_report.get("regime_label") or mr.get("label") or "").strip().lower()
    state = str(row_or_report.get("regime_risk_state") or mr.get("risk_state") or "").strip().lower()
    high_vol = bool(mr.get("high_volatility")) or "high_vol" in label or "volatility" in label
    if high_vol and state == "risk_off":
        return "risk_off_high_volatility"
    if high_vol and not state:
        return "high_volatility"
    if label in {"calm_risk_on", "risk_on", "risk_off", "risk_off_high_volatility", "mixed"}:
        return label
    if state in {"risk_on", "risk_off", "neutral"}:
        return state
    return "unknown_regime"


def _checklist(row_or_report: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = row_or_report.get("checklist") or row_or_report.get("checklist_json") or []
    raw = _coerce_json(raw) or []
    return raw if isinstance(raw, list) else []


def setup_bucket(row_or_report: Dict[str, Any]) -> str:
    """Classify setup style from the scored checklist, not user opinion."""
    rows = _checklist(row_or_report)
    by_key = {str(x.get("key") or ""): x for x in rows if isinstance(x, dict)}

    def score(key: str) -> float:
        try:
            return float((by_key.get(key) or {}).get("score") or 0.0)
        except Exception:
            return 0.0

    def available(key: str) -> bool:
        return bool((by_key.get(key) or {}).get("available"))

    news = max(score("news_catalysts"), score("guidance"))
    earnings = max(score("eps"), score("revenue_growth"), score("guidance"))
    squeeze = max(score("rvol"), score("moving_averages"), score("perf_30d"), score("relative_strength"))
    if news >= 0.8 or (available("news_catalysts") and news > 0.25):
        return "news_catalyst"
    if earnings >= 0.8 or (available("eps") and available("guidance") and earnings > 0.25):
        return "earnings_guidance"
    if squeeze >= 0.8:
        return "squeeze_momentum"
    if score("avg_volume") < -0.4:
        return "thin_liquidity"
    if available("analyst_ratings") and score("analyst_ratings") >= 0.8:
        return "analyst_revision"
    return "general"


def _widen_for_regime(cal: Dict[str, Any], regime: str, setup: str) -> Dict[str, Any]:
    out = dict(cal)
    width = _f(out.get("range_width_pct")) or 0.05
    # High-vol/risk-off regimes are whippier; do not fake tight ranges.
    if "high_volatility" in regime:
        width *= 1.25
    elif regime == "risk_off":
        width *= 1.12
    elif regime == "calm_risk_on":
        width *= 0.90
    if setup in {"squeeze_momentum", "news_catalyst", "earnings_guidance"}:
        width *= 1.08
    out["range_width_pct"] = round(_clamp(width, MIN_RANGE_WIDTH_PCT, MAX_RANGE_WIDTH_PCT), 4)
    return out


def derive_regime_calibration(events: Iterable[Dict[str, Any]], *, symbol: str, horizon: int, direction: str, regime: str, setup: str) -> Dict[str, Any]:
    rows = [dict(e) for e in events]
    profile = profile_from_precision_events(rows)
    profile.update({
        "symbol": symbol,
        "horizon_days": horizon,
        "direction": direction,
    })
    cal = derive_calibration_from_precision_profile(profile)
    cal = _widen_for_regime(cal, regime, setup)
    cal.update({
        "available": bool((cal.get("sample_count") or 0) >= MIN_REGIME_SAMPLES),
        "regime_bucket": regime,
        "setup_bucket": setup,
        "sample_count": int(cal.get("sample_count") or 0),
        "regime_reason": f"Calibration slice for {regime} / {setup}; falls back to broader profiles until enough samples exist.",
        "source": "regime_specific_precision_events",
    })
    if not cal["available"]:
        cal["calibration_status"] = "cold_start"
        cal["primary_reason"] = "Regime-specific calibration is collecting enough resolved samples for this market/setup slice."
    return cal


def _resolved_rows(cur, *, symbol: Optional[str], horizon: int, limit: int) -> List[Dict[str, Any]]:
    h = horizon if horizon in HORIZONS else 5
    price_col = f"price_{h}d"
    ret_col = f"return_{h}d_pct"
    correct_col = f"correct_{h}d"
    resolved_col = f"resolved_{h}d_at"
    cols = [
        "id", "symbol", "created_at", "reference_price", "direction", "action", "confidence",
        "accuracy_grade", "stop_loss", "target_price", "risk_reward", "max_favorable_pct", "max_adverse_pct",
        "hit_target", "hit_stop", "regime_label", "regime_risk_state", "market_regime_json", "checklist_json",
        price_col, ret_col, correct_col, resolved_col,
    ]
    where = f"{resolved_col} IS NOT NULL AND {price_col} IS NOT NULL"
    params: List[Any] = []
    if symbol:
        where += " AND symbol=%s"
        params.append(symbol.upper())
    cur.execute(
        f"SELECT {', '.join(cols)} FROM super_ghost_predictions WHERE {where} ORDER BY created_at DESC LIMIT %s",
        params + [max(1, min(5000, int(limit)))],
    )
    out = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        d[f"price_{h}d"] = d.pop(price_col)
        d[f"return_{h}d_pct"] = d.pop(ret_col)
        d[f"correct_{h}d"] = d.pop(correct_col)
        d[f"resolved_{h}d_at"] = d.pop(resolved_col)
        d["market_regime_json"] = _coerce_json(d.get("market_regime_json")) or {}
        d["checklist_json"] = _coerce_json(d.get("checklist_json")) or []
        out.append(d)
    return list(reversed(out))


def _group_keys(event: Dict[str, Any]) -> List[Tuple[str, int, str, str, str]]:
    sym = str(event.get("symbol") or "").upper()
    h = int(event.get("horizon_days") or 5)
    d = str(event.get("direction") or "HOLD").upper()
    rb = str(event.get("regime_bucket") or "unknown_regime")
    sb = str(event.get("setup_bucket") or "general")
    # Both symbol-specific and global fallbacks are persisted. Runtime lookup can
    # use the narrow slice first, then broader evidence if the narrow slice is cold.
    return [
        (sym, h, d, rb, sb),
        (sym, h, d, rb, "all"),
        ("ALL", h, d, rb, sb),
        ("ALL", h, d, rb, "all"),
    ]


def rebuild_regime_calibration(*, symbol: Optional[str] = None, horizon: int = 5, limit: int = 1000) -> Dict[str, Any]:
    h = horizon if horizon in HORIZONS else 5
    try:
        from core.db import db_conn
        from core.super_ghost_ledger import ensure_ledger_table

        with db_conn() as conn:
            cur = conn.cursor()
            ensure_ledger_table(cur)
            ensure_regime_calibration_tables(cur)
            rows = _resolved_rows(cur, symbol=symbol, horizon=h, limit=limit)
            grouped: Dict[Tuple[str, int, str, str, str], List[Dict[str, Any]]] = {}
            for row in rows:
                ev = score_ledger_row(row, horizon=h)
                ev["regime_bucket"] = regime_bucket(row)
                ev["setup_bucket"] = setup_bucket(row)
                # Copy top-level errors for profile builders and persistence.
                errs = ev.get("errors_pct") or {}
                ev.setdefault("target_error_pct", errs.get("target"))
                ev.setdefault("stop_error_pct", errs.get("stop"))
                for key in _group_keys(ev):
                    grouped.setdefault(key, []).append(ev)
            now = _now()
            profiles: List[Dict[str, Any]] = []
            for (sym, hh, direction, rb, sb), events in grouped.items():
                cal = derive_regime_calibration(events, symbol=sym, horizon=hh, direction=direction, regime=rb, setup=sb)
                cur.execute(
                    """
                    INSERT INTO super_ghost_regime_calibration_profiles (
                        symbol, horizon_days, direction, regime_bucket, setup_bucket,
                        sample_count, avg_precision_score, direction_win_rate,
                        target_move_multiplier, stop_distance_multiplier, range_width_pct,
                        calibration_status, primary_reason, updated_at, payload_json
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (symbol, horizon_days, direction, regime_bucket, setup_bucket) DO UPDATE SET
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
                        sym, hh, direction, rb, sb, int(cal.get("sample_count") or 0), cal.get("avg_precision_score"),
                        cal.get("direction_win_rate"), cal.get("target_move_multiplier"), cal.get("stop_distance_multiplier"),
                        cal.get("range_width_pct"), cal.get("calibration_status"), cal.get("primary_reason"), now, _jsonb(cal),
                    ),
                )
                profiles.append(cal)
        return {"ok": True, "symbol": (symbol or "ALL").upper(), "horizon_days": h, "profiles_rebuilt": len(profiles), "profiles": profiles[:20]}
    except Exception as exc:
        LOGGER.warning("rebuild_regime_calibration: %s", str(exc)[:180])
        return {"ok": False, "error": str(exc)[:180], "profiles_rebuilt": 0, "profiles": []}


def _fetch_profile(cur, *, symbol: str, horizon: int, direction: str, regime: str, setup: str) -> Optional[Dict[str, Any]]:
    cur.execute(
        """
        SELECT symbol, horizon_days, direction, regime_bucket, setup_bucket, sample_count,
               avg_precision_score, direction_win_rate, target_move_multiplier,
               stop_distance_multiplier, range_width_pct, calibration_status,
               primary_reason, updated_at, payload_json
        FROM super_ghost_regime_calibration_profiles
        WHERE symbol=%s AND horizon_days=%s AND direction=%s AND regime_bucket=%s AND setup_bucket=%s
        """,
        (symbol, horizon, direction, regime, setup),
    )
    r = cur.fetchone()
    if not r:
        return None
    payload = _coerce_json(r[14]) or {}
    payload.update({
        "symbol": r[0], "horizon_days": r[1], "direction": r[2], "regime_bucket": r[3], "setup_bucket": r[4],
        "sample_count": int(r[5] or 0), "avg_precision_score": r[6], "direction_win_rate": r[7],
        "target_move_multiplier": r[8], "stop_distance_multiplier": r[9], "range_width_pct": r[10],
        "calibration_status": r[11], "primary_reason": r[12], "updated_at": r[13],
        "available": bool((r[5] or 0) >= MIN_REGIME_SAMPLES),
    })
    return payload


def get_regime_calibration_profile(symbol: str, direction: str, report_or_regime: Optional[Dict[str, Any]] = None, *, horizon: int = 5) -> Dict[str, Any]:
    sym = (symbol or "").upper()
    d = (direction or "HOLD").upper()
    h = horizon if horizon in HORIZONS else 5
    ctx = report_or_regime or {}
    rb = regime_bucket(ctx)
    sb = setup_bucket(ctx)
    candidates = [
        (sym, rb, sb),
        (sym, rb, "all"),
        ("ALL", rb, sb),
        ("ALL", rb, "all"),
    ]
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_regime_calibration_tables(cur)
            seen = set()
            for csym, crb, csb in candidates:
                key = (csym, crb, csb)
                if key in seen:
                    continue
                seen.add(key)
                prof = _fetch_profile(cur, symbol=csym, horizon=h, direction=d, regime=crb, setup=csb)
                if prof and prof.get("available"):
                    prof["lookup"] = {"requested_symbol": sym, "requested_regime": rb, "requested_setup": sb, "matched": key}
                    return prof
            # Return the narrow cold profile if it exists; otherwise explicit cold-start.
            prof = _fetch_profile(cur, symbol=sym, horizon=h, direction=d, regime=rb, setup=sb)
            if prof:
                prof["lookup"] = {"requested_symbol": sym, "requested_regime": rb, "requested_setup": sb, "matched": (sym, rb, sb)}
                return prof
    except Exception as exc:
        return {"available": False, "symbol": sym, "direction": d, "horizon_days": h, "regime_bucket": rb, "setup_bucket": sb, "calibration_status": "unavailable", "error": str(exc)[:120]}
    return {
        "available": False,
        "symbol": sym,
        "direction": d,
        "horizon_days": h,
        "regime_bucket": rb,
        "setup_bucket": sb,
        "sample_count": 0,
        "calibration_status": "cold_start",
        "primary_reason": "No regime-specific calibration profile yet for this market/setup slice.",
    }


def apply_regime_calibration_to_report(report: Dict[str, Any], profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = dict(report or {})
    risk = dict(out.get("risk_plan") or {})
    pred = dict(out.get("prediction") or {})
    profile = dict(profile or {})
    raw_target = _f(risk.get("target_price_raw")) or _f(risk.get("target_price"))
    raw_stop = _f(risk.get("stop_loss_raw")) or _f(risk.get("stop_loss"))
    entry = _f(risk.get("entry"))
    direction = str(pred.get("direction") or profile.get("direction") or "HOLD").upper()

    if not profile or not profile.get("available"):
        out["regime_calibration"] = {
            "available": False,
            "applied": False,
            "status": profile.get("calibration_status") or "cold_start",
            "sample_count": int(profile.get("sample_count") or 0),
            "regime_bucket": profile.get("regime_bucket"),
            "setup_bucket": profile.get("setup_bucket"),
            "message": "Regime-specific calibration is collecting resolved precision samples; existing risk plan unchanged.",
        }
        return out
    if direction != "UP" or entry is None or raw_target is None or raw_stop is None or not (raw_target > entry and raw_stop < entry):
        out["regime_calibration"] = {
            "available": True,
            "applied": False,
            "status": "unsupported_raw_plan",
            "sample_count": int(profile.get("sample_count") or 0),
            "regime_bucket": profile.get("regime_bucket"),
            "setup_bucket": profile.get("setup_bucket"),
            "message": "Regime-specific price adjustment currently applies only to validated long-oriented UP risk plans.",
        }
        return out

    target_mult = _clamp(_f(profile.get("target_move_multiplier")) or 1.0, 0.80, 1.25)
    stop_mult = _clamp(_f(profile.get("stop_distance_multiplier")) or 1.0, 0.80, 1.20)
    width = _clamp(_f(profile.get("range_width_pct")) or 0.05, MIN_RANGE_WIDTH_PCT, MAX_RANGE_WIDTH_PCT)
    target = entry + (raw_target - entry) * target_mult
    stop = entry - (entry - raw_stop) * stop_mult
    rr = (target - entry) / max(entry - stop, 0.0001)
    high_range = [target * (1.0 - width), target * (1.0 + width)]
    low_range = [stop * (1.0 - width), stop * (1.0 + width)]
    close_mid = entry + (target - entry) * 0.55
    close_range = [close_mid * (1.0 - width), close_mid * (1.0 + width)]

    risk.update({
        "target_price_raw": _round(raw_target),
        "stop_loss_raw": _round(raw_stop),
        "target_price_regime_calibrated": _round(target),
        "stop_loss_regime_calibrated": _round(stop),
        "target_price": _round(target),
        "stop_loss": _round(stop),
        "risk_reward_ratio": _round(rr),
        "expected_high_range": [_round(high_range[0]), _round(high_range[1])],
        "expected_low_range": [_round(low_range[0]), _round(low_range[1])],
        "expected_close_range": [_round(close_range[0]), _round(close_range[1])],
        "bull_case_price": _round(target * (1.0 + width * 2.0)),
        "bear_case_price": _round(stop * (1.0 - width * 2.0)),
        "invalidation_level": _round(stop),
        "range_calibrated": True,
        "regime_calibrated": True,
    })
    out["risk_plan"] = risk
    out["regime_calibration"] = {
        "available": True,
        "applied": True,
        "status": profile.get("calibration_status") or "learning",
        "sample_count": int(profile.get("sample_count") or 0),
        "regime_bucket": profile.get("regime_bucket"),
        "setup_bucket": profile.get("setup_bucket"),
        "matched_profile": (profile.get("lookup") or {}).get("matched"),
        "target_move_multiplier": round(target_mult, 4),
        "stop_distance_multiplier": round(stop_mult, 4),
        "range_width_pct": round(width, 4),
        "primary_reason": profile.get("primary_reason"),
        "message": "Regime-specific calibration overrode the broad range profile because this market/setup slice has enough resolved evidence.",
    }
    return out


def regime_calibration_summary(*, symbol: Optional[str] = None, horizon: int = 5, limit: int = 20) -> Dict[str, Any]:
    h = horizon if horizon in HORIZONS else 5
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_regime_calibration_tables(cur)
            params: List[Any] = [h]
            where = "horizon_days=%s"
            if symbol:
                where += " AND symbol=%s"
                params.append(symbol.upper())
            cur.execute(
                f"""
                SELECT symbol, horizon_days, direction, regime_bucket, setup_bucket,
                       sample_count, avg_precision_score, direction_win_rate,
                       target_move_multiplier, stop_distance_multiplier, range_width_pct,
                       calibration_status, primary_reason, updated_at, payload_json
                FROM super_ghost_regime_calibration_profiles
                WHERE {where}
                ORDER BY updated_at DESC, sample_count DESC
                LIMIT %s
                """,
                params + [max(1, min(100, int(limit)))],
            )
            profiles = []
            for r in cur.fetchall():
                payload = _coerce_json(r[14]) or {}
                profiles.append({
                    "symbol": r[0], "horizon_days": r[1], "direction": r[2], "regime_bucket": r[3], "setup_bucket": r[4],
                    "sample_count": r[5], "avg_precision_score": r[6], "direction_win_rate": r[7],
                    "target_move_multiplier": r[8], "stop_distance_multiplier": r[9], "range_width_pct": r[10],
                    "calibration_status": r[11], "primary_reason": r[12], "updated_at": r[13],
                    "primary_mistake_type": payload.get("primary_mistake_type"), "available": bool((r[5] or 0) >= MIN_REGIME_SAMPLES),
                })
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:180], "profiles": []}
    return {
        "ok": True,
        "enabled": True,
        "symbol": (symbol or "ALL").upper(),
        "horizon_days": h,
        "min_samples": MIN_REGIME_SAMPLES,
        "profiles": profiles,
        "primary_profile": profiles[0] if profiles else None,
        "note": "Regime calibration learns range behavior by market regime and setup style; it falls back until enough evidence exists.",
    }
