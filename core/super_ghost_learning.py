"""Super Ghost Learning Brain (PR #93).

Turns resolved prediction outcomes into lessons Ghost can use next time.

Example: if Ghost predicted a target near $5 and realized price prints $7, the
learning brain records a postmortem (target too low / upside underestimated),
updates a per-symbol/direction/horizon learning profile, and exposes a modest
future target/confidence adjustment. It never fabricates accuracy and never
bypasses the Super Ghost coverage/risk gates.

Design boundaries:
- Prediction intelligence only; not auto-trading.
- Learns only from resolved ledger rows (truth first, no training accuracy hype).
- Small-sample safe: adjustments stay advisory until enough samples exist.
- Bounded adjustments: target move multiplier capped, confidence delta capped.
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

LOGGER = logging.getLogger("ghost.super_ghost_learning")

LEARNING_HORIZONS = (1, 5, 20)
MIN_PROFILE_SAMPLES = 3
MIN_STRONG_SAMPLES = 5
MAX_CONF_DELTA = 0.08
MAX_TARGET_MOVE_MULT = 1.35
MIN_TARGET_MOVE_MULT = 0.70


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


@dataclass
class Lesson:
    ledger_id: int
    symbol: str
    horizon_days: int
    direction: str
    mistake_type: str
    lesson: str
    reference_price: Optional[float]
    predicted_target: Optional[float]
    predicted_stop: Optional[float]
    realized_price: Optional[float]
    realized_return_pct: Optional[float]
    direction_correct: Optional[bool]
    target_error_pct: Optional[float]
    stop_error_pct: Optional[float]
    confidence: Optional[float]
    accuracy_grade: Optional[str]
    regime_label: Optional[str]
    regime_risk_state: Optional[str]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ledger_id": self.ledger_id,
            "symbol": self.symbol,
            "horizon_days": self.horizon_days,
            "direction": self.direction,
            "mistake_type": self.mistake_type,
            "lesson": self.lesson,
            "reference_price": self.reference_price,
            "predicted_target": self.predicted_target,
            "predicted_stop": self.predicted_stop,
            "realized_price": self.realized_price,
            "realized_return_pct": self.realized_return_pct,
            "direction_correct": self.direction_correct,
            "target_error_pct": self.target_error_pct,
            "stop_error_pct": self.stop_error_pct,
            "confidence": self.confidence,
            "accuracy_grade": self.accuracy_grade,
            "regime_label": self.regime_label,
            "regime_risk_state": self.regime_risk_state,
        }


def ensure_learning_tables(cur) -> None:
    """Create learning tables. Safe, non-destructive."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS super_ghost_learning_events (
            id SERIAL PRIMARY KEY,
            ledger_id INT NOT NULL,
            symbol VARCHAR(20) NOT NULL,
            horizon_days INT NOT NULL,
            created_at BIGINT NOT NULL,
            learned_at BIGINT NOT NULL,
            direction VARCHAR(10),
            mistake_type VARCHAR(64),
            lesson TEXT,
            reference_price FLOAT,
            predicted_target FLOAT,
            predicted_stop FLOAT,
            realized_price FLOAT,
            realized_return_pct FLOAT,
            direction_correct BOOLEAN,
            target_error_pct FLOAT,
            stop_error_pct FLOAT,
            confidence FLOAT,
            accuracy_grade VARCHAR(8),
            regime_label VARCHAR(40),
            regime_risk_state VARCHAR(20),
            payload_json JSONB,
            UNIQUE (ledger_id, horizon_days)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS super_ghost_learning_profiles (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            horizon_days INT NOT NULL,
            direction VARCHAR(10) NOT NULL,
            sample_count INT NOT NULL,
            direction_win_rate FLOAT,
            avg_realized_return_pct FLOAT,
            avg_target_error_pct FLOAT,
            avg_abs_target_error_pct FLOAT,
            target_move_multiplier FLOAT,
            confidence_delta FLOAT,
            conviction_multiplier FLOAT,
            learning_status VARCHAR(32),
            primary_lesson TEXT,
            updated_at BIGINT NOT NULL,
            payload_json JSONB,
            UNIQUE (symbol, horizon_days, direction)
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_sg_learning_events_symbol ON super_ghost_learning_events (symbol, learned_at DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_sg_learning_profiles_symbol ON super_ghost_learning_profiles (symbol, horizon_days, direction)"
    )


def _target_error_pct(direction: str, target: Optional[float], realized: Optional[float]) -> Optional[float]:
    """Positive means Ghost's target was too conservative for the realized move.

    For UP calls: realized above target => positive (target too low).
    For DOWN calls with a downside target: realized below target => positive.
    Existing Super Ghost risk plan is long-biased (target usually above entry),
    so DOWN target learning is kept mostly advisory.
    """
    if target is None or realized is None or target <= 0:
        return None
    d = (direction or "").upper()
    if d == "DOWN":
        return round((target - realized) / target * 100.0, 3)
    return round((realized - target) / target * 100.0, 3)


def _stop_error_pct(direction: str, stop: Optional[float], realized: Optional[float]) -> Optional[float]:
    if stop is None or realized is None or stop <= 0:
        return None
    d = (direction or "").upper()
    if d == "DOWN":
        return round((realized - stop) / stop * 100.0, 3)
    return round((stop - realized) / stop * 100.0, 3)


def classify_lesson(row: Dict[str, Any], *, horizon: int = 5) -> Lesson:
    """Pure postmortem classifier for one resolved ledger row.

    This is the heart of "if Ghost said $5 and it goes $7, learn why." It labels
    both direction mistakes and magnitude/target mistakes.
    """
    h = horizon if horizon in LEARNING_HORIZONS else 5
    direction = str(row.get("direction") or "HOLD").upper()
    realized = _f(row.get(f"price_{h}d"))
    ret = _f(row.get(f"return_{h}d_pct"))
    correct = row.get(f"correct_{h}d")
    if correct is not None:
        correct = bool(correct)
    ref = _f(row.get("reference_price"))
    target = _f(row.get("target_price"))
    stop = _f(row.get("stop_loss"))
    terr = _target_error_pct(direction, target, realized)
    serr = _stop_error_pct(direction, stop, realized)

    mistake_type = "unresolved"
    lesson = "Outcome has not resolved yet; no learning applied."

    if realized is None or ret is None:
        mistake_type = "price_unavailable"
        lesson = "Realized price unavailable; keep this row out of learning until price data resolves."
    elif direction == "HOLD":
        if correct:
            mistake_type = "good_skip"
            lesson = "HOLD/NO EDGE was correct because realized move stayed small. Continue requiring edge before acting."
        else:
            mistake_type = "missed_move"
            lesson = "Ghost skipped a meaningful move. Review missing catalysts, momentum, and coverage gates for this setup."
    elif correct is False:
        mistake_type = "wrong_direction"
        lesson = f"Ghost called {direction} but realized return was {ret:+.2f}%. Reduce confidence for similar setups until drivers improve."
    elif terr is not None and terr >= 20.0:
        mistake_type = "target_too_low"
        lesson = f"Ghost got direction right but target was too conservative by {terr:.1f}%. Future targets for similar setups may need a wider move estimate."
    elif terr is not None and terr <= -20.0:
        mistake_type = "target_too_high"
        lesson = f"Ghost got direction right but target was too aggressive by {abs(terr):.1f}%. Future targets for similar setups should be tightened unless new evidence appears."
    elif correct is True:
        mistake_type = "direction_right"
        lesson = "Ghost got direction right. Keep this setup as positive evidence for similar future calls."

    return Lesson(
        ledger_id=int(row.get("id") or row.get("ledger_id") or 0),
        symbol=str(row.get("symbol") or "").upper(),
        horizon_days=h,
        direction=direction,
        mistake_type=mistake_type,
        lesson=lesson,
        reference_price=ref,
        predicted_target=target,
        predicted_stop=stop,
        realized_price=realized,
        realized_return_pct=ret,
        direction_correct=correct,
        target_error_pct=terr,
        stop_error_pct=serr,
        confidence=_f(row.get("confidence")),
        accuracy_grade=row.get("accuracy_grade"),
        regime_label=row.get("regime_label"),
        regime_risk_state=row.get("regime_risk_state"),
    )


def profile_from_lessons(lessons: Iterable[Dict[str, Any] | Lesson]) -> Dict[str, Any]:
    """Build a bounded learning profile from row-level lessons."""
    rows = [l.as_dict() if isinstance(l, Lesson) else dict(l) for l in lessons]
    rows = [r for r in rows if r.get("direction") in ("UP", "DOWN", "HOLD") and r.get("realized_return_pct") is not None]
    n = len(rows)
    if n == 0:
        return {"sample_count": 0, "learning_status": "cold_start", "available": False}

    wins = [r for r in rows if r.get("direction_correct") is True]
    rets = [_f(r.get("realized_return_pct")) for r in rows]
    rets = [x for x in rets if x is not None]
    avg_ret = sum(rets) / len(rets) if rets else None
    win_rate = len(wins) / n if n else None

    # Target-magnitude calibration must learn ONLY from direction-correct rows.
    # A wrong-direction prediction's "target error" is meaningless noise for
    # how far a *correct* call should aim; mixing it in dilutes the real lesson
    # (e.g. "$5 target, price hit $7" = target too low). We therefore compute
    # the target-move multiplier from correct-direction lessons and require a
    # minimum number of them before adjusting.
    te_rows = [r for r in wins if _f(r.get("target_error_pct")) is not None]
    target_errs_correct = [_f(r.get("target_error_pct")) for r in te_rows]
    # Report-level error stats still reflect every resolved row (full picture).
    all_target_errs = [_f(r.get("target_error_pct")) for r in rows if _f(r.get("target_error_pct")) is not None]
    avg_te = (sum(target_errs_correct) / len(target_errs_correct)) if target_errs_correct else None
    avg_abs_te = (sum(abs(x) for x in all_target_errs) / len(all_target_errs)) if all_target_errs else None

    # Target multiplier is on the move from entry->target, not on absolute price.
    # If average (correct-call) target error is +40%, widen the move, but cap
    # hard to avoid overfit, and only once enough correct-direction samples exist.
    if avg_te is None or len(target_errs_correct) < MIN_PROFILE_SAMPLES:
        target_mult = 1.0
    else:
        target_mult = _clamp(1.0 + (avg_te / 100.0) * 0.50, MIN_TARGET_MOVE_MULT, MAX_TARGET_MOVE_MULT)

    # Confidence learning is intentionally modest and sample-count gated.
    conf_delta = 0.0
    conv_mult = 1.0
    status = "cold_start" if n < MIN_PROFILE_SAMPLES else "learning"
    if n >= MIN_PROFILE_SAMPLES and win_rate is not None:
        conf_delta = _clamp((win_rate - 0.50) * 0.20, -MAX_CONF_DELTA, MAX_CONF_DELTA)
        conv_mult = _clamp(1.0 + (win_rate - 0.50) * 0.25, 0.85, 1.15)
        if n >= MIN_STRONG_SAMPLES and win_rate < 0.40:
            status = "dampen"
        elif n >= MIN_STRONG_SAMPLES and win_rate >= 0.65:
            status = "supportive"

    counts: Dict[str, int] = {}
    for r in rows:
        mt = str(r.get("mistake_type") or "unknown")
        counts[mt] = counts.get(mt, 0) + 1
    primary_type = max(counts.items(), key=lambda kv: kv[1])[0] if counts else "unknown"
    if primary_type == "target_too_low":
        primary_lesson = "Targets have been too conservative; widen future target move modestly when similar evidence appears."
    elif primary_type == "target_too_high":
        primary_lesson = "Targets have been too aggressive; tighten future target move unless evidence improves."
    elif primary_type == "wrong_direction":
        primary_lesson = "Direction has been wrong too often; dampen confidence for similar setups."
    elif primary_type == "missed_move":
        primary_lesson = "Ghost has skipped meaningful moves; improve catalyst/momentum coverage before promoting."
    else:
        primary_lesson = "No dominant mistake pattern yet; keep collecting resolved outcomes."

    return {
        "available": n >= MIN_PROFILE_SAMPLES,
        "sample_count": n,
        "direction_win_rate": round(win_rate, 4) if win_rate is not None else None,
        "avg_realized_return_pct": round(avg_ret, 3) if avg_ret is not None else None,
        "avg_target_error_pct": round(avg_te, 3) if avg_te is not None else None,
        "avg_abs_target_error_pct": round(avg_abs_te, 3) if avg_abs_te is not None else None,
        "target_move_multiplier": round(target_mult, 4),
        "target_calibration_samples": len(target_errs_correct),
        "confidence_delta": round(conf_delta, 4),
        "conviction_multiplier": round(conv_mult, 4),
        "learning_status": status,
        "primary_mistake_type": primary_type,
        "primary_lesson": primary_lesson,
        "mistake_counts": counts,
        "recent_lessons": rows[-5:],
    }


def apply_learning_to_report(report: Dict[str, Any], profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Apply bounded learning profile to a Super Ghost report.

    Mutates and returns a copy. If the profile is cold/absent, it adds a visible
    learning block but does not change prediction values.
    """
    out = dict(report or {})
    pred = dict(out.get("prediction") or {})
    risk = dict(out.get("risk_plan") or {})
    profile = dict(profile or {})
    if not profile or not profile.get("available"):
        out["learning_adjustment"] = {
            "available": False,
            "status": profile.get("learning_status") or "cold_start",
            "sample_count": int(profile.get("sample_count") or 0),
            "message": "Learning brain is collecting resolved outcomes; no adjustment applied yet.",
        }
        return out

    original_conf = _f(pred.get("confidence"))
    original_conv = _f(pred.get("conviction_score"))
    conf_delta = _f(profile.get("confidence_delta")) or 0.0
    conv_mult = _f(profile.get("conviction_multiplier")) or 1.0
    target_mult = _f(profile.get("target_move_multiplier")) or 1.0
    direction = str(pred.get("direction") or "HOLD").upper()

    if original_conf is not None:
        pred["confidence"] = round(_clamp(original_conf + conf_delta, 0.50, 0.95), 3)
    if original_conv is not None:
        pred["conviction_score"] = round(_clamp(original_conv * conv_mult, 0.0, 100.0), 1)

    old_target = _f(risk.get("target_price"))
    entry = _f(risk.get("entry"))
    new_target = old_target
    # Existing risk plan is long-oriented; adjust only UP target move for now.
    if direction == "UP" and old_target is not None and entry is not None and old_target > entry and target_mult != 1.0:
        new_target = round(entry + (old_target - entry) * target_mult, 4)
        risk["target_price_original"] = old_target
        risk["target_price"] = new_target
        if risk.get("stop_loss") is not None and entry > _f(risk.get("stop_loss")):
            reward = new_target - entry
            risk_amt = entry - float(risk["stop_loss"])
            if risk_amt > 0:
                risk["risk_reward_ratio"] = round(reward / risk_amt, 4)

    if profile.get("learning_status") == "dampen" and direction in ("UP", "DOWN"):
        # Do not pretend bad history is fine. Keep it watch-only until profile improves.
        pred["action"] = "NO EDGE — LEARNING BLOCK"
        if pred.get("accuracy_grade") in ("A+", "A", "B+", "B"):
            pred["accuracy_grade"] = "C"

    out["prediction"] = pred
    out["risk_plan"] = risk
    out["learning_adjustment"] = {
        "available": True,
        "status": profile.get("learning_status"),
        "sample_count": profile.get("sample_count"),
        "direction_win_rate": profile.get("direction_win_rate"),
        "confidence_delta": round(conf_delta, 4),
        "conviction_multiplier": round(conv_mult, 4),
        "target_move_multiplier": round(target_mult, 4),
        "old_target_price": old_target,
        "new_target_price": new_target,
        "primary_lesson": profile.get("primary_lesson"),
        "primary_mistake_type": profile.get("primary_mistake_type"),
        "message": "Learning adjustment is bounded and based only on resolved Super Ghost ledger outcomes.",
    }
    return out


def _resolved_rows(cur, *, symbol: Optional[str], horizon: int, limit: int) -> List[Dict[str, Any]]:
    price_col = f"price_{horizon}d"
    ret_col = f"return_{horizon}d_pct"
    correct_col = f"correct_{horizon}d"
    resolved_col = f"resolved_{horizon}d_at"
    cols = [
        "id", "symbol", "created_at", "reference_price", "direction", "action", "confidence",
        "conviction_score", "edge_score", "quality_score", "accuracy_grade", "checklist_coverage",
        "regime_label", "regime_risk_state", "stop_loss", "target_price",
        price_col, ret_col, correct_col, resolved_col,
    ]
    where = f"{resolved_col} IS NOT NULL AND {price_col} IS NOT NULL"
    params: List[Any] = []
    if symbol:
        where += " AND symbol = %s"
        params.append(symbol.upper())
    cur.execute(
        f"SELECT {', '.join(cols)} FROM super_ghost_predictions WHERE {where} ORDER BY created_at DESC LIMIT %s",
        params + [max(1, min(2000, int(limit)))],
    )
    rows = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        d[f"price_{horizon}d"] = d.pop(price_col)
        d[f"return_{horizon}d_pct"] = d.pop(ret_col)
        d[f"correct_{horizon}d"] = d.pop(correct_col)
        d[f"resolved_{horizon}d_at"] = d.pop(resolved_col)
        rows.append(d)
    return list(reversed(rows))  # oldest -> newest for profile building


def learn_from_ledger(*, symbol: Optional[str] = None, horizon: int = 5, limit: int = 500) -> Dict[str, Any]:
    """Generate/update learning events and profiles from resolved ledger rows."""
    h = horizon if horizon in LEARNING_HORIZONS else 5
    try:
        from core.db import db_conn
        from core.super_ghost_ledger import ensure_ledger_table

        with db_conn() as conn:
            cur = conn.cursor()
            ensure_ledger_table(cur)
            ensure_learning_tables(cur)
            rows = _resolved_rows(cur, symbol=symbol, horizon=h, limit=limit)
            lessons = [classify_lesson(r, horizon=h) for r in rows]
            now = _now()
            inserted_or_seen = 0
            for lesson in lessons:
                d = lesson.as_dict()
                cur.execute(
                    """
                    INSERT INTO super_ghost_learning_events (
                        ledger_id, symbol, horizon_days, created_at, learned_at, direction,
                        mistake_type, lesson, reference_price, predicted_target, predicted_stop,
                        realized_price, realized_return_pct, direction_correct, target_error_pct,
                        stop_error_pct, confidence, accuracy_grade, regime_label, regime_risk_state, payload_json
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
                    ) ON CONFLICT (ledger_id, horizon_days) DO UPDATE SET
                        learned_at = EXCLUDED.learned_at,
                        mistake_type = EXCLUDED.mistake_type,
                        lesson = EXCLUDED.lesson,
                        realized_price = EXCLUDED.realized_price,
                        realized_return_pct = EXCLUDED.realized_return_pct,
                        direction_correct = EXCLUDED.direction_correct,
                        target_error_pct = EXCLUDED.target_error_pct,
                        stop_error_pct = EXCLUDED.stop_error_pct,
                        payload_json = EXCLUDED.payload_json
                    """,
                    (
                        d["ledger_id"], d["symbol"], d["horizon_days"], int(next((r.get("created_at") for r in rows if int(r.get("id") or 0)==d["ledger_id"]), now)), now,
                        d["direction"], d["mistake_type"], d["lesson"], d["reference_price"], d["predicted_target"], d["predicted_stop"],
                        d["realized_price"], d["realized_return_pct"], d["direction_correct"], d["target_error_pct"], d["stop_error_pct"],
                        d["confidence"], d["accuracy_grade"], d["regime_label"], d["regime_risk_state"], _jsonb(d),
                    ),
                )
                inserted_or_seen += 1

            # Profiles by symbol + direction.
            grouped: Dict[Tuple[str, str], List[Lesson]] = {}
            for l in lessons:
                grouped.setdefault((l.symbol, l.direction), []).append(l)
            profiles = []
            for (sym, direction), ls in grouped.items():
                profile = profile_from_lessons(ls)
                payload = dict(profile)
                cur.execute(
                    """
                    INSERT INTO super_ghost_learning_profiles (
                        symbol, horizon_days, direction, sample_count, direction_win_rate,
                        avg_realized_return_pct, avg_target_error_pct, avg_abs_target_error_pct,
                        target_move_multiplier, confidence_delta, conviction_multiplier,
                        learning_status, primary_lesson, updated_at, payload_json
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (symbol, horizon_days, direction) DO UPDATE SET
                        sample_count = EXCLUDED.sample_count,
                        direction_win_rate = EXCLUDED.direction_win_rate,
                        avg_realized_return_pct = EXCLUDED.avg_realized_return_pct,
                        avg_target_error_pct = EXCLUDED.avg_target_error_pct,
                        avg_abs_target_error_pct = EXCLUDED.avg_abs_target_error_pct,
                        target_move_multiplier = EXCLUDED.target_move_multiplier,
                        confidence_delta = EXCLUDED.confidence_delta,
                        conviction_multiplier = EXCLUDED.conviction_multiplier,
                        learning_status = EXCLUDED.learning_status,
                        primary_lesson = EXCLUDED.primary_lesson,
                        updated_at = EXCLUDED.updated_at,
                        payload_json = EXCLUDED.payload_json
                    """,
                    (
                        sym, h, direction, int(profile.get("sample_count") or 0), profile.get("direction_win_rate"),
                        profile.get("avg_realized_return_pct"), profile.get("avg_target_error_pct"), profile.get("avg_abs_target_error_pct"),
                        profile.get("target_move_multiplier"), profile.get("confidence_delta"), profile.get("conviction_multiplier"),
                        profile.get("learning_status"), profile.get("primary_lesson"), now, _jsonb(payload),
                    ),
                )
                profiles.append({"symbol": sym, "direction": direction, **profile})
        return {"ok": True, "horizon_days": h, "symbol": (symbol or "ALL").upper(), "learned_events": inserted_or_seen, "profiles_updated": len(profiles), "profiles": profiles[:20]}
    except Exception as exc:
        LOGGER.warning("learn_from_ledger: %s", str(exc)[:180])
        return {"ok": False, "error": str(exc)[:180], "learned_events": 0, "profiles_updated": 0}


def get_learning_profile(symbol: str, direction: str, *, horizon: int = 5) -> Dict[str, Any]:
    sym = (symbol or "").upper()
    d = (direction or "HOLD").upper()
    h = horizon if horizon in LEARNING_HORIZONS else 5
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_learning_tables(cur)
            cur.execute(
                """
                SELECT sample_count, direction_win_rate, avg_realized_return_pct,
                       avg_target_error_pct, avg_abs_target_error_pct, target_move_multiplier,
                       confidence_delta, conviction_multiplier, learning_status, primary_lesson,
                       updated_at, payload_json
                FROM super_ghost_learning_profiles
                WHERE symbol=%s AND horizon_days=%s AND direction=%s
                """,
                (sym, h, d),
            )
            r = cur.fetchone()
    except Exception as exc:
        return {"available": False, "error": str(exc)[:120], "sample_count": 0, "learning_status": "unavailable"}
    if not r:
        return {"available": False, "symbol": sym, "direction": d, "horizon_days": h, "sample_count": 0, "learning_status": "cold_start"}
    payload = _coerce_json(r[11]) if len(r) > 11 else {}
    out = dict(payload or {})
    out.update({
        "available": bool((r[0] or 0) >= MIN_PROFILE_SAMPLES),
        "symbol": sym,
        "direction": d,
        "horizon_days": h,
        "sample_count": int(r[0] or 0),
        "direction_win_rate": r[1],
        "avg_realized_return_pct": r[2],
        "avg_target_error_pct": r[3],
        "avg_abs_target_error_pct": r[4],
        "target_move_multiplier": r[5],
        "confidence_delta": r[6],
        "conviction_multiplier": r[7],
        "learning_status": r[8],
        "primary_lesson": r[9],
        "updated_at": r[10],
    })
    return out


def learning_summary(*, symbol: Optional[str] = None, horizon: int = 5, limit: int = 20) -> Dict[str, Any]:
    h = horizon if horizon in LEARNING_HORIZONS else 5
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_learning_tables(cur)
            params: List[Any] = []
            where = "horizon_days=%s"
            params.append(h)
            if symbol:
                where += " AND symbol=%s"
                params.append(symbol.upper())
            cur.execute(
                f"""
                SELECT symbol, horizon_days, direction, sample_count, direction_win_rate,
                       target_move_multiplier, confidence_delta, conviction_multiplier,
                       learning_status, primary_lesson, updated_at, payload_json
                FROM super_ghost_learning_profiles
                WHERE {where}
                ORDER BY updated_at DESC, sample_count DESC
                LIMIT %s
                """,
                params + [max(1, min(100, int(limit)))],
            )
            profiles = []
            for r in cur.fetchall():
                payload = _coerce_json(r[11]) or {}
                profiles.append({
                    "symbol": r[0], "horizon_days": r[1], "direction": r[2], "sample_count": r[3],
                    "direction_win_rate": r[4], "target_move_multiplier": r[5], "confidence_delta": r[6],
                    "conviction_multiplier": r[7], "learning_status": r[8], "primary_lesson": r[9],
                    "updated_at": r[10], "mistake_counts": payload.get("mistake_counts"),
                })
            cur.execute(
                f"""
                SELECT ledger_id, symbol, horizon_days, direction, mistake_type, lesson,
                       predicted_target, realized_price, target_error_pct, direction_correct, learned_at
                FROM super_ghost_learning_events
                WHERE {where}
                ORDER BY learned_at DESC
                LIMIT %s
                """,
                params + [max(1, min(100, int(limit)))],
            )
            lessons = [
                {
                    "ledger_id": r[0], "symbol": r[1], "horizon_days": r[2], "direction": r[3],
                    "mistake_type": r[4], "lesson": r[5], "predicted_target": r[6],
                    "realized_price": r[7], "target_error_pct": r[8], "direction_correct": r[9],
                    "learned_at": r[10],
                }
                for r in cur.fetchall()
            ]
        return {"ok": True, "horizon_days": h, "symbol": (symbol or "ALL").upper(), "profiles": profiles, "recent_lessons": lessons, "min_samples_for_adjustment": MIN_PROFILE_SAMPLES}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160], "profiles": [], "recent_lessons": []}
