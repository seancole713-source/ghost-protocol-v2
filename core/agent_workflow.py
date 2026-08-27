"""Durable, advisory-only workflow between Ghost and connected research agents.

Ghost creates bounded research tasks. Authenticated agents claim tasks with a
short lease and submit structured, source-backed evidence. Nothing in this
module imports prediction, alert, portfolio, or order-execution code: accepted
evidence remains advisory until a separate deterministic research contract
promotes it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

TASK_STATUSES = frozenset(
    {"PENDING", "CLAIMED", "COMPLETED", "CANCELLED", "DEAD_LETTER", "EXPIRED"}
)
ACTIVE_TASK_STATUSES = frozenset({"PENDING", "CLAIMED"})
VALIDATION_STATUSES = frozenset({"ACCEPTED", "QUARANTINED"})
QUARANTINE_CATEGORIES = frozenset(
    {"none", "schema_error", "source_error", "injection_suspected", "policy_violation"}
)
REPAIRABLE_QUARANTINE_CATEGORIES = frozenset({"schema_error", "source_error"})
WORKER_STATUSES = frozenset({"STARTING", "IDLE", "WORKING", "ERROR", "STOPPED"})

_TASK_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,19}$")
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/\-]{1,127}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/\-]{2,199}$")
_TASK_ID_RE = re.compile(r"^agt_[0-9a-f]{32}$")
_EVIDENCE_ID_RE = re.compile(r"^evd_[0-9a-f]{32}$")
_PROMPT_INJECTION_PATTERNS = (
    "ignore previous instructions",
    "ignore all previous instructions",
    "reveal your system prompt",
    "print the system prompt",
    "reveal secrets",
    "execute this shell command",
)

_MAX_REQUEST_BYTES = 32_768
_MAX_SCHEMA_BYTES = 32_768
_MAX_CLAIMS_BYTES = 65_536
_MAX_RAW_RESPONSE_BYTES = 131_072
_MAX_SOURCES = 25
_MAX_SUMMARY_CHARS = 8_000
_MIN_LEASE_SECONDS = 60
_MAX_LEASE_SECONDS = 3_600

SUBMISSION_CONTRACT_VERSION = "ghost.agent-evidence/v2"

DEFAULT_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["verdict", "evidence", "risks", "recommended_next_step"],
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["supports", "rejects", "mixed", "insufficient"],
        },
        "evidence": {"type": "array", "minItems": 1, "items": {"type": "object"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "recommended_next_step": {"type": "string", "minLength": 1},
    },
    "additionalProperties": True,
}

_TASK_COLUMNS = (
    "task_id", "idempotency_key", "task_type", "symbol", "priority", "status",
    "requested_by", "request_payload", "required_response_schema",
    "required_submissions", "available_at", "deadline_at", "created_at", "updated_at",
    "claimed_by", "lease_expires_at", "attempt_count", "max_attempts", "last_error",
    "completed_at", "advisory_only", "decision_eligible",
)
_TASK_SELECT = ", ".join(_TASK_COLUMNS)

_EVIDENCE_COLUMNS = (
    "evidence_id", "task_id", "agent_id", "agent_provider", "model_name",
    "prompt_version", "submitted_at", "summary", "claims", "source_refs",
    "agent_confidence", "payload_sha256", "raw_response", "validation_status",
    "validation_reasons", "quarantine_category", "validation_errors", "lease_id",
    "repair_of_evidence_id", "advisory_only", "decision_eligible",
)


class AgentWorkflowError(ValueError):
    """Expected workflow validation or state-transition failure."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _json_size(value: Any) -> int:
    return len(_canonical_json(value).encode("utf-8"))


def _json_depth(value: Any) -> int:
    max_depth = 0
    stack = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        max_depth = max(max_depth, depth)
        if max_depth > 20:
            return max_depth
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
    return max_depth


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _row_dict(row: Any, columns: Sequence[str]) -> Dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, Mapping):
        return {column: row.get(column) for column in columns}
    return dict(zip(columns, row))


def _public_task(row: Any) -> Dict[str, Any]:
    task = _row_dict(row, _TASK_COLUMNS)
    if not task:
        return {}
    task["advisory_only"] = True
    task["decision_eligible"] = False
    return task


def _public_evidence(row: Any) -> Dict[str, Any]:
    evidence = _row_dict(row, _EVIDENCE_COLUMNS)
    if not evidence:
        return {}
    evidence["advisory_only"] = True
    evidence["decision_eligible"] = False
    return evidence


def _bounded_json(value: Any, *, max_bytes: int, field: str) -> Any:
    if not isinstance(value, (dict, list)):
        raise AgentWorkflowError(f"{field} must be a JSON object or array")
    if _json_depth(value) > 20:
        raise AgentWorkflowError(f"{field} exceeds maximum nesting depth")
    if _json_size(value) > max_bytes:
        raise AgentWorkflowError(f"{field} exceeds {max_bytes} bytes")
    return value


def _normalize_task_type(value: Any) -> str:
    task_type = str(value or "").strip().lower()
    if not _TASK_TYPE_RE.fullmatch(task_type):
        raise AgentWorkflowError("invalid task_type")
    return task_type


def _normalize_symbol(value: Any) -> Optional[str]:
    if value is None or str(value).strip() == "":
        return None
    symbol = str(value).strip().upper()
    if not _SYMBOL_RE.fullmatch(symbol):
        raise AgentWorkflowError("invalid symbol")
    return symbol


def _normalize_agent_id(value: Any) -> str:
    agent_id = str(value or "").strip()
    if not _AGENT_ID_RE.fullmatch(agent_id):
        raise AgentWorkflowError("invalid agent_id")
    return agent_id


def _normalize_task_id(value: Any) -> str:
    task_id = str(value or "").strip().lower()
    if not _TASK_ID_RE.fullmatch(task_id):
        raise AgentWorkflowError("invalid task_id")
    return task_id


def _normalize_evidence_id(value: Any) -> str:
    evidence_id = str(value or "").strip().lower()
    if not _EVIDENCE_ID_RE.fullmatch(evidence_id):
        raise AgentWorkflowError("invalid evidence_id")
    return evidence_id


def _lease_seconds(value: Any) -> int:
    try:
        lease = int(value)
    except (TypeError, ValueError):
        raise AgentWorkflowError("lease_seconds must be an integer")
    if lease < _MIN_LEASE_SECONDS or lease > _MAX_LEASE_SECONDS:
        raise AgentWorkflowError(
            f"lease_seconds must be between {_MIN_LEASE_SECONDS} and {_MAX_LEASE_SECONDS}"
        )
    return lease


def _safe_raw_response(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, (dict, list, str)):
        value = str(value)
    encoded = _canonical_json(value).encode("utf-8")
    if len(encoded) <= _MAX_RAW_RESPONSE_BYTES:
        return value
    return {
        "truncated": True,
        "payload_sha256": hashlib.sha256(encoded).hexdigest(),
        "original_bytes": len(encoded),
    }


def _example_for_schema(schema: Any) -> Any:
    """Build a small deterministic example for the supported schema subset."""
    if not isinstance(schema, dict):
        return None
    if schema.get("enum"):
        return schema["enum"][0]
    expected = schema.get("type")
    if expected == "object":
        raw_properties = schema.get("properties")
        properties: Dict[str, Any] = raw_properties if isinstance(raw_properties, dict) else {}
        raw_required = schema.get("required")
        required: List[Any] = raw_required if isinstance(raw_required, list) else []
        fields = required or list(properties)[:4]
        return {
            str(field): _example_for_schema(properties.get(field, {}))
            for field in fields
        }
    if expected == "array":
        return [_example_for_schema(schema.get("items", {}))]
    if expected == "string":
        return "source-backed finding"
    if expected == "number":
        return 0.75
    if expected == "integer":
        return 1
    if expected == "boolean":
        return True
    return None


def submission_contract(response_schema: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the exact envelope agents must submit, including a valid example."""
    schema = response_schema or DEFAULT_RESPONSE_SCHEMA
    return {
        "version": SUBMISSION_CONTRACT_VERSION,
        "required_response_schema": schema,
        "submission_fields": {
            "summary": "non-empty string, at most 8000 characters",
            "claims": "object matching required_response_schema",
            "source_refs": "one or more {kind, locator} objects",
            "agent_confidence": "optional number from 0 through 1",
            "raw_response": "optional bounded audit payload",
            "repair_of_evidence_id": "required on a corrected resubmission",
        },
        "submission_example": {
            "summary": "Official and independent sources support a mixed catalyst verdict.",
            "claims": _example_for_schema(schema),
            "source_refs": [
                {
                    "kind": "official_release",
                    "locator": "https://example.com/investor-relations/release",
                    "retrieved_ts": 1_800_000_000,
                }
            ],
            "agent_confidence": 0.75,
        },
        "repair_policy": {
            "repairable_categories": sorted(REPAIRABLE_QUARANTINE_CATEGORIES),
            "lease_retained_for_repair": True,
            "instruction": (
                "When accepted=false and retry_allowed=true, correct only the listed "
                "validation_errors and resubmit with repair_of_evidence_id before lease expiry."
            ),
        },
        "safety": {"advisory_only": True, "decision_eligible": False},
    }


def ensure_agent_workflow_tables(cur) -> None:
    """Create the queue and immutable audit tables additively."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_agent_tasks (
            task_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            task_type TEXT NOT NULL,
            symbol VARCHAR(20),
            priority INT NOT NULL DEFAULT 50 CHECK (priority BETWEEN 0 AND 100),
            status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK (status IN ('PENDING','CLAIMED','COMPLETED','CANCELLED','DEAD_LETTER','EXPIRED')),
            requested_by TEXT NOT NULL,
            request_payload JSONB NOT NULL,
            required_response_schema JSONB NOT NULL,
            required_submissions INT NOT NULL DEFAULT 1 CHECK (required_submissions BETWEEN 1 AND 5),
            available_at BIGINT NOT NULL,
            deadline_at BIGINT,
            created_at BIGINT NOT NULL,
            updated_at BIGINT NOT NULL,
            claimed_by TEXT,
            lease_token_sha256 CHAR(64),
            lease_id TEXT,
            lease_expires_at BIGINT,
            attempt_count INT NOT NULL DEFAULT 0,
            max_attempts INT NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 20),
            last_error TEXT,
            completed_at BIGINT,
            advisory_only BOOLEAN NOT NULL DEFAULT TRUE CHECK (advisory_only IS TRUE),
            decision_eligible BOOLEAN NOT NULL DEFAULT FALSE CHECK (decision_eligible IS FALSE)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_tasks_claim
        ON ghost_agent_tasks (status, priority DESC, available_at, created_at)
        WHERE status IN ('PENDING','CLAIMED')
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_tasks_symbol_time
        ON ghost_agent_tasks (symbol, created_at DESC)
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_agent_task_events (
            id BIGSERIAL PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES ghost_agent_tasks(task_id),
            event_type TEXT NOT NULL,
            actor TEXT NOT NULL,
            event_ts BIGINT NOT NULL,
            metadata JSONB NOT NULL DEFAULT '{}'
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_task_events_task_time
        ON ghost_agent_task_events (task_id, event_ts, id)
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_agent_evidence (
            evidence_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES ghost_agent_tasks(task_id),
            agent_id TEXT NOT NULL,
            agent_provider TEXT NOT NULL,
            model_name TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            submitted_at BIGINT NOT NULL,
            summary TEXT NOT NULL,
            claims JSONB NOT NULL,
            source_refs JSONB NOT NULL,
            agent_confidence DOUBLE PRECISION,
            payload_sha256 CHAR(64) NOT NULL,
            raw_response JSONB,
            validation_status TEXT NOT NULL
                CHECK (validation_status IN ('ACCEPTED','QUARANTINED')),
            validation_reasons JSONB NOT NULL,
            quarantine_category TEXT NOT NULL DEFAULT 'none'
                CHECK (quarantine_category IN (
                    'none','schema_error','source_error','injection_suspected','policy_violation'
                )),
            validation_errors JSONB NOT NULL DEFAULT '[]',
            lease_id TEXT,
            repair_of_evidence_id TEXT REFERENCES ghost_agent_evidence(evidence_id),
            advisory_only BOOLEAN NOT NULL DEFAULT TRUE CHECK (advisory_only IS TRUE),
            decision_eligible BOOLEAN NOT NULL DEFAULT FALSE CHECK (decision_eligible IS FALSE),
            UNIQUE (task_id, agent_id, payload_sha256)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_evidence_task_time
        ON ghost_agent_evidence (task_id, submitted_at, evidence_id)
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_agent_evidence_validations (
            id BIGSERIAL PRIMARY KEY,
            evidence_id TEXT NOT NULL REFERENCES ghost_agent_evidence(evidence_id),
            validator TEXT NOT NULL,
            validation_ts BIGINT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('ACCEPTED','QUARANTINED')),
            reasons JSONB NOT NULL,
            quarantine_category TEXT NOT NULL DEFAULT 'none',
            errors JSONB NOT NULL DEFAULT '[]',
            checks JSONB NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_validation_evidence_time
        ON ghost_agent_evidence_validations (evidence_id, validation_ts, id)
        """
    )
    # Additive migrations for installations created by the Phase 2.0 workflow.
    cur.execute("ALTER TABLE ghost_agent_tasks ADD COLUMN IF NOT EXISTS lease_id TEXT")
    cur.execute(
        """ALTER TABLE ghost_agent_evidence ADD COLUMN IF NOT EXISTS quarantine_category TEXT
           NOT NULL DEFAULT 'none' CHECK (quarantine_category IN (
             'none','schema_error','source_error','injection_suspected','policy_violation'
           ))"""
    )
    cur.execute(
        "ALTER TABLE ghost_agent_evidence ADD COLUMN IF NOT EXISTS validation_errors JSONB "
        "NOT NULL DEFAULT '[]'"
    )
    cur.execute("ALTER TABLE ghost_agent_evidence ADD COLUMN IF NOT EXISTS lease_id TEXT")
    cur.execute(
        "ALTER TABLE ghost_agent_evidence ADD COLUMN IF NOT EXISTS repair_of_evidence_id TEXT "
        "REFERENCES ghost_agent_evidence(evidence_id)"
    )
    cur.execute(
        "ALTER TABLE ghost_agent_evidence_validations ADD COLUMN IF NOT EXISTS "
        "quarantine_category TEXT NOT NULL DEFAULT 'none'"
    )
    cur.execute(
        "ALTER TABLE ghost_agent_evidence_validations ADD COLUMN IF NOT EXISTS errors JSONB "
        "NOT NULL DEFAULT '[]'"
    )
    cur.execute(
        """UPDATE ghost_agent_evidence
           SET quarantine_category=CASE
             WHEN validation_reasons::text ILIKE '%prompt injection%' THEN 'injection_suspected'
             WHEN validation_reasons::text ILIKE '%claims.%' THEN 'schema_error'
             WHEN validation_reasons::text ILIKE '%source_ref%' THEN 'source_error'
             ELSE 'schema_error'
           END
           WHERE validation_status='QUARANTINED' AND quarantine_category='none'"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS ghost_agent_workers (
            agent_id TEXT PRIMARY KEY,
            agent_provider TEXT NOT NULL,
            model_name TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('STARTING','IDLE','WORKING','ERROR','STOPPED')),
            current_task_id TEXT REFERENCES ghost_agent_tasks(task_id),
            started_at BIGINT NOT NULL,
            last_seen_at BIGINT NOT NULL,
            processed_count BIGINT NOT NULL DEFAULT 0,
            accepted_count BIGINT NOT NULL DEFAULT 0,
            quarantined_count BIGINT NOT NULL DEFAULT 0,
            last_error TEXT,
            metadata JSONB NOT NULL DEFAULT '{}',
            advisory_only BOOLEAN NOT NULL DEFAULT TRUE CHECK (advisory_only IS TRUE),
            decision_eligible BOOLEAN NOT NULL DEFAULT FALSE CHECK (decision_eligible IS FALSE)
        )"""
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_workers_last_seen "
        "ON ghost_agent_workers (last_seen_at DESC)"
    )


def _event(cur, task_id: str, event_type: str, actor: str, now_ts: int, metadata: Any = None) -> None:
    cur.execute(
        """INSERT INTO ghost_agent_task_events
           (task_id, event_type, actor, event_ts, metadata)
           VALUES (%s,%s,%s,%s,%s::jsonb)""",
        (task_id, event_type, actor[:128], now_ts, _canonical_json(metadata or {})),
    )


def create_task(
    *,
    task_type: str,
    requested_by: str,
    request_payload: Dict[str, Any],
    symbol: Optional[str] = None,
    priority: int = 50,
    available_at: Optional[int] = None,
    deadline_at: Optional[int] = None,
    required_response_schema: Optional[Dict[str, Any]] = None,
    required_submissions: int = 1,
    max_attempts: int = 3,
    idempotency_key: Optional[str] = None,
    now_ts: Optional[int] = None,
) -> Dict[str, Any]:
    """Create one idempotent advisory task and return its public record."""
    task_type = _normalize_task_type(task_type)
    symbol = _normalize_symbol(symbol)
    requested_by = str(requested_by or "").strip()
    if not requested_by or len(requested_by) > 128:
        raise AgentWorkflowError("invalid requested_by")
    payload = _bounded_json(request_payload, max_bytes=_MAX_REQUEST_BYTES, field="request_payload")
    schema = _bounded_json(
        required_response_schema or DEFAULT_RESPONSE_SCHEMA,
        max_bytes=_MAX_SCHEMA_BYTES,
        field="required_response_schema",
    )
    if _json_depth(schema) > 12:
        raise AgentWorkflowError("required_response_schema exceeds maximum nesting depth")
    try:
        priority = int(priority)
        required_submissions = int(required_submissions)
        max_attempts = int(max_attempts)
    except (TypeError, ValueError):
        raise AgentWorkflowError("priority, required_submissions, and max_attempts must be integers")
    if not 0 <= priority <= 100:
        raise AgentWorkflowError("priority must be between 0 and 100")
    if not 1 <= required_submissions <= 5:
        raise AgentWorkflowError("required_submissions must be between 1 and 5")
    if not required_submissions <= max_attempts <= 20:
        raise AgentWorkflowError("max_attempts must be >= required_submissions and <= 20")
    now = int(time.time()) if now_ts is None else int(now_ts)
    available = now if available_at is None else int(available_at)
    deadline = None if deadline_at is None else int(deadline_at)
    if deadline is not None and deadline <= available:
        raise AgentWorkflowError("deadline_at must be later than available_at")
    if idempotency_key is None:
        idempotency_key = "agent-task:" + _digest(
            {"task_type": task_type, "symbol": symbol, "payload": payload}
        )[:40]
    idempotency_key = str(idempotency_key).strip()
    if not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
        raise AgentWorkflowError("invalid idempotency_key")
    task_id = "agt_" + uuid.uuid4().hex

    from core.db import db_conn

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""INSERT INTO ghost_agent_tasks (
                    task_id, idempotency_key, task_type, symbol, priority, status,
                    requested_by, request_payload, required_response_schema,
                    required_submissions, available_at, deadline_at, created_at, updated_at,
                    max_attempts, advisory_only, decision_eligible
                ) VALUES (%s,%s,%s,%s,%s,'PENDING',%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s,%s,TRUE,FALSE)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING {_TASK_SELECT}""",
            (
                task_id, idempotency_key, task_type, symbol, priority, requested_by,
                _canonical_json(payload), _canonical_json(schema), required_submissions,
                available, deadline, now, now, max_attempts,
            ),
        )
        row = cur.fetchone()
        created = row is not None
        if row is None:
            cur.execute(
                f"SELECT {_TASK_SELECT} FROM ghost_agent_tasks WHERE idempotency_key=%s",
                (idempotency_key,),
            )
            row = cur.fetchone()
        if not row:
            raise RuntimeError("agent task insert returned no row")
        task = _public_task(row)
        if not created and (
            task.get("task_type") != task_type
            or task.get("symbol") != symbol
            or _digest(task.get("request_payload")) != _digest(payload)
            or _digest(task.get("required_response_schema")) != _digest(schema)
        ):
            raise AgentWorkflowError("idempotency_key already belongs to a different task payload")
        if created:
            _event(
                cur, task["task_id"], "CREATED", requested_by, now,
                {"task_type": task_type, "symbol": symbol, "priority": priority},
            )
    return {"ok": True, "created": created, "task": task}


def _expire_unavailable_tasks(cur, now: int) -> Dict[str, int]:
    cur.execute(
        """UPDATE ghost_agent_tasks
           SET status='EXPIRED', updated_at=%s, claimed_by=NULL,
               lease_token_sha256=NULL, lease_id=NULL, lease_expires_at=NULL
           WHERE status IN ('PENDING','CLAIMED')
             AND deadline_at IS NOT NULL AND deadline_at <= %s
           RETURNING task_id""",
        (now, now),
    )
    expired_rows = cur.fetchall() or []
    for row in expired_rows:
        task_id = row[0] if not isinstance(row, Mapping) else row["task_id"]
        _event(cur, task_id, "EXPIRED", "ghost.workflow", now)

    cur.execute(
        """UPDATE ghost_agent_tasks
           SET status=CASE WHEN attempt_count >= max_attempts THEN 'DEAD_LETTER' ELSE 'PENDING' END,
               updated_at=%s, claimed_by=NULL, lease_token_sha256=NULL,
               lease_id=NULL, lease_expires_at=NULL, last_error='lease_expired'
           WHERE status='CLAIMED' AND lease_expires_at <= %s
           RETURNING task_id, status""",
        (now, now),
    )
    lease_rows = cur.fetchall() or []
    requeued = 0
    dead_letter = 0
    for row in lease_rows:
        item = _row_dict(row, ("task_id", "status"))
        event_type = "DEAD_LETTER" if item["status"] == "DEAD_LETTER" else "LEASE_EXPIRED"
        if item["status"] == "DEAD_LETTER":
            dead_letter += 1
        else:
            requeued += 1
        _event(cur, item["task_id"], event_type, "ghost.workflow", now)
    return {"expired": len(expired_rows), "requeued": requeued, "dead_letter": dead_letter}


def maintain_workflow(*, now_ts: Optional[int] = None) -> Dict[str, Any]:
    """Expire deadlines and recover abandoned leases independently of agents."""
    now = int(time.time()) if now_ts is None else int(now_ts)
    from core.db import db_conn

    with db_conn() as conn:
        result = _expire_unavailable_tasks(conn.cursor(), now)
    return {"ok": True, **result}


def claim_task(
    *,
    agent_id: str,
    lease_seconds: int = 600,
    task_types: Optional[Iterable[str]] = None,
    now_ts: Optional[int] = None,
) -> Dict[str, Any]:
    """Atomically claim the highest-priority task available to this agent."""
    agent_id = _normalize_agent_id(agent_id)
    lease = _lease_seconds(lease_seconds)
    normalized_types = None
    if task_types:
        normalized_types = [_normalize_task_type(item) for item in task_types]
    now = int(time.time()) if now_ts is None else int(now_ts)
    lease_token = secrets.token_urlsafe(32)
    token_sha = hashlib.sha256(lease_token.encode("utf-8")).hexdigest()

    from core.db import db_conn

    with db_conn() as conn:
        cur = conn.cursor()
        _expire_unavailable_tasks(cur, now)
        where_type = ""
        query_params: List[Any] = [now, now, agent_id]
        if normalized_types:
            where_type = " AND t.task_type = ANY(%s)"
            query_params.append(normalized_types)
        cur.execute(
            f"""SELECT {_TASK_SELECT}
                FROM ghost_agent_tasks t
                WHERE t.status='PENDING' AND t.available_at <= %s
                  AND (t.deadline_at IS NULL OR t.deadline_at > %s)
                  AND t.attempt_count < t.max_attempts
                  AND NOT EXISTS (
                      SELECT 1 FROM ghost_agent_evidence e
                      WHERE e.task_id=t.task_id AND e.agent_id=%s
                        AND e.validation_status='ACCEPTED'
                  )
                  {where_type}
                ORDER BY t.priority DESC, t.available_at, t.created_at
                FOR UPDATE SKIP LOCKED LIMIT 1""",
            query_params,
        )
        row = cur.fetchone()
        if not row:
            return {"ok": True, "claimed": False, "task": None}
        task = _public_task(row)
        lease_expires = now + lease
        lease_id = "lease_" + uuid.uuid4().hex
        cur.execute(
            """UPDATE ghost_agent_tasks
               SET status='CLAIMED', claimed_by=%s, lease_token_sha256=%s,
                   lease_id=%s, lease_expires_at=%s, attempt_count=attempt_count+1,
                   updated_at=%s, last_error=NULL
               WHERE task_id=%s""",
            (agent_id, token_sha, lease_id, lease_expires, now, task["task_id"]),
        )
        _event(
            cur, task["task_id"], "CLAIMED", agent_id, now,
            {"lease_expires_at": lease_expires},
        )
        task.update(
            {
                "status": "CLAIMED",
                "claimed_by": agent_id,
                "lease_expires_at": lease_expires,
                "attempt_count": int(task.get("attempt_count") or 0) + 1,
            }
        )
    return {
        "ok": True,
        "claimed": True,
        "task": task,
        "lease_token": lease_token,
        "lease_id": lease_id,
        "lease_expires_at": lease_expires,
        "submission_contract": submission_contract(task.get("required_response_schema")),
        "safety": {"advisory_only": True, "decision_eligible": False},
    }


def _assert_active_lease(task: Mapping[str, Any], *, agent_id: str, lease_token: str, now: int) -> None:
    if task.get("status") != "CLAIMED":
        raise AgentWorkflowError("task is not claimed")
    if task.get("claimed_by") != agent_id:
        raise AgentWorkflowError("task is claimed by another agent")
    expected = str(task.get("lease_token_sha256") or "")
    supplied = hashlib.sha256(str(lease_token or "").encode("utf-8")).hexdigest()
    if not expected or not hmac.compare_digest(expected, supplied):
        raise AgentWorkflowError("invalid lease token")
    if int(task.get("lease_expires_at") or 0) <= now:
        raise AgentWorkflowError("lease expired")


def heartbeat_task(
    *, task_id: str, agent_id: str, lease_token: str,
    lease_seconds: int = 600, now_ts: Optional[int] = None,
) -> Dict[str, Any]:
    task_id = _normalize_task_id(task_id)
    agent_id = _normalize_agent_id(agent_id)
    lease = _lease_seconds(lease_seconds)
    now = int(time.time()) if now_ts is None else int(now_ts)

    from core.db import db_conn

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT status, claimed_by, lease_token_sha256, lease_expires_at
               FROM ghost_agent_tasks WHERE task_id=%s FOR UPDATE""",
            (task_id,),
        )
        row = cur.fetchone()
        if not row:
            raise AgentWorkflowError("task not found")
        task = _row_dict(row, ("status", "claimed_by", "lease_token_sha256", "lease_expires_at"))
        _assert_active_lease(task, agent_id=agent_id, lease_token=lease_token, now=now)
        lease_expires = now + lease
        cur.execute(
            "UPDATE ghost_agent_tasks SET lease_expires_at=%s, updated_at=%s WHERE task_id=%s",
            (lease_expires, now, task_id),
        )
        _event(cur, task_id, "HEARTBEAT", agent_id, now, {"lease_expires_at": lease_expires})
    return {"ok": True, "task_id": task_id, "lease_expires_at": lease_expires}


def _validation_error(
    code: str, path: str, message: str, category: str, *, repairable: bool = True,
) -> Dict[str, Any]:
    return {
        "code": code,
        "path": path,
        "message": message,
        "category": category,
        "repairable": repairable,
    }


def _validate_schema_value_details(
    value: Any, schema: Any, path: str = "claims",
) -> List[Dict[str, Any]]:
    """Validate the bounded JSON-Schema subset with machine-readable errors."""
    if not isinstance(schema, dict):
        return [_validation_error("invalid_schema", path, "invalid response schema", "schema_error")]
    errors: List[Dict[str, Any]] = []
    expected = schema.get("type")
    type_ok = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if isinstance(expected, str) and expected in type_ok and not type_ok[expected]:
        return [_validation_error("type_mismatch", path, f"expected {expected}", "schema_error")]
    if "enum" in schema and value not in schema.get("enum", []):
        errors.append(_validation_error("enum", path, "value not in enum", "schema_error"))
    if isinstance(value, dict):
        required = schema.get("required", [])
        if isinstance(required, list):
            for field in required:
                if field not in value:
                    errors.append(
                        _validation_error("required", f"{path}.{field}", "required", "schema_error")
                    )
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for field, child_schema in properties.items():
                if field in value:
                    errors.extend(
                        _validate_schema_value_details(value[field], child_schema, f"{path}.{field}")
                    )
            if schema.get("additionalProperties") is False:
                for field in value:
                    if field not in properties:
                        errors.append(
                            _validation_error(
                                "additional_property", f"{path}.{field}",
                                "additional property not allowed", "schema_error",
                            )
                        )
    if isinstance(value, list):
        if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
            errors.append(_validation_error("min_items", path, "fewer than minItems", "schema_error"))
        if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
            errors.append(_validation_error("max_items", path, "more than maxItems", "schema_error"))
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(_validate_schema_value_details(item, item_schema, f"{path}[{index}]"))
    if isinstance(value, str):
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            errors.append(_validation_error("min_length", path, "shorter than minLength", "schema_error"))
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            errors.append(_validation_error("max_length", path, "longer than maxLength", "schema_error"))
    return errors[:50]


def _validate_schema_value(value: Any, schema: Any, path: str = "claims") -> List[str]:
    """Compatibility wrapper returning the original human-readable reasons."""
    return [f"{item['path']}: {item['message']}" for item in _validate_schema_value_details(value, schema, path)]


def validate_submission_details(
    *, claims: Dict[str, Any], source_refs: List[Dict[str, Any]], summary: str,
    agent_confidence: Optional[float], response_schema: Dict[str, Any],
    raw_response: Any = None, now_ts: Optional[int] = None,
) -> Dict[str, Any]:
    """Return categorized and machine-readable validation output."""
    now = int(time.time()) if now_ts is None else int(now_ts)
    errors: List[Dict[str, Any]] = []
    if not isinstance(claims, dict):
        errors.append(_validation_error("type_mismatch", "claims", "must be an object", "schema_error"))
    else:
        if _json_depth(claims) > 20:
            errors.append(
                _validation_error(
                    "max_depth", "claims", "exceeds maximum nesting depth", "schema_error",
                )
            )
        elif _json_size(claims) > _MAX_CLAIMS_BYTES:
            errors.append(
                _validation_error(
                    "max_bytes", "claims", f"exceeds {_MAX_CLAIMS_BYTES} bytes", "schema_error",
                )
            )
        errors.extend(_validate_schema_value_details(claims, response_schema))
    if not isinstance(summary, str) or not summary.strip():
        errors.append(_validation_error("required", "summary", "is required", "schema_error"))
    elif len(summary) > _MAX_SUMMARY_CHARS:
        errors.append(
            _validation_error(
                "max_length", "summary", f"exceeds {_MAX_SUMMARY_CHARS} characters", "schema_error",
            )
        )
    if not isinstance(source_refs, list) or not source_refs:
        errors.append(
            _validation_error(
                "required", "source_refs", "at least one source_ref is required", "source_error",
            )
        )
    elif len(source_refs) > _MAX_SOURCES:
        errors.append(
            _validation_error(
                "max_items", "source_refs", f"exceeds {_MAX_SOURCES} items", "source_error",
            )
        )
    else:
        for index, source in enumerate(source_refs):
            path = f"source_refs[{index}]"
            if not isinstance(source, dict):
                errors.append(_validation_error("type_mismatch", path, "must be an object", "source_error"))
                continue
            locator = str(source.get("locator") or "").strip()
            kind = str(source.get("kind") or "").strip()
            if not locator or len(locator) > 2_048:
                errors.append(
                    _validation_error("required_bounded", f"{path}.locator", "is required and bounded", "source_error")
                )
            if not kind or len(kind) > 64:
                errors.append(
                    _validation_error("required_bounded", f"{path}.kind", "is required and bounded", "source_error")
                )
            for timestamp_field in ("published_ts", "observed_ts", "retrieved_ts"):
                if source.get(timestamp_field) is None:
                    continue
                timestamp_path = f"{path}.{timestamp_field}"
                try:
                    source_ts = int(source[timestamp_field])
                except (TypeError, ValueError):
                    errors.append(
                        _validation_error("epoch_seconds", timestamp_path, "must be epoch seconds", "source_error")
                    )
                    continue
                if source_ts > now + 300:
                    errors.append(
                        _validation_error("future_timestamp", timestamp_path, "is in the future", "source_error")
                    )
    if agent_confidence is not None:
        try:
            confidence = float(agent_confidence)
        except (TypeError, ValueError, OverflowError):
            errors.append(
                _validation_error("numeric", "agent_confidence", "must be numeric", "schema_error")
            )
        else:
            if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
                errors.append(
                    _validation_error(
                        "range", "agent_confidence", "must be between 0 and 1", "schema_error",
                    )
                )
    untrusted_text = " ".join(
        (
            str(summary or ""),
            _canonical_json(claims),
            _canonical_json(source_refs),
            _canonical_json(raw_response) if raw_response is not None else "",
        )
    ).lower()
    if any(pattern in untrusted_text for pattern in _PROMPT_INJECTION_PATTERNS):
        errors.append(
            _validation_error(
                "prompt_injection", "submission", "potential prompt injection detected in agent evidence",
                "injection_suspected", repairable=False,
            )
        )
    errors = errors[:50]
    categories = {str(item["category"]) for item in errors}
    if "injection_suspected" in categories:
        primary = "injection_suspected"
    elif "policy_violation" in categories:
        primary = "policy_violation"
    elif "schema_error" in categories:
        primary = "schema_error"
    elif "source_error" in categories:
        primary = "source_error"
    else:
        primary = "none"
    reasons = []
    for item in errors:
        path = str(item.get("path") or "")
        message = str(item["message"])
        if path == "claims" and message == "must be an object":
            reasons.append("claims must be an object")
        elif path == "summary":
            reasons.append(f"summary {message}")
        elif path == "agent_confidence":
            reasons.append(f"agent_confidence {message}")
        elif path in {"source_refs", "submission"}:
            reasons.append(message)
        else:
            reasons.append(f"{path}: {message}" if path else message)
    return {
        "valid": not errors,
        "quarantine_category": primary,
        "validation_categories": sorted(categories),
        "validation_errors": errors,
        "validation_reasons": reasons,
        "retry_allowed": bool(errors) and all(bool(item.get("repairable")) for item in errors),
    }


def validate_submission(
    *, claims: Dict[str, Any], source_refs: List[Dict[str, Any]], summary: str,
    agent_confidence: Optional[float], response_schema: Dict[str, Any],
    raw_response: Any = None,
    now_ts: Optional[int] = None,
) -> List[str]:
    """Return structural validation reasons; an empty list means accepted."""
    result = validate_submission_details(
        claims=claims,
        source_refs=source_refs,
        summary=summary,
        agent_confidence=agent_confidence,
        response_schema=response_schema,
        raw_response=raw_response,
        now_ts=now_ts,
    )
    return list(result["validation_reasons"])


def submit_evidence(
    *,
    task_id: str,
    agent_id: str,
    lease_token: str,
    agent_provider: str,
    model_name: str,
    prompt_version: str,
    summary: str,
    claims: Dict[str, Any],
    source_refs: List[Dict[str, Any]],
    agent_confidence: Optional[float] = None,
    raw_response: Any = None,
    repair_of_evidence_id: Optional[str] = None,
    now_ts: Optional[int] = None,
) -> Dict[str, Any]:
    """Submit evidence, retaining the lease when a bounded repair is possible."""
    task_id = _normalize_task_id(task_id)
    agent_id = _normalize_agent_id(agent_id)
    repair_id = (
        _normalize_evidence_id(repair_of_evidence_id)
        if repair_of_evidence_id is not None
        else None
    )
    for field, value in (
        ("agent_provider", agent_provider),
        ("model_name", model_name),
        ("prompt_version", prompt_version),
    ):
        if not str(value or "").strip() or len(str(value)) > 128:
            raise AgentWorkflowError(f"invalid {field}")
    now = int(time.time()) if now_ts is None else int(now_ts)
    raw_safe = _safe_raw_response(raw_response)
    payload_sha = _digest(
        {
            "task_id": task_id,
            "agent_id": agent_id,
            "summary": summary,
            "claims": claims,
            "source_refs": source_refs,
            "raw_response": raw_safe,
            "repair_of_evidence_id": repair_id,
        }
    )

    from core.db import db_conn

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT status, claimed_by, lease_token_sha256, lease_expires_at,
                      lease_id, required_response_schema, required_submissions,
                      attempt_count, max_attempts
               FROM ghost_agent_tasks WHERE task_id=%s FOR UPDATE""",
            (task_id,),
        )
        row = cur.fetchone()
        if not row:
            raise AgentWorkflowError("task not found")
        task = _row_dict(
            row,
            (
                "status", "claimed_by", "lease_token_sha256", "lease_expires_at",
                "lease_id", "required_response_schema", "required_submissions",
                "attempt_count", "max_attempts",
            ),
        )

        cur.execute(
            f"""SELECT {', '.join(_EVIDENCE_COLUMNS)} FROM ghost_agent_evidence
                WHERE task_id=%s AND agent_id=%s AND payload_sha256=%s""",
            (task_id, agent_id, payload_sha),
        )
        existing = cur.fetchone()
        if existing:
            evidence = _public_evidence(existing)
            return {
                "ok": True,
                "idempotent": True,
                "accepted": evidence["validation_status"] == "ACCEPTED",
                "evidence": evidence,
                "quarantine_category": evidence.get("quarantine_category", "none"),
                "validation_errors": evidence.get("validation_errors") or [],
            }

        _assert_active_lease(task, agent_id=agent_id, lease_token=lease_token, now=now)
        lease_id = str(task.get("lease_id") or "")
        if not lease_id:
            raise AgentWorkflowError("task lease has no lease_id")
        if repair_id:
            cur.execute(
                """SELECT 1 FROM ghost_agent_evidence
                   WHERE evidence_id=%s AND task_id=%s AND agent_id=%s
                     AND lease_id=%s AND validation_status='QUARANTINED'""",
                (repair_id, task_id, agent_id, lease_id),
            )
            if not cur.fetchone():
                raise AgentWorkflowError(
                    "repair_of_evidence_id must reference this agent's quarantined evidence "
                    "from the active lease"
                )
        schema = task.get("required_response_schema") or DEFAULT_RESPONSE_SCHEMA
        if isinstance(schema, str):
            schema = json.loads(schema)
        validation = validate_submission_details(
            claims=claims,
            source_refs=source_refs,
            summary=summary,
            agent_confidence=agent_confidence,
            response_schema=schema,
            raw_response=raw_safe,
            now_ts=now,
        )
        reasons = list(validation["validation_reasons"])
        validation_errors = list(validation["validation_errors"])
        quarantine_category = str(validation["quarantine_category"])
        validation_status = "QUARANTINED" if reasons else "ACCEPTED"
        evidence_id = "evd_" + uuid.uuid4().hex
        try:
            confidence_value = None if agent_confidence is None else float(agent_confidence)
        except (TypeError, ValueError, OverflowError):
            confidence_value = None
        if confidence_value is not None and not math.isfinite(confidence_value):
            confidence_value = None
        cur.execute(
            f"""INSERT INTO ghost_agent_evidence (
                    evidence_id, task_id, agent_id, agent_provider, model_name,
                    prompt_version, submitted_at, summary, claims, source_refs,
                    agent_confidence, payload_sha256, raw_response, validation_status,
                    validation_reasons, quarantine_category, validation_errors,
                    lease_id, repair_of_evidence_id, advisory_only, decision_eligible
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s::jsonb,%s,
                    %s::jsonb,%s,%s::jsonb,%s,%s,TRUE,FALSE
                )
                RETURNING {', '.join(_EVIDENCE_COLUMNS)}""",
            (
                evidence_id, task_id, agent_id, str(agent_provider).strip(),
                str(model_name).strip(), str(prompt_version).strip(), now, summary.strip(),
                _canonical_json(claims), _canonical_json(source_refs), confidence_value,
                payload_sha, _canonical_json(raw_safe) if raw_safe is not None else None,
                validation_status, _canonical_json(reasons), quarantine_category,
                _canonical_json(validation_errors), lease_id, repair_id,
            ),
        )
        evidence = _public_evidence(cur.fetchone())
        cur.execute(
            """INSERT INTO ghost_agent_evidence_validations
               (evidence_id, validator, validation_ts, status, reasons,
                quarantine_category, errors, checks)
               VALUES (%s,'ghost.schema_validator/v2',%s,%s,%s::jsonb,%s,%s::jsonb,%s::jsonb)""",
            (
                evidence_id, now, validation_status, _canonical_json(reasons),
                quarantine_category, _canonical_json(validation_errors),
                _canonical_json(
                    {
                        "schema": True,
                        "sources": bool(source_refs),
                        "bounds": True,
                        "deterministic_market_validation": False,
                    }
                ),
            ),
        )
        cur.execute(
            """SELECT COUNT(DISTINCT agent_id) FROM ghost_agent_evidence
               WHERE task_id=%s AND validation_status='ACCEPTED'""",
            (task_id,),
        )
        accepted_count = int((cur.fetchone() or (0,))[0])
        required = int(task.get("required_submissions") or 1)
        attempts = int(task.get("attempt_count") or 0)
        max_attempts = int(task.get("max_attempts") or 1)
        cur.execute(
            """SELECT COUNT(*) FROM ghost_agent_evidence
               WHERE task_id=%s AND agent_id=%s AND lease_id=%s
                 AND validation_status='QUARANTINED'""",
            (task_id, agent_id, lease_id),
        )
        lease_quarantine_count = int((cur.fetchone() or (0,))[0])
        try:
            max_repairs = max(1, min(5, int(os.getenv("AGENT_MAX_REPAIR_SUBMISSIONS", "2"))))
        except ValueError:
            max_repairs = 2
        retry_allowed = bool(validation["retry_allowed"])
        lease_retained = (
            validation_status == "QUARANTINED"
            and retry_allowed
            and lease_quarantine_count <= max_repairs
        )
        if validation_status == "ACCEPTED" and accepted_count >= required:
            next_status = "COMPLETED"
            completed_at = now
        elif lease_retained:
            next_status = "CLAIMED"
            completed_at = None
        elif validation_status == "QUARANTINED" and attempts >= max_attempts:
            next_status = "DEAD_LETTER"
            completed_at = None
        else:
            next_status = "PENDING"
            completed_at = None
        last_error = "; ".join(reasons)[:1_000] if reasons else None
        if lease_retained:
            cur.execute(
                """UPDATE ghost_agent_tasks
                   SET status='CLAIMED', updated_at=%s, last_error=%s
                   WHERE task_id=%s""",
                (now, last_error, task_id),
            )
        else:
            cur.execute(
                """UPDATE ghost_agent_tasks
                   SET status=%s, updated_at=%s, completed_at=%s, claimed_by=NULL,
                       lease_token_sha256=NULL, lease_id=NULL, lease_expires_at=NULL,
                       last_error=%s
                   WHERE task_id=%s""",
                (next_status, now, completed_at, last_error, task_id),
            )
        _event(
            cur, task_id,
            "EVIDENCE_ACCEPTED" if validation_status == "ACCEPTED" else "EVIDENCE_QUARANTINED",
            agent_id, now,
            {
                "evidence_id": evidence_id,
                "validation_status": validation_status,
                "quarantine_category": quarantine_category,
                "validation_categories": validation["validation_categories"],
                "accepted_submissions": accepted_count,
                "required_submissions": required,
                "task_status": next_status,
                "lease_retained": lease_retained,
                "repair_of_evidence_id": repair_id,
            },
        )
    return {
        "ok": True,
        "idempotent": False,
        "accepted": validation_status == "ACCEPTED",
        "task_status": next_status,
        "accepted_submissions": accepted_count,
        "required_submissions": required,
        "evidence": evidence,
        "quarantine_category": quarantine_category,
        "validation_categories": validation["validation_categories"],
        "validation_errors": validation_errors,
        "validation_reasons": reasons,
        "retry_allowed": lease_retained,
        "lease_retained": lease_retained,
        "lease_expires_at": task.get("lease_expires_at") if lease_retained else None,
        "submission_contract": submission_contract(schema) if reasons else None,
        "safety": {"advisory_only": True, "decision_eligible": False},
    }


def release_task(
    *, task_id: str, agent_id: str, lease_token: str,
    reason: str = "agent_released", now_ts: Optional[int] = None,
) -> Dict[str, Any]:
    task_id = _normalize_task_id(task_id)
    agent_id = _normalize_agent_id(agent_id)
    now = int(time.time()) if now_ts is None else int(now_ts)
    reason = str(reason or "agent_released").strip()[:500]

    from core.db import db_conn

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT status, claimed_by, lease_token_sha256, lease_expires_at,
                      attempt_count, max_attempts
               FROM ghost_agent_tasks WHERE task_id=%s FOR UPDATE""",
            (task_id,),
        )
        row = cur.fetchone()
        if not row:
            raise AgentWorkflowError("task not found")
        task = _row_dict(
            row,
            ("status", "claimed_by", "lease_token_sha256", "lease_expires_at", "attempt_count", "max_attempts"),
        )
        _assert_active_lease(task, agent_id=agent_id, lease_token=lease_token, now=now)
        next_status = (
            "DEAD_LETTER"
            if int(task.get("attempt_count") or 0) >= int(task.get("max_attempts") or 1)
            else "PENDING"
        )
        cur.execute(
            """UPDATE ghost_agent_tasks SET status=%s, updated_at=%s, claimed_by=NULL,
                      lease_token_sha256=NULL, lease_id=NULL, lease_expires_at=NULL, last_error=%s
               WHERE task_id=%s""",
            (next_status, now, reason, task_id),
        )
        _event(cur, task_id, "RELEASED", agent_id, now, {"reason": reason, "status": next_status})
    return {"ok": True, "task_id": task_id, "status": next_status}


def list_tasks(
    *, status: Optional[str] = "PENDING", task_type: Optional[str] = None,
    symbol: Optional[str] = None, limit: int = 50,
) -> Dict[str, Any]:
    if status:
        status = str(status).strip().upper()
        if status not in TASK_STATUSES:
            raise AgentWorkflowError("invalid status")
    if task_type:
        task_type = _normalize_task_type(task_type)
    symbol = _normalize_symbol(symbol)
    limit = max(1, min(int(limit), 200))
    conditions: List[str] = []
    params: List[Any] = []
    if status:
        conditions.append("status=%s")
        params.append(status)
    if task_type:
        conditions.append("task_type=%s")
        params.append(task_type)
    if symbol:
        conditions.append("symbol=%s")
        params.append(symbol)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""

    from core.db import db_conn

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {_TASK_SELECT} FROM ghost_agent_tasks{where} "
            "ORDER BY priority DESC, created_at DESC LIMIT %s",
            [*params, limit],
        )
        rows = cur.fetchall() or []
    tasks = [_public_task(row) for row in rows]
    return {
        "ok": True,
        "tasks": tasks,
        "count": len(tasks),
        "safety": {"advisory_only": True, "decision_eligible": False},
    }


def get_task(task_id: str) -> Dict[str, Any]:
    task_id = _normalize_task_id(task_id)
    from core.db import db_conn

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT {_TASK_SELECT} FROM ghost_agent_tasks WHERE task_id=%s", (task_id,))
        row = cur.fetchone()
        if not row:
            return {"ok": False, "error": "task_not_found"}
        task = _public_task(row)
        cur.execute(
            f"""SELECT {', '.join(_EVIDENCE_COLUMNS)} FROM ghost_agent_evidence
                WHERE task_id=%s ORDER BY submitted_at, evidence_id""",
            (task_id,),
        )
        evidence = [_public_evidence(item) for item in (cur.fetchall() or [])]
        cur.execute(
            """SELECT event_type, actor, event_ts, metadata
               FROM ghost_agent_task_events WHERE task_id=%s ORDER BY event_ts, id""",
            (task_id,),
        )
        events = [
            _row_dict(item, ("event_type", "actor", "event_ts", "metadata"))
            for item in (cur.fetchall() or [])
        ]
    return {"ok": True, "task": task, "evidence": evidence, "events": events}


def heartbeat_worker(
    *, agent_id: str, agent_provider: str, model_name: str, status: str,
    current_task_id: Optional[str] = None, processed_delta: int = 0,
    accepted_delta: int = 0, quarantined_delta: int = 0,
    last_error: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
    now_ts: Optional[int] = None,
) -> Dict[str, Any]:
    """Upsert a bounded worker heartbeat for operator monitoring."""
    agent_id = _normalize_agent_id(agent_id)
    worker_status = str(status or "").strip().upper()
    if worker_status not in WORKER_STATUSES:
        raise AgentWorkflowError("invalid worker status")
    provider = str(agent_provider or "").strip()
    model = str(model_name or "").strip()
    if not provider or len(provider) > 128:
        raise AgentWorkflowError("invalid agent_provider")
    if not model or len(model) > 128:
        raise AgentWorkflowError("invalid model_name")
    task_id = _normalize_task_id(current_task_id) if current_task_id else None
    meta = _bounded_json(metadata or {}, max_bytes=8_192, field="metadata")
    try:
        deltas = [int(processed_delta), int(accepted_delta), int(quarantined_delta)]
    except (TypeError, ValueError):
        raise AgentWorkflowError("worker counters must be integers")
    if any(value < 0 or value > 100 for value in deltas):
        raise AgentWorkflowError("worker counter deltas must be between 0 and 100")
    now = int(time.time()) if now_ts is None else int(now_ts)
    error = str(last_error or "").strip()[:1_000] or None

    from core.db import db_conn

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO ghost_agent_workers (
                   agent_id, agent_provider, model_name, status, current_task_id,
                   started_at, last_seen_at, processed_count, accepted_count,
                   quarantined_count, last_error, metadata, advisory_only, decision_eligible
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,TRUE,FALSE)
               ON CONFLICT (agent_id) DO UPDATE SET
                   agent_provider=EXCLUDED.agent_provider,
                   model_name=EXCLUDED.model_name,
                   status=EXCLUDED.status,
                   current_task_id=EXCLUDED.current_task_id,
                   started_at=CASE WHEN EXCLUDED.status='STARTING'
                                   THEN EXCLUDED.started_at
                                   ELSE ghost_agent_workers.started_at END,
                   last_seen_at=EXCLUDED.last_seen_at,
                   processed_count=ghost_agent_workers.processed_count+EXCLUDED.processed_count,
                   accepted_count=ghost_agent_workers.accepted_count+EXCLUDED.accepted_count,
                   quarantined_count=ghost_agent_workers.quarantined_count+EXCLUDED.quarantined_count,
                   last_error=EXCLUDED.last_error,
                   metadata=EXCLUDED.metadata,
                   advisory_only=TRUE,
                   decision_eligible=FALSE
               RETURNING agent_id, agent_provider, model_name, status, current_task_id,
                         started_at, last_seen_at, processed_count, accepted_count,
                         quarantined_count, last_error, metadata,
                         advisory_only, decision_eligible""",
            (
                agent_id, provider, model, worker_status, task_id, now, now,
                deltas[0], deltas[1], deltas[2], error, _canonical_json(meta),
            ),
        )
        columns = (
            "agent_id", "agent_provider", "model_name", "status", "current_task_id",
            "started_at", "last_seen_at", "processed_count", "accepted_count",
            "quarantined_count", "last_error", "metadata", "advisory_only",
            "decision_eligible",
        )
        worker = _row_dict(cur.fetchone(), columns)
    worker["advisory_only"] = True
    worker["decision_eligible"] = False
    return {"ok": True, "worker": worker}


def workflow_dashboard(*, limit: int = 30, now_ts: Optional[int] = None) -> Dict[str, Any]:
    """Return a sanitized operator view of workers, tasks, and validations."""
    now = int(time.time()) if now_ts is None else int(now_ts)
    limit = max(1, min(int(limit), 100))
    from core.db import db_conn

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT agent_id, agent_provider, model_name, status, current_task_id,
                      started_at, last_seen_at, processed_count, accepted_count,
                      quarantined_count, last_error
               FROM ghost_agent_workers ORDER BY last_seen_at DESC"""
        )
        worker_columns = (
            "agent_id", "agent_provider", "model_name", "status", "current_task_id",
            "started_at", "last_seen_at", "processed_count", "accepted_count",
            "quarantined_count", "last_error",
        )
        workers = [_row_dict(row, worker_columns) for row in (cur.fetchall() or [])]
        for worker in workers:
            age = max(0, now - int(worker.get("last_seen_at") or 0))
            worker["heartbeat_age_seconds"] = age
            worker["online"] = age <= 120 and worker.get("status") != "STOPPED"
        cur.execute(
            f"SELECT {_TASK_SELECT} FROM ghost_agent_tasks "
            "ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        tasks = [_public_task(row) for row in (cur.fetchall() or [])]
        cur.execute(
            """SELECT evidence_id, task_id, agent_id, model_name, submitted_at,
                      summary, validation_status, quarantine_category,
                      validation_reasons, repair_of_evidence_id
               FROM ghost_agent_evidence
               ORDER BY submitted_at DESC, evidence_id DESC LIMIT %s""",
            (limit,),
        )
        evidence_columns = (
            "evidence_id", "task_id", "agent_id", "model_name", "submitted_at",
            "summary", "validation_status", "quarantine_category",
            "validation_reasons", "repair_of_evidence_id",
        )
        evidence = [_row_dict(row, evidence_columns) for row in (cur.fetchall() or [])]
        cur.execute(
            """SELECT quarantine_category, COUNT(*) FROM ghost_agent_evidence
               WHERE validation_status='QUARANTINED' GROUP BY quarantine_category"""
        )
        categories = {category: 0 for category in QUARANTINE_CATEGORIES if category != "none"}
        for row in cur.fetchall() or []:
            item = _row_dict(row, ("category", "count"))
            categories[str(item["category"])] = int(item["count"])
    return {
        "ok": True,
        "generated_at": now,
        "health": workflow_health(),
        "workers": workers,
        "recent_tasks": tasks,
        "recent_evidence": evidence,
        "quarantine_categories": categories,
        "submission_contract": submission_contract(),
        "safety": {"advisory_only": True, "decision_eligible": False},
    }


def workflow_health() -> Dict[str, Any]:
    from core.db import db_conn

    now = int(time.time())
    counts = {status.lower(): 0 for status in TASK_STATUSES}
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT status, COUNT(*) FROM ghost_agent_tasks GROUP BY status")
        for row in cur.fetchall() or []:
            item = _row_dict(row, ("status", "count"))
            counts[str(item["status"]).lower()] = int(item["count"])
        cur.execute(
            "SELECT COUNT(*) FROM ghost_agent_tasks WHERE status='CLAIMED' AND lease_expires_at <= %s",
            (now,),
        )
        stale_leases = int((cur.fetchone() or (0,))[0])
        cur.execute(
            """SELECT validation_status, COUNT(*) FROM ghost_agent_evidence
               GROUP BY validation_status"""
        )
        validation_counts = {"accepted": 0, "quarantined": 0}
        for row in cur.fetchall() or []:
            item = _row_dict(row, ("status", "count"))
            validation_counts[str(item["status"]).lower()] = int(item["count"])
        cur.execute(
            """SELECT quarantine_category, COUNT(*) FROM ghost_agent_evidence
               WHERE validation_status='QUARANTINED' GROUP BY quarantine_category"""
        )
        category_counts = {
            category: 0 for category in QUARANTINE_CATEGORIES if category != "none"
        }
        for row in cur.fetchall() or []:
            item = _row_dict(row, ("category", "count"))
            category_counts[str(item["category"])] = int(item["count"])
        cur.execute(
            """SELECT COUNT(*), COUNT(*) FILTER (
                   WHERE last_seen_at >= %s AND status <> 'STOPPED'
               ) FROM ghost_agent_workers""",
            (now - 120,),
        )
        worker_row = cur.fetchone() or (0, 0)
        worker_counts = {"registered": int(worker_row[0]), "online": int(worker_row[1])}
    healthy = stale_leases == 0 and counts.get("dead_letter", 0) == 0
    return {
        "ok": healthy,
        "status": "healthy" if healthy else "degraded",
        "tasks": counts,
        "evidence": validation_counts,
        "quarantine_categories": category_counts,
        "workers": worker_counts,
        "stale_leases": stale_leases,
        "advisory_only": True,
        "decision_eligible": False,
    }


def enqueue_external_radar_tasks(
    radar: Dict[str, Any], *, now_ts: Optional[int] = None,
) -> Dict[str, Any]:
    """Turn significant persisted radar observations into idempotent agent tasks."""
    if os.getenv("AGENT_WORKFLOW_AUTOTASKS_ENABLED", "1").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        return {"ok": True, "status": "disabled", "attempted": 0, "created": 0}
    now = int(time.time()) if now_ts is None else int(now_ts)
    session_date = datetime.fromtimestamp(now, ZoneInfo("America/New_York")).date().isoformat()
    attempted = 0
    created = 0
    task_ids: List[str] = []
    for item in radar.get("items") or []:
        if not isinstance(item, dict) or item.get("market_status") != "available":
            continue
        symbol = _normalize_symbol(item.get("symbol"))
        if not symbol:
            continue
        move = float(item.get("observed_current_move_pct") or 0.0)
        peak = float(item.get("observed_peak_move_pct") or 0.0)
        rvol = float(item.get("observed_rvol") or 0.0)
        if max(abs(move), abs(peak)) < 5.0 and rvol < 2.0:
            continue
        attempted += 1
        priority = min(95, max(50, int(50 + max(abs(move), abs(peak)) + min(rvol, 10))))
        result = create_task(
            task_type="external_mover_triage",
            symbol=symbol,
            priority=priority,
            requested_by="ghost.external_radar",
            request_payload={
                "question": "Classify the observed move and identify source-backed catalysts and risks.",
                "radar_run_id": radar.get("run_id"),
                "observation": item,
                "required_output": {
                    "classifications": [
                        "earnings_gap", "short_squeeze", "news_breakout",
                        "momentum_anomaly", "unknown",
                    ],
                    "safety": "Research only. Do not recommend or submit a trade.",
                },
            },
            required_submissions=max(
                1, min(3, int(os.getenv("AGENT_EVENT_REQUIRED_SUBMISSIONS", "1")))
            ),
            max_attempts=5,
            deadline_at=now + 6 * 3600,
            idempotency_key=f"external-mover:{symbol}:{session_date}",
            now_ts=now,
        )
        created += int(bool(result.get("created")))
        task_ids.append(result["task"]["task_id"])
    return {
        "ok": True,
        "status": "queued" if attempted else "no_significant_movers",
        "attempted": attempted,
        "created": created,
        "task_ids": task_ids,
        "advisory_only": True,
        "decision_eligible": False,
    }
