"""core/research_artifacts.py — immutable research artifact registry (Phase 3).

Research artifacts (models, policies, ensembles) are stored by immutable
SHA-256 identity. An artifact row is append-only; lifecycle events use a
separate event table. No challenger is ever stored in ghost_v3_model until
explicit activation.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

LOGGER = logging.getLogger("ghost.research_artifacts")

# ── frozen types ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ArtifactMeta:
    """Immutable metadata for one research artifact."""
    artifact_sha: str           # SHA-256 of the serialized payload
    contract_id: str            # which contract this artifact targets
    policy_lineage_id: str      # stable lineage identifier across retrains
    policy_lineage_version: int  # monotonically increasing
    symbol_scope: Tuple[str, ...]  # symbols or ("__UNIVERSE__",) for pooled
    output_domain: Tuple[str, ...]
    feature_schema: str
    evidence_schema: str
    validation_schema: str
    horizon_bars: int
    training_manifest_sha: str
    calibration_proof: Dict[str, Any]  # from precision_gate.select_fire_threshold
    gate_proof: Dict[str, Any]
    feature_order: Tuple[str, ...]
    created_at: int = field(default_factory=lambda: int(time.time()))
    trained_at: int = 0
    status: str = "ACTIVE"      # ACTIVE | RETIRED | SUPERSEDED
    retired_at: int = 0
    retirement_reason: str = ""

    def __post_init__(self):
        if not self.artifact_sha or len(self.artifact_sha) != 64:
            raise ValueError("artifact_sha must be a 64-char hex string")
        if not self.contract_id:
            raise ValueError("contract_id is required")
        if self.policy_lineage_version < 1:
            raise ValueError("policy_lineage_version must be >= 1")


@dataclass(frozen=True)
class ArtifactLifecycleEvent:
    """One lifecycle state change for an artifact."""
    artifact_sha: str
    event_type: str             # REGISTERED | RETIRED | SUPERSEDED | ACTIVATED
    event_ts: int
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


# ── persistence ─────────────────────────────────────────────────────────────

def ensure_research_artifact_tables(cur) -> None:
    """Create research artifact tables if they don't exist."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_research_artifacts (
            artifact_sha TEXT PRIMARY KEY,
            contract_id TEXT NOT NULL,
            policy_lineage_id TEXT NOT NULL,
            policy_lineage_version INT NOT NULL,
            symbol_scope TEXT NOT NULL,
            output_domain TEXT NOT NULL,
            feature_schema TEXT NOT NULL,
            evidence_schema TEXT NOT NULL,
            validation_schema TEXT NOT NULL,
            horizon_bars INT NOT NULL,
            training_manifest_sha TEXT NOT NULL,
            calibration_proof JSONB,
            gate_proof JSONB,
            feature_order TEXT NOT NULL,
            payload_bytes TEXT,
            created_at BIGINT NOT NULL,
            trained_at BIGINT NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            retired_at BIGINT NOT NULL DEFAULT 0,
            retirement_reason TEXT DEFAULT ''
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_research_artifact_events (
            id SERIAL PRIMARY KEY,
            artifact_sha TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_ts BIGINT NOT NULL,
            reason TEXT DEFAULT '',
            metadata JSONB,
            created_at BIGINT NOT NULL
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_artifacts_contract "
        "ON ghost_research_artifacts (contract_id, status)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_artifacts_lineage "
        "ON ghost_research_artifacts (policy_lineage_id, policy_lineage_version DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_artifact_events_sha "
        "ON ghost_research_artifact_events (artifact_sha, event_ts DESC)"
    )


def register_artifact(
    meta: ArtifactMeta,
    payload_bytes: str = "",
    *,
    cur=None,
) -> bool:
    """Register a new artifact. Idempotent by artifact_sha.

    Also records a REGISTERED lifecycle event.
    """
    if cur is not None:
        return _register_artifact_impl(cur, meta, payload_bytes)
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        ensure_research_artifact_tables(c)
        result = _register_artifact_impl(c, meta, payload_bytes)
        conn.commit()
        return result


def _register_artifact_impl(cur, meta: ArtifactMeta, payload_bytes: str) -> bool:
    now = int(time.time())
    cur.execute(
        """
        INSERT INTO ghost_research_artifacts
            (artifact_sha, contract_id, policy_lineage_id, policy_lineage_version,
             symbol_scope, output_domain, feature_schema, evidence_schema,
             validation_schema, horizon_bars, training_manifest_sha,
             calibration_proof, gate_proof, feature_order, payload_bytes,
             created_at, trained_at, status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (artifact_sha) DO NOTHING
        """,
        (
            meta.artifact_sha,
            meta.contract_id,
            meta.policy_lineage_id,
            meta.policy_lineage_version,
            json.dumps(list(meta.symbol_scope)),
            json.dumps(list(meta.output_domain)),
            meta.feature_schema,
            meta.evidence_schema,
            meta.validation_schema,
            meta.horizon_bars,
            meta.training_manifest_sha,
            json.dumps(meta.calibration_proof) if meta.calibration_proof else None,
            json.dumps(meta.gate_proof) if meta.gate_proof else None,
            json.dumps(list(meta.feature_order)),
            payload_bytes or None,
            now,
            meta.trained_at or now,
            meta.status,
        ),
    )
    inserted = cur.rowcount > 0
    if inserted:
        _record_lifecycle_event(cur, meta.artifact_sha, "REGISTERED", now, "")
    return inserted


def _record_lifecycle_event(
    cur, artifact_sha: str, event_type: str, event_ts: int, reason: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    cur.execute(
        """
        INSERT INTO ghost_research_artifact_events
            (artifact_sha, event_type, event_ts, reason, metadata, created_at)
        VALUES (%s,%s,%s,%s,%s,%s)
        """,
        (
            artifact_sha,
            event_type,
            event_ts,
            reason,
            json.dumps(metadata or {}),
            int(time.time()),
        ),
    )


def retire_artifact(
    artifact_sha: str,
    reason: str,
    *,
    cur=None,
) -> bool:
    """Mark an artifact as RETIRED. Append-only — never deletes the row."""
    if cur is not None:
        return _retire_artifact_impl(cur, artifact_sha, reason)
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        result = _retire_artifact_impl(c, artifact_sha, reason)
        conn.commit()
        return result


def _retire_artifact_impl(cur, artifact_sha: str, reason: str) -> bool:
    now = int(time.time())
    cur.execute(
        """
        UPDATE ghost_research_artifacts
        SET status = 'RETIRED', retired_at = %s, retirement_reason = %s
        WHERE artifact_sha = %s AND status = 'ACTIVE'
        """,
        (now, reason, artifact_sha),
    )
    updated = cur.rowcount > 0
    if updated:
        _record_lifecycle_event(cur, artifact_sha, "RETIRED", now, reason)
    return updated


def get_artifact(artifact_sha: str, cur=None) -> Optional[Dict[str, Any]]:
    """Load artifact metadata by SHA."""
    if cur is not None:
        return _get_artifact_impl(cur, artifact_sha)
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        return _get_artifact_impl(c, artifact_sha)


def _get_artifact_impl(cur, artifact_sha: str) -> Optional[Dict[str, Any]]:
    cur.execute(
        """
        SELECT artifact_sha, contract_id, policy_lineage_id, policy_lineage_version,
               symbol_scope, output_domain, feature_schema, evidence_schema,
               validation_schema, horizon_bars, training_manifest_sha,
               calibration_proof, gate_proof, feature_order, payload_bytes,
               created_at, trained_at, status, retired_at, retirement_reason
        FROM ghost_research_artifacts
        WHERE artifact_sha = %s
        """,
        (artifact_sha,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "artifact_sha": row[0],
        "contract_id": row[1],
        "policy_lineage_id": row[2],
        "policy_lineage_version": row[3],
        "symbol_scope": tuple(json.loads(row[4]) if isinstance(row[4], str) else row[4]),
        "output_domain": tuple(json.loads(row[5]) if isinstance(row[5], str) else row[5]),
        "feature_schema": row[6],
        "evidence_schema": row[7],
        "validation_schema": row[8],
        "horizon_bars": row[9],
        "training_manifest_sha": row[10],
        "calibration_proof": _coerce_json(row[11]),
        "gate_proof": _coerce_json(row[12]),
        "feature_order": tuple(json.loads(row[13]) if isinstance(row[13], str) else row[13]),
        "payload_bytes": row[14],
        "created_at": row[15],
        "trained_at": row[16],
        "status": row[17],
        "retired_at": row[18],
        "retirement_reason": row[19],
    }


def list_artifacts(
    contract_id: Optional[str] = None,
    status: str = "ACTIVE",
    cur=None,
) -> List[Dict[str, Any]]:
    """List artifacts, optionally filtered by contract and status."""
    if cur is not None:
        return _list_artifacts_impl(cur, contract_id, status)
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        return _list_artifacts_impl(c, contract_id, status)


def _list_artifacts_impl(cur, contract_id, status) -> List[Dict[str, Any]]:
    where = "WHERE status = %s"
    params: List[Any] = [status]
    if contract_id:
        where += " AND contract_id = %s"
        params.append(contract_id)
    cur.execute(
        f"""
        SELECT artifact_sha, contract_id, policy_lineage_id, policy_lineage_version,
               symbol_scope, output_domain, feature_schema, evidence_schema,
               validation_schema, horizon_bars, training_manifest_sha,
               calibration_proof, gate_proof, feature_order,
               created_at, trained_at, status
        FROM ghost_research_artifacts
        {where}
        ORDER BY created_at DESC
        LIMIT 200
        """,
        params,
    )
    return [
        {
            "artifact_sha": r[0],
            "contract_id": r[1],
            "policy_lineage_id": r[2],
            "policy_lineage_version": r[3],
            "symbol_scope": tuple(json.loads(r[4]) if isinstance(r[4], str) else r[4]),
            "output_domain": tuple(json.loads(r[5]) if isinstance(r[5], str) else r[5]),
            "feature_schema": r[6],
            "evidence_schema": r[7],
            "validation_schema": r[8],
            "horizon_bars": r[9],
            "training_manifest_sha": r[10],
            "calibration_proof": _coerce_json(r[11]),
            "gate_proof": _coerce_json(r[12]),
            "feature_order": tuple(json.loads(r[13]) if isinstance(r[13], str) else r[13]),
            "created_at": r[14],
            "trained_at": r[15],
            "status": r[16],
        }
        for r in cur.fetchall()
    ]


def get_lifecycle_events(artifact_sha: str, cur=None) -> List[Dict[str, Any]]:
    """Get lifecycle events for an artifact."""
    if cur is not None:
        return _get_lifecycle_impl(cur, artifact_sha)
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        return _get_lifecycle_impl(c, artifact_sha)


def _get_lifecycle_impl(cur, artifact_sha: str) -> List[Dict[str, Any]]:
    cur.execute(
        """
        SELECT event_type, event_ts, reason, metadata
        FROM ghost_research_artifact_events
        WHERE artifact_sha = %s
        ORDER BY event_ts ASC
        """,
        (artifact_sha,),
    )
    return [
        {
            "event_type": r[0],
            "event_ts": r[1],
            "reason": r[2],
            "metadata": _coerce_json(r[3]),
        }
        for r in cur.fetchall()
    ]


def _coerce_json(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return v


def compute_artifact_sha(
    model_sha256: str,
    contract_id: str,
    direction: str,
    policy_lineage_id: str,
    policy_lineage_version: int,
    feature_order: Tuple[str, ...],
    feature_schema: str = "",
    label_schema: str = "",
    validation_schema: str = "",
    hold_bars: int = 0,
    training_manifest_sha: str = "",
    calibration_proof: Optional[Dict[str, Any]] = None,
    gate_proof: Optional[Dict[str, Any]] = None,
) -> str:
    """Deterministic artifact package SHA from its complete identity.

    Binds model bytes (via model_sha256), contract, direction, ordered features,
    all schemas, training manifest, and calibration/gate proof. Any change to
    any of these produces a new artifact identity.
    """
    canonical: Dict[str, Any] = {
        "model_sha256": model_sha256,
        "contract_id": contract_id,
        "direction": direction,
        "policy_lineage_id": policy_lineage_id,
        "policy_lineage_version": policy_lineage_version,
        "feature_order": list(feature_order),  # preserve order, don't sort
        "feature_schema": feature_schema,
        "label_schema": label_schema,
        "validation_schema": validation_schema,
        "hold_bars": hold_bars,
        "training_manifest_sha": training_manifest_sha,
    }
    if calibration_proof:
        canonical["calibration_proof"] = calibration_proof
    if gate_proof:
        canonical["gate_proof"] = gate_proof
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def compute_model_sha256(raw_model_bytes: bytes) -> str:
    """SHA-256 of the raw deserialized model bytes.

    This must match what signal_engine._load_model_uncached() recomputes
    after base64 decoding the stored payload.
    """
    return hashlib.sha256(raw_model_bytes).hexdigest()
