"""Shadow-only storage for deterministic evidence scores.

This is the write half of "feed accepted evidence into a shadow-only
feature ledger that cannot affect live predictions." Two structural
guarantees make that true, not just documented:

1. No function in this module is called from -- or calls into --
   core.prediction, core.signal_engine, any gate module, or the paper
   wallet. Nothing here can be reached from the fire path even by accident;
   the only inbound edge is core.agent_workflow's ACCEPTED evidence rows,
   and the only outbound edge is a read-only API surface for a dashboard.
2. A score is immutable once written. Re-scoring the same evidence under a
   new SCORING_VERSION inserts a NEW row (unique on
   (evidence_id, scoring_version)); it never overwrites history. That
   mirrors core.checklist_ledger's "no backfill" rule for the same reason:
   a feature store whose past values can change out from under a later
   experiment is not a foundation you can preregister anything against.

Nothing here is a probability, a signal, or a recommendation -- read-only
research telemetry, exactly like the rest of Ghost's evidence-integrity
layer.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from core.evidence_scoring import SCORING_VERSION, score_evidence

LOGGER = logging.getLogger("ghost.shadow_evidence_ledger")


def ensure_shadow_evidence_tables(cur) -> None:
    """Create the shadow score table. Safe and idempotent."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_shadow_evidence_scores (
            id BIGSERIAL PRIMARY KEY,
            evidence_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            symbol VARCHAR(20),
            scoring_version VARCHAR(40) NOT NULL,
            composite_score FLOAT NOT NULL,
            dimensions_json JSONB NOT NULL,
            detail_json JSONB NOT NULL,
            weights_json JSONB NOT NULL,
            sibling_count INT NOT NULL DEFAULT 0,
            scored_at BIGINT NOT NULL,
            created_at BIGINT NOT NULL,
            UNIQUE (evidence_id, scoring_version)
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_shadow_evidence_scores_task "
        "ON ghost_shadow_evidence_scores (task_id, scored_at DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_shadow_evidence_scores_symbol_time "
        "ON ghost_shadow_evidence_scores (symbol, scored_at DESC)"
    )


def _now() -> int:
    return int(time.time())


def _accepted_evidence_for_task(cur, task_id: str) -> List[Dict[str, Any]]:
    cur.execute(
        """
        SELECT evidence_id, agent_id, claims, source_refs
          FROM ghost_agent_evidence
         WHERE task_id = %s AND validation_status = 'ACCEPTED'
         ORDER BY submitted_at ASC
        """,
        (task_id,),
    )
    rows = []
    for evidence_id, agent_id, claims, source_refs in cur.fetchall():
        claims = claims if isinstance(claims, dict) else (json.loads(claims) if claims else {})
        source_refs = source_refs if isinstance(source_refs, list) else (
            json.loads(source_refs) if source_refs else []
        )
        rows.append({
            "evidence_id": evidence_id,
            "agent_id": agent_id,
            "verdict": claims.get("verdict"),
            "classification": claims.get("classification"),
            "claims": claims,
            "source_refs": source_refs,
        })
    return rows


def score_and_store_evidence(
    *,
    evidence_id: str,
    task_id: str,
    symbol: Optional[str],
    claims: Dict[str, Any],
    source_refs: List[Dict[str, Any]],
    now_ts: Optional[int] = None,
) -> Dict[str, Any]:
    """Score one accepted evidence row and persist it. Returns the score row.

    Sibling evidence (other ACCEPTED submissions on the same task, e.g. a
    second agent's independent read) is pulled fresh so contradiction
    detection sees the current consensus state, not a stale snapshot.
    Idempotent on (evidence_id, scoring_version): calling this twice for the
    same evidence under the same SCORING_VERSION returns the existing row
    rather than writing a duplicate.
    """
    from core.db import db_conn

    now = _now() if now_ts is None else int(now_ts)
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_shadow_evidence_tables(cur)

        cur.execute(
            """SELECT id, composite_score, dimensions_json, detail_json, weights_json,
                      sibling_count, scored_at
                 FROM ghost_shadow_evidence_scores
                WHERE evidence_id=%s AND scoring_version=%s""",
            (evidence_id, SCORING_VERSION),
        )
        existing = cur.fetchone()
        if existing is not None:
            row_id, composite, dims, detail, weights, sib_count, scored_at = existing
            return {
                "ok": True,
                "idempotent": True,
                "id": row_id,
                "evidence_id": evidence_id,
                "scoring_version": SCORING_VERSION,
                "composite_score": composite,
                "dimensions": dims,
                "sibling_count": sib_count,
                "scored_at": scored_at,
            }

        siblings_raw = _accepted_evidence_for_task(cur, task_id)
        siblings = [s for s in siblings_raw if s["evidence_id"] != evidence_id]

        result = score_evidence(
            claims=claims, source_refs=source_refs, now_ts=now, sibling_evidence=siblings,
        )

        cur.execute(
            """
            INSERT INTO ghost_shadow_evidence_scores
                (evidence_id, task_id, symbol, scoring_version, composite_score,
                 dimensions_json, detail_json, weights_json, sibling_count,
                 scored_at, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                evidence_id, task_id, symbol, SCORING_VERSION,
                result["composite_score"],
                json.dumps(result["dimensions"]),
                json.dumps(result["detail"], default=str),
                json.dumps(result["weights"]),
                len(siblings),
                now, now,
            ),
        )
        row_id = cur.fetchone()[0]
        conn.commit()
        return {
            "ok": True,
            "idempotent": False,
            "id": row_id,
            "evidence_id": evidence_id,
            "scoring_version": SCORING_VERSION,
            "composite_score": result["composite_score"],
            "dimensions": result["dimensions"],
            "sibling_count": len(siblings),
            "scored_at": now,
        }


def score_pending_evidence(limit: int = 50) -> Dict[str, Any]:
    """Scheduler job: score every ACCEPTED evidence row that has no score yet.

    Read-only against ghost_agent_evidence and ghost_agent_tasks (never
    writes to either); writes only to ghost_shadow_evidence_scores. One
    row failing to score is logged and skipped -- it never blocks the rest
    of the pass, and it is never silently swallowed into a fake success:
    the return payload always reports how many rows actually failed.
    """
    from core.db import db_conn

    with db_conn() as conn:
        cur = conn.cursor()
        ensure_shadow_evidence_tables(cur)
        cur.execute(
            """
            SELECT e.evidence_id, e.task_id, e.claims, e.source_refs, t.symbol
             FROM ghost_agent_evidence e
              JOIN ghost_agent_tasks t ON t.task_id = e.task_id
             WHERE e.validation_status = 'ACCEPTED'
               AND t.status = 'COMPLETED'
               AND NOT EXISTS (
                   SELECT 1 FROM ghost_shadow_evidence_scores s
                    WHERE s.evidence_id = e.evidence_id
                      AND s.scoring_version = %s
               )
             ORDER BY e.submitted_at ASC
             LIMIT %s
            """,
            (SCORING_VERSION, max(1, min(500, int(limit)))),
        )
        rows = cur.fetchall()

    scored = 0
    failed = 0
    for evidence_id, task_id, claims, source_refs, symbol in rows:
        try:
            claims = claims if isinstance(claims, dict) else (json.loads(claims) if claims else {})
            source_refs = source_refs if isinstance(source_refs, list) else (
                json.loads(source_refs) if source_refs else []
            )
            score_and_store_evidence(
                evidence_id=evidence_id, task_id=task_id, symbol=symbol,
                claims=claims, source_refs=source_refs,
            )
            scored += 1
        except Exception as exc:  # noqa: BLE001 - isolate one row's failure
            failed += 1
            LOGGER.warning("shadow evidence scoring failed for %s: %s", evidence_id, str(exc)[:160])

    return {"ok": True, "scored": scored, "failed": failed, "scanned": len(rows)}


def recent_scores(*, symbol: Optional[str] = None, task_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """Read path for the dashboard -- most recently scored evidence first."""
    from core.db import db_conn

    clauses = []
    params: List[Any] = []
    if symbol:
        clauses.append("symbol = %s")
        params.append(symbol.upper())
    if task_id:
        clauses.append("task_id = %s")
        params.append(task_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with db_conn() as conn:
        cur = conn.cursor()
        ensure_shadow_evidence_tables(cur)
        cur.execute(
            f"""
            SELECT evidence_id, task_id, symbol, scoring_version, composite_score,
                   dimensions_json, sibling_count, scored_at
              FROM ghost_shadow_evidence_scores
              {where}
             ORDER BY scored_at DESC
             LIMIT %s
            """,
            tuple(params) + (max(1, min(200, int(limit))),),
        )
        cols = (
            "evidence_id", "task_id", "symbol", "scoring_version", "composite_score",
            "dimensions", "sibling_count", "scored_at",
        )
        return [dict(zip(cols, row)) for row in cur.fetchall()]
