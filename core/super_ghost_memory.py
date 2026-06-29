"""Super Ghost Model Registry + Feature Attribution Memory (PR #96).

This is the evidence-memory layer underneath the evolving Ghost brain.

PR #93 taught Ghost to classify mistakes. PR #95 let Ghost compare challenger
policies. PR #96 answers the next question: which evidence/features caused the
prediction to be right or wrong?

Design:
- every logged Super Ghost prediction stores its model/version metadata;
- every checklist item becomes an attribution row tied to that prediction;
- after outcomes resolve, each feature is scored as helped/hurt/noise/missing;
- long-term feature profiles remember which evidence has been reliable by
  symbol/horizon.

This is prediction intelligence only. No auto-trading. No auto-promotion.
"""
from __future__ import annotations

import json
import logging
import math
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

LOGGER = logging.getLogger("ghost.super_ghost_memory")

HORIZONS = (1, 5, 20)
DEFAULT_MODEL_ID = "super_ghost_checklist_v1"
DEFAULT_FEATURE_SET_ID = "super_ghost_25_point_v1"


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


def ensure_memory_tables(cur) -> None:
    """Create model registry + feature memory tables. Safe/idempotent."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS super_ghost_model_versions (
            id SERIAL PRIMARY KEY,
            model_id VARCHAR(80) NOT NULL UNIQUE,
            model_name TEXT NOT NULL,
            model_type VARCHAR(60) NOT NULL,
            version VARCHAR(40) NOT NULL,
            status VARCHAR(30) NOT NULL,
            feature_set_id VARCHAR(80),
            parameters_json JSONB,
            notes TEXT,
            created_at BIGINT NOT NULL,
            updated_at BIGINT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS super_ghost_prediction_features (
            id SERIAL PRIMARY KEY,
            ledger_id INT NOT NULL,
            symbol VARCHAR(20) NOT NULL,
            model_id VARCHAR(80) NOT NULL,
            feature_set_id VARCHAR(80) NOT NULL,
            feature_name VARCHAR(120) NOT NULL,
            feature_category VARCHAR(80),
            feature_source VARCHAR(80),
            feature_available BOOLEAN,
            feature_status VARCHAR(40),
            feature_score FLOAT,
            feature_confidence FLOAT,
            feature_weight FLOAT,
            feature_importance FLOAT,
            directional_effect VARCHAR(24),
            feature_value_json JSONB,
            created_at BIGINT NOT NULL,
            UNIQUE (ledger_id, feature_name)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS super_ghost_model_contributions (
            id SERIAL PRIMARY KEY,
            ledger_id INT NOT NULL,
            symbol VARCHAR(20) NOT NULL,
            model_id VARCHAR(80) NOT NULL,
            model_direction VARCHAR(10),
            model_confidence FLOAT,
            model_action TEXT,
            model_edge_score FLOAT,
            model_conviction_score FLOAT,
            final_direction VARCHAR(10),
            final_action TEXT,
            horizon_days INT,
            model_correct BOOLEAN,
            model_signed_return_pct FLOAT,
            created_at BIGINT NOT NULL,
            updated_at BIGINT NOT NULL,
            payload_json JSONB,
            UNIQUE (ledger_id, model_id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS super_ghost_feature_outcomes (
            id SERIAL PRIMARY KEY,
            ledger_id INT NOT NULL,
            feature_name VARCHAR(120) NOT NULL,
            symbol VARCHAR(20) NOT NULL,
            horizon_days INT NOT NULL,
            prediction_direction VARCHAR(10),
            realized_return_pct FLOAT,
            prediction_correct BOOLEAN,
            feature_score FLOAT,
            feature_importance FLOAT,
            feature_alignment VARCHAR(32),
            outcome_effect VARCHAR(40),
            lesson TEXT,
            scored_at BIGINT NOT NULL,
            payload_json JSONB,
            UNIQUE (ledger_id, feature_name, horizon_days)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS super_ghost_feature_profiles (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            horizon_days INT NOT NULL,
            feature_name VARCHAR(120) NOT NULL,
            feature_category VARCHAR(80),
            sample_count INT NOT NULL,
            available_count INT NOT NULL,
            helped_count INT NOT NULL,
            hurt_count INT NOT NULL,
            noise_count INT NOT NULL,
            missing_count INT NOT NULL,
            reliability FLOAT,
            avg_feature_score FLOAT,
            avg_feature_importance FLOAT,
            primary_lesson TEXT,
            updated_at BIGINT NOT NULL,
            payload_json JSONB,
            UNIQUE (symbol, horizon_days, feature_name)
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sg_pred_features_ledger ON super_ghost_prediction_features (ledger_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sg_feature_outcomes_symbol ON super_ghost_feature_outcomes (symbol, horizon_days, feature_name)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sg_feature_profiles_symbol ON super_ghost_feature_profiles (symbol, horizon_days, reliability DESC)")


def ensure_default_model(cur) -> None:
    now = _now()
    cur.execute(
        """
        INSERT INTO super_ghost_model_versions (
            model_id, model_name, model_type, version, status, feature_set_id,
            parameters_json, notes, created_at, updated_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
        ON CONFLICT (model_id) DO UPDATE SET
            status = EXCLUDED.status,
            feature_set_id = EXCLUDED.feature_set_id,
            updated_at = EXCLUDED.updated_at
        """,
        (
            DEFAULT_MODEL_ID,
            "Super Ghost 25-point checklist engine",
            "deterministic_checklist",
            "1",
            "production",
            DEFAULT_FEATURE_SET_ID,
            _jsonb({"checklist_points": 25, "coverage_gate": 18}),
            "Production deterministic Super Ghost engine; AI brief and learning layers are additive.",
            now,
            now,
        ),
    )


def _directional_effect(score: Optional[float]) -> str:
    if score is None:
        return "missing"
    if score > 0.25:
        return "bullish"
    if score < -0.25:
        return "bearish"
    return "neutral"


def _importance(item: Dict[str, Any]) -> Optional[float]:
    score = _f(item.get("score"))
    if score is None:
        return None
    weight = _f(item.get("weight")) or 1.0
    conf = _f(item.get("confidence"))
    if conf is None:
        conf = 1.0 if item.get("available") else 0.0
    return round(abs(score) * weight * conf, 4)


def extract_feature_rows(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Convert Super Ghost checklist rows to feature-attribution rows."""
    symbol = str(report.get("symbol") or "").upper()
    rows: List[Dict[str, Any]] = []
    for item in report.get("checklist") or []:
        if not isinstance(item, dict):
            continue
        score = _f(item.get("score"))
        rows.append({
            "symbol": symbol,
            "model_id": DEFAULT_MODEL_ID,
            "feature_set_id": DEFAULT_FEATURE_SET_ID,
            "feature_name": str(item.get("key") or item.get("title") or "unknown")[:120],
            "feature_category": item.get("category"),
            "feature_source": item.get("source"),
            "feature_available": bool(item.get("available")),
            "feature_status": item.get("status"),
            "feature_score": score,
            "feature_confidence": _f(item.get("confidence")),
            "feature_weight": _f(item.get("weight")),
            "feature_importance": _importance(item),
            "directional_effect": _directional_effect(score),
            "feature_value_json": item.get("value"),
        })
    return rows


def log_prediction_memory(cur, ledger_id: int, report: Dict[str, Any]) -> Dict[str, Any]:
    """Persist model/feature attribution rows for a freshly logged prediction."""
    ensure_memory_tables(cur)
    ensure_default_model(cur)
    symbol = str(report.get("symbol") or "").upper()
    pred = report.get("prediction") or {}
    now = int(report.get("ts") or _now())
    features = extract_feature_rows(report)
    inserted_features = 0
    for f in features:
        cur.execute(
            """
            INSERT INTO super_ghost_prediction_features (
                ledger_id, symbol, model_id, feature_set_id, feature_name,
                feature_category, feature_source, feature_available, feature_status,
                feature_score, feature_confidence, feature_weight, feature_importance,
                directional_effect, feature_value_json, created_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
            ON CONFLICT (ledger_id, feature_name) DO UPDATE SET
                feature_score = EXCLUDED.feature_score,
                feature_confidence = EXCLUDED.feature_confidence,
                feature_importance = EXCLUDED.feature_importance,
                directional_effect = EXCLUDED.directional_effect,
                feature_value_json = EXCLUDED.feature_value_json
            """,
            (
                ledger_id, symbol, f["model_id"], f["feature_set_id"], f["feature_name"],
                f.get("feature_category"), f.get("feature_source"), f.get("feature_available"), f.get("feature_status"),
                f.get("feature_score"), f.get("feature_confidence"), f.get("feature_weight"), f.get("feature_importance"),
                f.get("directional_effect"), _jsonb(f.get("feature_value_json")), now,
            ),
        )
        inserted_features += 1
    cur.execute(
        """
        INSERT INTO super_ghost_model_contributions (
            ledger_id, symbol, model_id, model_direction, model_confidence, model_action,
            model_edge_score, model_conviction_score, final_direction, final_action,
            created_at, updated_at, payload_json
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT (ledger_id, model_id) DO UPDATE SET
            model_direction = EXCLUDED.model_direction,
            model_confidence = EXCLUDED.model_confidence,
            model_action = EXCLUDED.model_action,
            model_edge_score = EXCLUDED.model_edge_score,
            model_conviction_score = EXCLUDED.model_conviction_score,
            final_direction = EXCLUDED.final_direction,
            final_action = EXCLUDED.final_action,
            updated_at = EXCLUDED.updated_at,
            payload_json = EXCLUDED.payload_json
        """,
        (
            ledger_id, symbol, DEFAULT_MODEL_ID, pred.get("direction"), _f(pred.get("confidence")), pred.get("action"),
            _f(pred.get("edge_score")), _f(pred.get("conviction_score")), pred.get("direction"), pred.get("action"),
            now, now, _jsonb({"prediction": pred, "coverage": report.get("coverage"), "risk_plan": report.get("risk_plan")}),
        ),
    )
    return {"ok": True, "features_logged": inserted_features, "model_id": DEFAULT_MODEL_ID}


def _alignment(feature_score: Optional[float], direction: str) -> str:
    if feature_score is None:
        return "missing"
    if abs(feature_score) <= 0.25:
        return "neutral"
    d = (direction or "HOLD").upper()
    sign = 1 if feature_score > 0 else -1
    if d == "UP":
        return "supports_prediction" if sign > 0 else "opposes_prediction"
    if d == "DOWN":
        return "supports_prediction" if sign < 0 else "opposes_prediction"
    return "directionless"


def classify_feature_outcome(*, feature_score: Optional[float], feature_available: bool, direction: str, prediction_correct: Optional[bool]) -> Tuple[str, str, str]:
    """Classify whether a feature helped/hurt/noised a resolved prediction."""
    if not feature_available or feature_score is None:
        return "missing", "missing", "Feature was unavailable; improve data coverage before trusting this signal."
    align = _alignment(feature_score, direction)
    if align == "neutral" or align == "directionless":
        return align, "noise", "Feature was neutral/directionless; treat as low signal until stronger evidence appears."
    if prediction_correct is True and align == "supports_prediction":
        return align, "helped", "Feature supported the correct direction; keep it as positive evidence."
    if prediction_correct is False and align == "supports_prediction":
        return align, "hurt", "Feature supported a wrong prediction; reduce trust/weight in similar setups."
    if prediction_correct is True and align == "opposes_prediction":
        return align, "noise", "Feature conflicted with a correct prediction; it may be noise or overruled by stronger drivers."
    if prediction_correct is False and align == "opposes_prediction":
        return align, "underweighted", "Feature warned against the final call; it may need more weight next time."
    return align, "noise", "Outcome indeterminate for this feature."


def score_features_from_ledger(*, symbol: Optional[str] = None, horizon: int = 5, limit: int = 1000) -> Dict[str, Any]:
    """Score stored prediction features against resolved ledger outcomes."""
    h = horizon if horizon in HORIZONS else 5
    correct_col = f"correct_{h}d"
    ret_col = f"return_{h}d_pct"
    resolved_col = f"resolved_{h}d_at"
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_memory_tables(cur)
            where = f"p.{resolved_col} IS NOT NULL AND p.{ret_col} IS NOT NULL"
            params: List[Any] = []
            if symbol:
                where += " AND p.symbol = %s"
                params.append(symbol.upper())
            cur.execute(
                f"""
                SELECT pf.ledger_id, pf.symbol, pf.feature_name, pf.feature_category,
                       pf.feature_available, pf.feature_score, pf.feature_importance,
                       p.direction, p.{ret_col}, p.{correct_col}
                FROM super_ghost_prediction_features pf
                JOIN super_ghost_predictions p ON p.id = pf.ledger_id
                WHERE {where}
                ORDER BY p.created_at DESC
                LIMIT %s
                """,
                params + [max(1, min(5000, int(limit)))],
            )
            rows = cur.fetchall()
            scored = 0
            now = _now()
            for r in rows:
                ledger_id, sym, fname, fcat, favail, fscore, fimp, direction, ret, correct = r
                align, effect, lesson = classify_feature_outcome(
                    feature_score=_f(fscore), feature_available=bool(favail), direction=direction, prediction_correct=correct
                )
                payload = {"alignment": align, "effect": effect, "lesson": lesson}
                cur.execute(
                    """
                    INSERT INTO super_ghost_feature_outcomes (
                        ledger_id, feature_name, symbol, horizon_days, prediction_direction,
                        realized_return_pct, prediction_correct, feature_score, feature_importance,
                        feature_alignment, outcome_effect, lesson, scored_at, payload_json
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (ledger_id, feature_name, horizon_days) DO UPDATE SET
                        realized_return_pct = EXCLUDED.realized_return_pct,
                        prediction_correct = EXCLUDED.prediction_correct,
                        feature_alignment = EXCLUDED.feature_alignment,
                        outcome_effect = EXCLUDED.outcome_effect,
                        lesson = EXCLUDED.lesson,
                        scored_at = EXCLUDED.scored_at,
                        payload_json = EXCLUDED.payload_json
                    """,
                    (ledger_id, fname, sym, h, direction, ret, correct, fscore, fimp, align, effect, lesson, now, _jsonb(payload)),
                )
                scored += 1
            _refresh_feature_profiles(cur, symbol=symbol, horizon=h)
            model_updates = _score_model_contributions(cur, symbol=symbol, horizon=h)
        return {"ok": True, "symbol": (symbol or "ALL").upper(), "horizon_days": h, "feature_outcomes_scored": scored, "model_contributions_scored": model_updates}
    except Exception as exc:
        LOGGER.warning("score_features_from_ledger: %s", str(exc)[:180])
        return {"ok": False, "error": str(exc)[:180], "feature_outcomes_scored": 0}


def _refresh_feature_profiles(cur, *, symbol: Optional[str], horizon: int) -> None:
    where = "horizon_days = %s"
    params: List[Any] = [horizon]
    if symbol:
        where += " AND symbol = %s"
        params.append(symbol.upper())
    cur.execute(
        f"""
        SELECT symbol, feature_name, horizon_days,
               COUNT(*) AS sample_count,
               SUM(CASE WHEN outcome_effect != 'missing' THEN 1 ELSE 0 END) AS available_count,
               SUM(CASE WHEN outcome_effect = 'helped' THEN 1 ELSE 0 END) AS helped_count,
               SUM(CASE WHEN outcome_effect = 'hurt' THEN 1 ELSE 0 END) AS hurt_count,
               SUM(CASE WHEN outcome_effect IN ('noise','underweighted') THEN 1 ELSE 0 END) AS noise_count,
               SUM(CASE WHEN outcome_effect = 'missing' THEN 1 ELSE 0 END) AS missing_count,
               AVG(feature_score) AS avg_score,
               AVG(feature_importance) AS avg_importance
        FROM super_ghost_feature_outcomes
        WHERE {where}
        GROUP BY symbol, feature_name, horizon_days
        """,
        params,
    )
    rows = cur.fetchall()
    now = _now()
    for r in rows:
        sym, fname, h, n, available, helped, hurt, noise, missing, avg_score, avg_imp = r
        denom = int(helped or 0) + int(hurt or 0)
        reliability = (float(helped) / denom) if denom else None
        if reliability is None:
            primary = "Insufficient helped/hurt history; keep collecting outcomes."
        elif reliability >= 0.60:
            primary = "Feature has helped more than hurt; keep as useful evidence in similar setups."
        elif reliability <= 0.40:
            primary = "Feature has hurt or misled often; reduce trust/weight in similar setups."
        else:
            primary = "Feature is mixed; use only with stronger confirming evidence."
        payload = {
            "sample_count": int(n or 0), "available_count": int(available or 0),
            "helped_count": int(helped or 0), "hurt_count": int(hurt or 0),
            "noise_count": int(noise or 0), "missing_count": int(missing or 0),
            "reliability": reliability, "avg_feature_score": _f(avg_score), "avg_feature_importance": _f(avg_imp),
        }
        cur.execute(
            """
            INSERT INTO super_ghost_feature_profiles (
                symbol, horizon_days, feature_name, sample_count, available_count, helped_count,
                hurt_count, noise_count, missing_count, reliability, avg_feature_score,
                avg_feature_importance, primary_lesson, updated_at, payload_json
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
            ON CONFLICT (symbol, horizon_days, feature_name) DO UPDATE SET
                sample_count=EXCLUDED.sample_count,
                available_count=EXCLUDED.available_count,
                helped_count=EXCLUDED.helped_count,
                hurt_count=EXCLUDED.hurt_count,
                noise_count=EXCLUDED.noise_count,
                missing_count=EXCLUDED.missing_count,
                reliability=EXCLUDED.reliability,
                avg_feature_score=EXCLUDED.avg_feature_score,
                avg_feature_importance=EXCLUDED.avg_feature_importance,
                primary_lesson=EXCLUDED.primary_lesson,
                updated_at=EXCLUDED.updated_at,
                payload_json=EXCLUDED.payload_json
            """,
            (sym, h, fname, int(n or 0), int(available or 0), int(helped or 0), int(hurt or 0), int(noise or 0), int(missing or 0), reliability, avg_score, avg_imp, primary, now, _jsonb(payload)),
        )


def _direction_correct(direction: str, ret_pct: Optional[float]) -> Optional[bool]:
    if ret_pct is None:
        return None
    d = (direction or "").upper()
    if d == "UP":
        return ret_pct > 0
    if d == "DOWN":
        return ret_pct < 0
    if d in ("HOLD", "SKIP", ""):
        return abs(ret_pct) < 3.0
    return None


def _signed_return(direction: str, ret_pct: Optional[float]) -> Optional[float]:
    if ret_pct is None:
        return None
    d = (direction or "").upper()
    if d == "UP":
        return float(ret_pct)
    if d == "DOWN":
        return -float(ret_pct)
    return 0.0


def _score_model_contributions(cur, *, symbol: Optional[str], horizon: int) -> int:
    """Score model contribution rows once outcomes resolve."""
    ret_col = f"return_{horizon}d_pct"
    resolved_col = f"resolved_{horizon}d_at"
    where = f"p.{resolved_col} IS NOT NULL AND p.{ret_col} IS NOT NULL"
    params: List[Any] = []
    if symbol:
        where += " AND p.symbol = %s"
        params.append(symbol.upper())
    cur.execute(
        f"""
        SELECT mc.ledger_id, mc.model_id, mc.model_direction, p.{ret_col}
        FROM super_ghost_model_contributions mc
        JOIN super_ghost_predictions p ON p.id = mc.ledger_id
        WHERE {where}
        """,
        params,
    )
    now = _now()
    updated = 0
    for ledger_id, model_id, direction, ret in cur.fetchall():
        correct = _direction_correct(direction, _f(ret))
        signed = _signed_return(direction, _f(ret))
        cur.execute(
            """
            UPDATE super_ghost_model_contributions
            SET horizon_days=%s, model_correct=%s, model_signed_return_pct=%s, updated_at=%s
            WHERE ledger_id=%s AND model_id=%s
            """,
            (horizon, correct, signed, now, ledger_id, model_id),
        )
        updated += 1
    return updated


def list_models() -> Dict[str, Any]:
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_memory_tables(cur)
            ensure_default_model(cur)
            cur.execute(
                """
                SELECT model_id, model_name, model_type, version, status, feature_set_id,
                       created_at, updated_at, notes, parameters_json
                FROM super_ghost_model_versions
                ORDER BY status='production' DESC, updated_at DESC
                """
            )
            rows = cur.fetchall()
        return {"ok": True, "models": [
            {"model_id": r[0], "model_name": r[1], "model_type": r[2], "version": r[3], "status": r[4], "feature_set_id": r[5], "created_at": r[6], "updated_at": r[7], "notes": r[8], "parameters": _coerce_json(r[9])}
            for r in rows
        ]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160], "models": []}


def recent_features(*, symbol: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_memory_tables(cur)
            where = "1=1"
            params: List[Any] = []
            if symbol:
                where += " AND symbol=%s"
                params.append(symbol.upper())
            cur.execute(
                f"""
                SELECT ledger_id, symbol, model_id, feature_name, feature_category,
                       feature_available, feature_status, feature_score, feature_importance,
                       directional_effect, feature_value_json, created_at
                FROM super_ghost_prediction_features
                WHERE {where}
                ORDER BY created_at DESC, ledger_id DESC
                LIMIT %s
                """,
                params + [max(1, min(1000, int(limit)))],
            )
            rows = cur.fetchall()
        return {"ok": True, "symbol": (symbol or "ALL").upper(), "count": len(rows), "features": [
            {"ledger_id": r[0], "symbol": r[1], "model_id": r[2], "feature_name": r[3], "feature_category": r[4], "available": r[5], "status": r[6], "score": r[7], "importance": r[8], "directional_effect": r[9], "value": _coerce_json(r[10]), "created_at": r[11]}
            for r in rows
        ]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160], "features": []}


def feature_profile(*, symbol: Optional[str] = None, horizon: int = 5, limit: int = 50) -> Dict[str, Any]:
    h = horizon if horizon in HORIZONS else 5
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_memory_tables(cur)
            where = "horizon_days=%s"
            params: List[Any] = [h]
            if symbol:
                where += " AND symbol=%s"
                params.append(symbol.upper())
            cur.execute(
                f"""
                SELECT symbol, horizon_days, feature_name, sample_count, available_count,
                       helped_count, hurt_count, noise_count, missing_count, reliability,
                       avg_feature_score, avg_feature_importance, primary_lesson, updated_at, payload_json
                FROM super_ghost_feature_profiles
                WHERE {where}
                ORDER BY reliability DESC NULLS LAST, sample_count DESC
                LIMIT %s
                """,
                params + [max(1, min(500, int(limit)))],
            )
            rows = cur.fetchall()
        return {"ok": True, "symbol": (symbol or "ALL").upper(), "horizon_days": h, "profiles": [
            {"symbol": r[0], "horizon_days": r[1], "feature_name": r[2], "sample_count": r[3], "available_count": r[4], "helped_count": r[5], "hurt_count": r[6], "noise_count": r[7], "missing_count": r[8], "reliability": r[9], "avg_feature_score": r[10], "avg_feature_importance": r[11], "primary_lesson": r[12], "updated_at": r[13], "payload": _coerce_json(r[14])}
            for r in rows
        ]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160], "profiles": []}
