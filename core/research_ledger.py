"""core/research_ledger.py — isolated research evidence ledger.

Every research prediction is appended exactly once with full contract/artifact
identity. Resolutions are appended exactly once per prediction and write an
outbox row in the same transaction. No updates to prediction truth rows.
Research tables are physically separate from live prediction, shadow, wallet,
and Super Ghost tables.

Schema is owned by core/research_schema.py and created at startup via
core.db._migrate_schema(). This module never calls DDL.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("ghost.research_ledger")


# ── Eligible evaluation logging ─────────────────────────────────────────────

def log_research_evaluation(
    *,
    contract_id: str,
    artifact_sha: str,
    symbol: str,
    direction: str,
    evaluation_date: str,
    evaluated_ts: int,
    feature_available_ts: int,
    calibrated_prob: float,
    threshold: float,
    eligible: bool,
    fired: bool,
    reason: str,
    metadata: Optional[Dict[str, Any]] = None,
    cur=None,
) -> bool:
    """Append one immutable eligible-score opportunity.

    Returns True for the first row and False for any same-day retry. The unique
    key makes the first valid evaluation authoritative; later scans cannot
    rewrite its probability, threshold decision, or fire state.
    """
    if cur is not None:
        return _log_evaluation_impl(
            cur, contract_id, artifact_sha, symbol, direction,
            evaluation_date, evaluated_ts, feature_available_ts,
            calibrated_prob, threshold, eligible, fired, reason, metadata,
        )
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        inserted = _log_evaluation_impl(
            c, contract_id, artifact_sha, symbol, direction,
            evaluation_date, evaluated_ts, feature_available_ts,
            calibrated_prob, threshold, eligible, fired, reason, metadata,
        )
        conn.commit()
        return inserted


def _log_evaluation_impl(
    cur, contract_id, artifact_sha, symbol, direction, evaluation_date,
    evaluated_ts, feature_available_ts, calibrated_prob, threshold,
    eligible, fired, reason, metadata,
) -> bool:
    if feature_available_ts > evaluated_ts:
        raise ValueError(
            f"feature_available_ts ({feature_available_ts}) > evaluated_ts ({evaluated_ts})"
        )
    if not 0.0 <= calibrated_prob <= 1.0:
        raise ValueError(f"calibrated_prob {calibrated_prob} not in [0,1]")
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(f"threshold {threshold} not in [0,1]")
    if fired and not eligible:
        raise ValueError("an ineligible evaluation cannot fire")

    cur.execute(
        """
        SELECT direction, evaluated_ts, feature_available_ts, calibrated_prob,
               threshold, eligible, fired, reason, metadata
        FROM ghost_research_evaluations
        WHERE contract_id = %s AND artifact_sha = %s
          AND symbol = %s AND evaluation_date = %s
        """,
        (contract_id, artifact_sha, symbol, evaluation_date),
    )
    existing = cur.fetchone()
    expected_metadata = metadata or {}
    if existing:
        return False

    cur.execute(
        """
        INSERT INTO ghost_research_evaluations
            (contract_id, artifact_sha, symbol, direction, evaluation_date,
             evaluated_ts, feature_available_ts, calibrated_prob, threshold,
             eligible, fired, reason, metadata, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (contract_id, artifact_sha, symbol, evaluation_date)
        DO NOTHING
        """,
        (
            contract_id, artifact_sha, symbol, direction, evaluation_date,
            evaluated_ts, feature_available_ts, calibrated_prob, threshold,
            eligible, fired, reason, json.dumps(expected_metadata), int(time.time()),
        ),
    )
    return cur.rowcount > 0


# ── Prediction logging ──────────────────────────────────────────────────────

def log_research_prediction(
    *,
    contract_id: str,
    artifact_sha: str,
    policy_lineage_id: str,
    symbol: str,
    direction: str,
    issued_ts: int,
    feature_available_ts: int,
    output: str,
    calibrated_prob: Optional[float] = None,
    threshold: Optional[float] = None,
    source_snapshot_sha: str = "",
    feature_snapshot_sha: str = "",
    selector_decision: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
    cur=None,
) -> Optional[int]:
    """Append one research prediction. Returns the prediction ID or None.

    Idempotent by (contract_id, artifact_sha, symbol, issued_ts).
    A duplicate with identical immutable fields returns the existing ID.
    A conflicting duplicate (same key, different fields) raises ValueError.
    """
    if cur is not None:
        return _log_pred_impl(
            cur, contract_id, artifact_sha, policy_lineage_id, symbol,
            direction, issued_ts, feature_available_ts, output,
            calibrated_prob, threshold, source_snapshot_sha,
            feature_snapshot_sha, selector_decision, context,
        )
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        pred_id = _log_pred_impl(
            c, contract_id, artifact_sha, policy_lineage_id, symbol,
            direction, issued_ts, feature_available_ts, output,
            calibrated_prob, threshold, source_snapshot_sha,
            feature_snapshot_sha, selector_decision, context,
        )
        conn.commit()
        return pred_id


def _log_pred_impl(
    cur, contract_id, artifact_sha, policy_lineage_id, symbol,
    direction, issued_ts, feature_available_ts, output,
    calibrated_prob, threshold, source_snapshot_sha,
    feature_snapshot_sha, selector_decision, context,
) -> Optional[int]:
    # Validate chronology
    if feature_available_ts > issued_ts:
        raise ValueError(
            f"feature_available_ts ({feature_available_ts}) > issued_ts ({issued_ts})"
        )
    # Validate probability range
    if calibrated_prob is not None and not (0.0 <= calibrated_prob <= 1.0):
        raise ValueError(f"calibrated_prob {calibrated_prob} not in [0,1]")
    if threshold is not None and not (0.0 <= threshold <= 1.0):
        raise ValueError(f"threshold {threshold} not in [0,1]")

    now = int(time.time())
    # Check for existing row with same key
    cur.execute(
        """
        SELECT id, output, calibrated_prob, threshold,
               source_snapshot_sha, feature_snapshot_sha
        FROM ghost_research_predictions
        WHERE contract_id = %s AND artifact_sha = %s
          AND symbol = %s AND issued_ts = %s
        """,
        (contract_id, artifact_sha, symbol, issued_ts),
    )
    existing = cur.fetchone()
    if existing:
        existing_output = str(existing[1] or "")
        existing_cp = existing[2]
        existing_thr = existing[3]
        existing_ss = str(existing[4] or "")
        existing_fs = str(existing[5] or "")
        if (existing_output != output
                or existing_cp != calibrated_prob
                or existing_thr != threshold
                or existing_ss != (source_snapshot_sha or "")
                or existing_fs != (feature_snapshot_sha or "")):
            raise ValueError(
                f"Conflicting duplicate prediction for "
                f"{contract_id}/{artifact_sha}/{symbol}/{issued_ts}"
            )
        return int(existing[0])

    cur.execute(
        """
        INSERT INTO ghost_research_predictions
            (contract_id, artifact_sha, policy_lineage_id, symbol,
             direction, issued_ts, feature_available_ts, output,
             calibrated_prob, threshold,
             source_snapshot_sha, feature_snapshot_sha,
             selector_decision, context, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (contract_id, artifact_sha, symbol, issued_ts) DO NOTHING
        RETURNING id
        """,
        (
            contract_id, artifact_sha, policy_lineage_id, symbol,
            direction, issued_ts, feature_available_ts, output,
            calibrated_prob, threshold,
            source_snapshot_sha or "", feature_snapshot_sha or "",
            json.dumps(selector_decision or {}),
            json.dumps(context or {}),
            now,
        ),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


# ── Resolution logging ──────────────────────────────────────────────────────

def resolve_research_prediction(
    *,
    prediction_id: int,
    resolver_id: str,
    resolver_version: str,
    outcome: str,
    observed_value: Optional[float] = None,
    resolved_ts: int = 0,
    evidence_available_ts: int = 0,
    evidence_payload: Optional[Dict[str, Any]] = None,
    evidence_sha: str = "",
    reason: str = "",
    cur=None,
) -> bool:
    """Append one resolution and an outbox row in the same transaction.

    Idempotent by prediction_id. Returns True if a new resolution was inserted.
    Contract and artifact identity are derived from the prediction row.
    """
    if cur is not None:
        return _resolve_impl(
            cur, prediction_id, resolver_id, resolver_version, outcome,
            observed_value, resolved_ts, evidence_available_ts,
            evidence_payload, evidence_sha, reason,
        )
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        result = _resolve_impl(
            c, prediction_id, resolver_id, resolver_version, outcome,
            observed_value, resolved_ts, evidence_available_ts,
            evidence_payload, evidence_sha, reason,
        )
        conn.commit()
        return result


def _resolve_impl(
    cur, prediction_id, resolver_id, resolver_version, outcome,
    observed_value, resolved_ts, evidence_available_ts,
    evidence_payload, evidence_sha, reason,
) -> bool:
    valid_outcomes = {"WIN", "LOSS", "EXPIRED", "DATA_INVALID"}
    if outcome.upper() not in valid_outcomes:
        raise ValueError(f"Invalid outcome '{outcome}'. Must be one of {valid_outcomes}")
    outcome = outcome.upper()

    # Verify prediction exists and capture issued_ts for chronology check
    cur.execute(
        "SELECT issued_ts FROM ghost_research_predictions WHERE id = %s",
        (prediction_id,),
    )
    pred_row = cur.fetchone()
    if not pred_row:
        raise ValueError(f"Prediction {prediction_id} not found")
    pred_issued_ts = int(pred_row[0])

    now = int(time.time())
    actual_resolved_ts = resolved_ts or now
    actual_available_ts = evidence_available_ts or now
    if actual_resolved_ts <= pred_issued_ts:
        raise ValueError(
            f"resolved_ts ({actual_resolved_ts}) <= issued_ts ({pred_issued_ts})"
        )
    if actual_available_ts < actual_resolved_ts:
        raise ValueError(
            f"evidence_available_ts ({actual_available_ts}) < resolved_ts ({actual_resolved_ts})"
        )

    cur.execute(
        """
        INSERT INTO ghost_research_resolutions
            (prediction_id, resolver_id, resolver_version, outcome,
             observed_value, resolved_ts, evidence_available_ts,
             evidence_payload, evidence_sha, reason, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (prediction_id) DO NOTHING
        """,
        (
            prediction_id, resolver_id, resolver_version, outcome,
            observed_value, actual_resolved_ts, actual_available_ts,
            json.dumps(evidence_payload or {}),
            evidence_sha or "", reason, now,
        ),
    )
    inserted = cur.rowcount > 0

    if inserted:
        cur.execute(
            "SELECT id FROM ghost_research_resolutions WHERE prediction_id = %s",
            (prediction_id,),
        )
        res_row = cur.fetchone()
        if res_row:
            resolution_id = int(res_row[0])
            cur.execute(
                """
                INSERT INTO ghost_research_outbox
                    (prediction_id, resolution_id, status, created_at)
                VALUES (%s, %s, 'PENDING', %s)
                ON CONFLICT (prediction_id) DO NOTHING
                """,
                (prediction_id, resolution_id, now),
            )

    return inserted


# ── Query helpers ────────────────────────────────────────────────────────────

def get_pending_predictions(
    contract_id: Optional[str] = None,
    limit: int = 200,
    cur=None,
) -> List[Dict[str, Any]]:
    """Get unresolved research predictions using an indexed anti-join."""
    if cur is not None:
        return _get_pending_impl(cur, contract_id, limit)
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        return _get_pending_impl(c, contract_id, limit)


def _get_pending_impl(cur, contract_id, limit) -> List[Dict[str, Any]]:
    where = "WHERE r.prediction_id IS NULL"
    params: List[Any] = []
    if contract_id:
        where += " AND p.contract_id = %s"
        params.append(contract_id)
    cur.execute(
        f"""
        SELECT p.id, p.contract_id, p.artifact_sha, p.symbol, p.direction,
             p.issued_ts, p.output, p.calibrated_prob, p.threshold, p.context
        FROM ghost_research_predictions p
        LEFT JOIN ghost_research_resolutions r ON r.prediction_id = p.id
        {where}
        ORDER BY p.issued_ts ASC
        LIMIT %s
        """,
        params + [limit],
    )
    rows = []
    for r in cur.fetchall():
        context = r[9]
        if isinstance(context, str):
            try:
                context = json.loads(context)
            except (TypeError, ValueError):
                context = {}
        if not isinstance(context, dict):
            context = {}
        rows.append({
            "id": r[0], "contract_id": r[1], "artifact_sha": r[2],
            "symbol": r[3], "direction": r[4], "issued_ts": r[5],
            "output": r[6], "calibrated_prob": r[7], "threshold": r[8],
            "context": context,
        })
    return rows


def get_resolved_predictions(
    contract_id: Optional[str] = None,
    artifact_sha: Optional[str] = None,
    limit: int = 200,
    cur=None,
) -> List[Dict[str, Any]]:
    """Get resolved research predictions with their outcomes."""
    if cur is not None:
        return _get_resolved_impl(cur, contract_id, artifact_sha, limit)
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        return _get_resolved_impl(c, contract_id, artifact_sha, limit)


def _get_resolved_impl(cur, contract_id, artifact_sha, limit) -> List[Dict[str, Any]]:
    where = "WHERE r.prediction_id IS NOT NULL"
    params: List[Any] = []
    if contract_id:
        where += " AND p.contract_id = %s"
        params.append(contract_id)
    if artifact_sha:
        where += " AND p.artifact_sha = %s"
        params.append(artifact_sha)
    cur.execute(
        f"""
        SELECT p.id, p.contract_id, p.artifact_sha, p.symbol, p.direction,
               p.issued_ts, p.output, p.calibrated_prob, p.threshold,
               r.outcome, r.observed_value, r.resolved_ts, r.reason
        FROM ghost_research_predictions p
        JOIN ghost_research_resolutions r ON r.prediction_id = p.id
        {where}
        ORDER BY p.issued_ts DESC
        LIMIT %s
        """,
        params + [limit],
    )
    return [
        {
            "id": r[0], "contract_id": r[1], "artifact_sha": r[2],
            "symbol": r[3], "direction": r[4], "issued_ts": r[5],
            "output": r[6], "calibrated_prob": r[7], "threshold": r[8],
            "outcome": r[9], "observed_value": r[10],
            "resolved_ts": r[11], "reason": r[12],
        }
        for r in cur.fetchall()
    ]


def get_outbox_pending(limit: int = 50, cur=None) -> List[Dict[str, Any]]:
    """Get pending outbox rows for processing."""
    if cur is not None:
        return _get_outbox_impl(cur, limit)
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        return _get_outbox_impl(c, limit)


def _get_outbox_impl(cur, limit) -> List[Dict[str, Any]]:
    cur.execute(
        """
        SELECT o.id, o.prediction_id, o.resolution_id, o.status,
               o.attempt_count, o.created_at
        FROM ghost_research_outbox o
        WHERE o.status = 'PENDING'
        ORDER BY o.created_at ASC
        LIMIT %s
        """,
        (limit,),
    )
    return [
        {
            "id": r[0], "prediction_id": r[1], "resolution_id": r[2],
            "status": r[3], "attempt_count": r[4], "created_at": r[5],
        }
        for r in cur.fetchall()
    ]


def mark_outbox_processed(outbox_id: int, cur=None) -> bool:
    """Mark an outbox row as PROCESSED."""
    if cur is not None:
        return _mark_outbox_impl(cur, outbox_id)
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        result = _mark_outbox_impl(c, outbox_id)
        conn.commit()
        return result


def _mark_outbox_impl(cur, outbox_id) -> bool:
    now = int(time.time())
    cur.execute(
        """
        UPDATE ghost_research_outbox
        SET status = 'PROCESSED', processed_at = %s
        WHERE id = %s AND status = 'PENDING'
        """,
        (now, outbox_id),
    )
    return cur.rowcount > 0


def mark_outbox_failed(outbox_id: int, error: str, cur=None) -> bool:
    """Increment attempt count and record error. Marks DEAD after 10 attempts."""
    if cur is not None:
        return _mark_failed_impl(cur, outbox_id, error)
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        result = _mark_failed_impl(c, outbox_id, error)
        conn.commit()
        return result


def _mark_failed_impl(cur, outbox_id, error) -> bool:
    cur.execute(
        """
        UPDATE ghost_research_outbox
        SET attempt_count = attempt_count + 1,
            last_error = %s,
            status = CASE WHEN attempt_count >= 9 THEN 'DEAD' ELSE 'PENDING' END
        WHERE id = %s
        """,
        (error[:500], outbox_id),
    )
    return cur.rowcount > 0
