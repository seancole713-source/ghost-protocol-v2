"""Super Ghost Precision Brain (PR #102).

This is the missing layer between "Ghost was directionally right" and "Ghost's
prices were actually close." It scores resolved Truth Ledger rows for price
precision, persists row-level postmortems, builds per-symbol precision profiles,
and exposes summary APIs for the console/top-pick gate.
"""
from __future__ import annotations

import json
import logging
import math
import time
from typing import Any, Dict, Iterable, List, Optional

from core.ghost_precision import PRICE_PRECISION_TARGET, score_trade_precision

LOGGER = logging.getLogger("ghost.super_ghost_precision")
HORIZONS = (1, 5, 20)
MIN_PRECISION_SAMPLES = 5


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


def ensure_precision_tables(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS super_ghost_precision_events (
            id SERIAL PRIMARY KEY,
            ledger_id INT NOT NULL,
            symbol VARCHAR(20) NOT NULL,
            horizon_days INT NOT NULL,
            created_at BIGINT,
            evaluated_at BIGINT NOT NULL,
            direction VARCHAR(10),
            direction_result VARCHAR(16),
            direction_correct BOOLEAN,
            target_stop_result VARCHAR(16),
            precision_score FLOAT,
            precision_grade VARCHAR(4),
            overall_score FLOAT,
            target_error_pct FLOAT,
            stop_error_pct FLOAT,
            open_error_pct FLOAT,
            low_error_pct FLOAT,
            high_error_pct FLOAT,
            close_error_pct FLOAT,
            mistake_type VARCHAR(64),
            lesson TEXT,
            payload_json JSONB,
            UNIQUE (ledger_id, horizon_days)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS super_ghost_precision_profiles (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            horizon_days INT NOT NULL,
            direction VARCHAR(10) NOT NULL,
            sample_count INT NOT NULL,
            avg_precision_score FLOAT,
            avg_overall_score FLOAT,
            direction_win_rate FLOAT,
            precise_rate FLOAT,
            poor_precision_rate FLOAT,
            avg_abs_target_error_pct FLOAT,
            avg_abs_stop_error_pct FLOAT,
            primary_mistake_type VARCHAR(64),
            primary_lesson TEXT,
            precision_status VARCHAR(32),
            updated_at BIGINT NOT NULL,
            payload_json JSONB,
            UNIQUE (symbol, horizon_days, direction)
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_sg_precision_events_symbol ON super_ghost_precision_events (symbol, horizon_days, evaluated_at DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_sg_precision_profiles_symbol ON super_ghost_precision_profiles (symbol, horizon_days, direction)"
    )


def _actual_extremes_from_row(row: Dict[str, Any]) -> Dict[str, Optional[float]]:
    ref = _f(row.get("reference_price"))
    fav = _f(row.get("max_favorable_pct"))
    adv = _f(row.get("max_adverse_pct"))
    if ref is None or ref <= 0:
        return {"high": None, "low": None}
    high = ref * (1.0 + fav / 100.0) if fav is not None else None
    low = ref * (1.0 + adv / 100.0) if adv is not None else None
    return {"high": high, "low": low}


def score_ledger_row(row: Dict[str, Any], *, horizon: int = 5) -> Dict[str, Any]:
    h = horizon if horizon in HORIZONS else 5
    direction = str(row.get("direction") or "HOLD").upper()
    close = _f(row.get(f"price_{h}d"))
    correct = row.get(f"correct_{h}d")
    correct_bool = bool(correct) if correct is not None else None
    extremes = _actual_extremes_from_row(row)
    # If high/low extremes are absent but close resolved, use close as a weak
    # fallback for both so the precision brain can still generate a cautious
    # postmortem instead of pretending no evidence exists.
    live_high = extremes["high"] if extremes["high"] is not None else close
    live_low = extremes["low"] if extremes["low"] is not None else close
    scored = score_trade_precision(
        direction=direction,
        entry=row.get("reference_price"),
        target=row.get("target_price"),
        stop=row.get("stop_loss"),
        live_open=None,
        live_low=live_low,
        live_high=live_high,
        live_close=close,
    )
    if correct_bool is True:
        direction_result = "WIN"
    elif correct_bool is False:
        direction_result = "LOSS"
    else:
        direction_result = "NEUTRAL"
    scored["direction_result"] = direction_result
    scored["direction_correct"] = correct_bool
    if correct_bool is False and scored.get("mistake_type") in ("no_follow_through", "target_too_high", "awaiting_truth"):
        scored["mistake_type"] = "wrong_direction"
        scored["lesson"] = "Ghost's direction was wrong at the resolved horizon; reduce confidence for similar evidence until the profile improves."
    elif (
        correct_bool is True
        and scored.get("precision_score") is not None
        and scored["precision_score"] < PRICE_PRECISION_TARGET
        and scored.get("mistake_type") in ("precise_direction_win", "no_follow_through", "uncategorized")
    ):
        scored["mistake_type"] = "direction_right_low_precision"
        scored["lesson"] = "Ghost got direction right, but price levels were not close enough; train range/high/low calibration separately from direction."
    scored["ledger_id"] = int(row.get("id") or row.get("ledger_id") or 0)
    scored["symbol"] = str(row.get("symbol") or "").upper()
    scored["horizon_days"] = h
    scored["created_at"] = row.get("created_at")
    scored["resolved_price"] = close
    scored["resolved_return_pct"] = _f(row.get(f"return_{h}d_pct"))
    return scored


def profile_from_precision_events(events: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows = [dict(e) for e in events]
    rows = [r for r in rows if r.get("precision_score") is not None]
    n = len(rows)
    if n <= 0:
        return {"sample_count": 0, "available": False, "precision_status": "cold_start"}
    scores = [_f(r.get("precision_score")) for r in rows]
    scores = [x for x in scores if x is not None]
    overall_scores = [_f(r.get("overall_score")) for r in rows]
    overall_scores = [x for x in overall_scores if x is not None]
    wins = [r for r in rows if r.get("direction_result") == "WIN" or r.get("direction_correct") is True]
    precise = [r for r in rows if (_f(r.get("precision_score")) or 0) >= PRICE_PRECISION_TARGET]
    poor = [r for r in rows if (_f(r.get("precision_score")) or 0) < 45]
    target_errs = [abs(_f(r.get("target_error_pct")) or 0.0) for r in rows if _f(r.get("target_error_pct")) is not None]
    stop_errs = [abs(_f(r.get("stop_error_pct")) or 0.0) for r in rows if _f(r.get("stop_error_pct")) is not None]
    counts: Dict[str, int] = {}
    for r in rows:
        mt = str(r.get("mistake_type") or "unknown")
        counts[mt] = counts.get(mt, 0) + 1
    primary = max(counts.items(), key=lambda kv: kv[1])[0] if counts else "unknown"
    avg_score = sum(scores) / len(scores) if scores else None
    win_rate = len(wins) / n if n else None
    precise_rate = len(precise) / n if n else None
    poor_rate = len(poor) / n if n else None
    if n < MIN_PRECISION_SAMPLES:
        status = "cold_start"
    elif avg_score is not None and avg_score >= 75 and win_rate is not None and win_rate >= 0.70:
        status = "precision_supportive"
    elif avg_score is not None and avg_score < PRICE_PRECISION_TARGET:
        status = "range_calibration_needed"
    else:
        status = "learning"
    lessons = {
        "target_too_low": "Targets/high estimates are too conservative; widen only when repeated with high-quality evidence.",
        "target_too_high": "Targets/high estimates are too aggressive; tighten unless catalysts/volume improve.",
        "stop_too_wide": "Stops/lows are too loose; improve downside range calibration and risk efficiency.",
        "direction_right_low_precision": "Direction works more often than exact levels; train price-range precision separately.",
        "wrong_direction": "Direction failed; reduce confidence and identify misleading features.",
    }
    return {
        "available": n >= MIN_PRECISION_SAMPLES,
        "sample_count": n,
        "avg_precision_score": round(avg_score, 3) if avg_score is not None else None,
        "avg_overall_score": round(sum(overall_scores) / len(overall_scores), 3) if overall_scores else None,
        "direction_win_rate": round(win_rate, 4) if win_rate is not None else None,
        "precise_rate": round(precise_rate, 4) if precise_rate is not None else None,
        "poor_precision_rate": round(poor_rate, 4) if poor_rate is not None else None,
        "avg_abs_target_error_pct": round(sum(target_errs) / len(target_errs), 3) if target_errs else None,
        "avg_abs_stop_error_pct": round(sum(stop_errs) / len(stop_errs), 3) if stop_errs else None,
        "primary_mistake_type": primary,
        "primary_lesson": lessons.get(primary, "No dominant precision mistake pattern yet; keep collecting resolved rows."),
        "precision_status": status,
        "mistake_counts": counts,
        "recent_events": rows[-5:],
    }


def _resolved_rows(cur, *, symbol: Optional[str], horizon: int, limit: int) -> List[Dict[str, Any]]:
    price_col = f"price_{horizon}d"
    ret_col = f"return_{horizon}d_pct"
    correct_col = f"correct_{horizon}d"
    resolved_col = f"resolved_{horizon}d_at"
    cols = [
        "id", "symbol", "created_at", "reference_price", "direction", "action", "confidence",
        "accuracy_grade", "stop_loss", "target_price", "risk_reward", "max_favorable_pct", "max_adverse_pct",
        "hit_target", "hit_stop", price_col, ret_col, correct_col, resolved_col,
    ]
    where = f"{resolved_col} IS NOT NULL AND {price_col} IS NOT NULL"
    params: List[Any] = []
    if symbol:
        where += " AND symbol = %s"
        params.append(symbol.upper())
    cur.execute(
        f"SELECT {', '.join(cols)} FROM super_ghost_predictions WHERE {where} ORDER BY created_at DESC LIMIT %s",
        params + [max(1, min(5000, int(limit)))],
    )
    rows = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        d[f"price_{horizon}d"] = d.pop(price_col)
        d[f"return_{horizon}d_pct"] = d.pop(ret_col)
        d[f"correct_{horizon}d"] = d.pop(correct_col)
        d[f"resolved_{horizon}d_at"] = d.pop(resolved_col)
        rows.append(d)
    return list(reversed(rows))


def score_precision_from_ledger(*, symbol: Optional[str] = None, horizon: int = 5, limit: int = 1000) -> Dict[str, Any]:
    h = horizon if horizon in HORIZONS else 5
    try:
        from core.db import db_conn
        from core.super_ghost_ledger import ensure_ledger_table

        with db_conn() as conn:
            cur = conn.cursor()
            ensure_ledger_table(cur)
            ensure_precision_tables(cur)
            rows = _resolved_rows(cur, symbol=symbol, horizon=h, limit=limit)
            events = [score_ledger_row(r, horizon=h) for r in rows]
            now = _now()
            upserts = 0
            for ev in events:
                errs = ev.get("errors_pct") or {}
                cur.execute(
                    """
                    INSERT INTO super_ghost_precision_events (
                        ledger_id, symbol, horizon_days, created_at, evaluated_at, direction,
                        direction_result, direction_correct, target_stop_result, precision_score,
                        precision_grade, overall_score, target_error_pct, stop_error_pct,
                        open_error_pct, low_error_pct, high_error_pct, close_error_pct,
                        mistake_type, lesson, payload_json
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (ledger_id, horizon_days) DO UPDATE SET
                        evaluated_at=EXCLUDED.evaluated_at,
                        direction_result=EXCLUDED.direction_result,
                        direction_correct=EXCLUDED.direction_correct,
                        target_stop_result=EXCLUDED.target_stop_result,
                        precision_score=EXCLUDED.precision_score,
                        precision_grade=EXCLUDED.precision_grade,
                        overall_score=EXCLUDED.overall_score,
                        target_error_pct=EXCLUDED.target_error_pct,
                        stop_error_pct=EXCLUDED.stop_error_pct,
                        open_error_pct=EXCLUDED.open_error_pct,
                        low_error_pct=EXCLUDED.low_error_pct,
                        high_error_pct=EXCLUDED.high_error_pct,
                        close_error_pct=EXCLUDED.close_error_pct,
                        mistake_type=EXCLUDED.mistake_type,
                        lesson=EXCLUDED.lesson,
                        payload_json=EXCLUDED.payload_json
                    """,
                    (
                        ev["ledger_id"], ev["symbol"], h, ev.get("created_at"), now, ev.get("direction"),
                        ev.get("direction_result"), ev.get("direction_correct"), ev.get("target_stop_result"), ev.get("precision_score"),
                        ev.get("precision_grade"), ev.get("overall_score"), errs.get("target"), errs.get("stop"),
                        errs.get("open"), errs.get("low"), errs.get("high"), errs.get("close_vs_entry"),
                        ev.get("mistake_type"), ev.get("lesson"), _jsonb(ev),
                    ),
                )
                upserts += 1

            groups: Dict[tuple, List[Dict[str, Any]]] = {}
            for ev in events:
                groups.setdefault((ev["symbol"], h, ev.get("direction") or "HOLD"), []).append(ev)
            profiles = []
            for (sym, hh, direction), evs in groups.items():
                profile = profile_from_precision_events(evs)
                payload = dict(profile)
                cur.execute(
                    """
                    INSERT INTO super_ghost_precision_profiles (
                        symbol, horizon_days, direction, sample_count, avg_precision_score,
                        avg_overall_score, direction_win_rate, precise_rate, poor_precision_rate,
                        avg_abs_target_error_pct, avg_abs_stop_error_pct, primary_mistake_type,
                        primary_lesson, precision_status, updated_at, payload_json
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (symbol, horizon_days, direction) DO UPDATE SET
                        sample_count=EXCLUDED.sample_count,
                        avg_precision_score=EXCLUDED.avg_precision_score,
                        avg_overall_score=EXCLUDED.avg_overall_score,
                        direction_win_rate=EXCLUDED.direction_win_rate,
                        precise_rate=EXCLUDED.precise_rate,
                        poor_precision_rate=EXCLUDED.poor_precision_rate,
                        avg_abs_target_error_pct=EXCLUDED.avg_abs_target_error_pct,
                        avg_abs_stop_error_pct=EXCLUDED.avg_abs_stop_error_pct,
                        primary_mistake_type=EXCLUDED.primary_mistake_type,
                        primary_lesson=EXCLUDED.primary_lesson,
                        precision_status=EXCLUDED.precision_status,
                        updated_at=EXCLUDED.updated_at,
                        payload_json=EXCLUDED.payload_json
                    """,
                    (
                        sym, hh, direction, int(profile.get("sample_count") or 0), profile.get("avg_precision_score"),
                        profile.get("avg_overall_score"), profile.get("direction_win_rate"), profile.get("precise_rate"),
                        profile.get("poor_precision_rate"), profile.get("avg_abs_target_error_pct"), profile.get("avg_abs_stop_error_pct"),
                        profile.get("primary_mistake_type"), profile.get("primary_lesson"), profile.get("precision_status"), now, _jsonb(payload),
                    ),
                )
                profiles.append({"symbol": sym, "direction": direction, **profile})
        return {"ok": True, "symbol": (symbol or "ALL").upper(), "horizon_days": h, "events_scored": upserts, "profiles_updated": len(profiles), "profiles": profiles[:20]}
    except Exception as exc:
        LOGGER.warning("score_precision_from_ledger: %s", str(exc)[:180])
        return {"ok": False, "error": str(exc)[:180], "events_scored": 0, "profiles_updated": 0}


def precision_summary(*, symbol: Optional[str] = None, horizon: int = 5, limit: int = 20) -> Dict[str, Any]:
    h = horizon if horizon in HORIZONS else 5
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_precision_tables(cur)
            params: List[Any] = [h]
            where = "horizon_days=%s"
            if symbol:
                where += " AND symbol=%s"
                params.append(symbol.upper())
            cur.execute(
                f"""
                SELECT symbol, horizon_days, direction, sample_count, avg_precision_score,
                       avg_overall_score, direction_win_rate, precise_rate, poor_precision_rate,
                       avg_abs_target_error_pct, avg_abs_stop_error_pct, primary_mistake_type,
                       primary_lesson, precision_status, updated_at, payload_json
                FROM super_ghost_precision_profiles
                WHERE {where}
                ORDER BY updated_at DESC, sample_count DESC
                LIMIT %s
                """,
                params + [max(1, min(100, int(limit)))],
            )
            profiles = []
            for r in cur.fetchall():
                payload = _coerce_json(r[15]) or {}
                profiles.append({
                    "symbol": r[0], "horizon_days": r[1], "direction": r[2], "sample_count": r[3],
                    "avg_precision_score": r[4], "avg_overall_score": r[5], "direction_win_rate": r[6],
                    "precise_rate": r[7], "poor_precision_rate": r[8], "avg_abs_target_error_pct": r[9],
                    "avg_abs_stop_error_pct": r[10], "primary_mistake_type": r[11], "primary_lesson": r[12],
                    "precision_status": r[13], "updated_at": r[14], "mistake_counts": payload.get("mistake_counts"),
                })
            ev_params: List[Any] = [h]
            ev_where = "horizon_days=%s"
            if symbol:
                ev_where += " AND symbol=%s"
                ev_params.append(symbol.upper())
            cur.execute(
                f"""
                SELECT ledger_id, symbol, horizon_days, direction, direction_result,
                       precision_score, precision_grade, mistake_type, lesson, evaluated_at, payload_json
                FROM super_ghost_precision_events
                WHERE {ev_where}
                ORDER BY evaluated_at DESC, id DESC
                LIMIT %s
                """,
                ev_params + [max(1, min(100, int(limit)))],
            )
            events = []
            for r in cur.fetchall():
                payload = _coerce_json(r[10]) or {}
                events.append({
                    "ledger_id": r[0], "symbol": r[1], "horizon_days": r[2], "direction": r[3],
                    "direction_result": r[4], "precision_score": r[5], "precision_grade": r[6],
                    "mistake_type": r[7], "lesson": r[8], "evaluated_at": r[9],
                    "errors_pct": payload.get("errors_pct"), "overall_score": payload.get("overall_score"),
                })
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:180], "profiles": [], "recent_events": []}

    primary = profiles[0] if profiles else None
    return {
        "ok": True,
        "enabled": True,
        "symbol": (symbol or "ALL").upper(),
        "horizon_days": h,
        "min_samples": MIN_PRECISION_SAMPLES,
        "required_avg_precision_score": PRICE_PRECISION_TARGET,
        "available": bool(primary and (primary.get("sample_count") or 0) >= MIN_PRECISION_SAMPLES),
        "primary_profile": primary,
        "profiles": profiles,
        "recent_events": events,
        "note": "Direction WIN/LOSS and price precision are separate. Top Picks should require both directional proof and price precision.",
    }
