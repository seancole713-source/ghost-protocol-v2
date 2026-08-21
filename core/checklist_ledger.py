"""Point-in-time storage for checklist snapshots, and the resolved-outcome
feed that `core.checklist_calibration` measures against.

Two honesty rules live here, structurally, not by convention:

1. No backfill. `store_snapshot` writes exactly the evidence and score Ghost
   had *at the moment it issued the call*. It is never rewritten once a
   result is known -- that is how a checklist would quietly learn to look
   good in hindsight instead of learning to predict.
2. Calibration reads only resolved rows. `resolved_samples_for_calibration`
   pulls rows with a known outcome; anything still inside its hold window is
   excluded, never guessed at as a win or a loss.

Read/write for its own snapshot table only. Never touches a trading gate,
a wallet balance, or a kill-switch condition.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from core.catalyst_checklist import HOLD_BARS
from core.tp_sl_resolve import LABEL_SCHEMA, label_hold_bars

LOGGER = logging.getLogger("ghost.checklist_ledger")

# Every calibration row must carry this full cohort identity.  Direction is a
# column already; the remaining fields distinguish incompatible checklist and
# resolution contracts so history is never pooled merely because the numeric
# scores happen to look alike.
DEFAULT_OUTCOME_CONTRACT = f"{LABEL_SCHEMA}:direction_aware:{HOLD_BARS}_daily_bars"
LEGACY_OUTCOME_CONTRACT = "legacy_unversioned:excluded_from_calibration"


def validate_outcome_contract() -> None:
    """Fail closed if issuance and TP/SL resolution horizons diverge."""
    runtime_hold_bars = label_hold_bars()
    if runtime_hold_bars != HOLD_BARS:
        raise RuntimeError(
            "checklist hold-bars contract mismatch: "
            f"checklist={HOLD_BARS}, resolver={runtime_hold_bars}"
        )

# Calibration inputs change only when a snapshot resolves, so a short TTL
# cache spares the checklist endpoint a full-table scan per request (the
# Today tab fires up to 3 checklist reads in parallel per page load).
_CALIB_CACHE_TTL_S = 120
_calib_cache: Dict[str, Any] = {}


def _bust_calibration_cache() -> None:
    _calib_cache.clear()


def ensure_checklist_tables(cur) -> None:
    """Create the snapshot table. Safe and idempotent."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_checklist_snapshots (
            id BIGSERIAL PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            direction VARCHAR(8) NOT NULL,
            checklist_version VARCHAR(40) NOT NULL,
            outcome_contract VARCHAR(120) NOT NULL,
            issued_at BIGINT NOT NULL,
            hold_bars INT NOT NULL,
            score_pct FLOAT NOT NULL,
            blocked BOOLEAN NOT NULL DEFAULT FALSE,
            entry_price FLOAT,
            target_price FLOAT,
            stop_price FLOAT,
            deadline_ts BIGINT,
            evidence_json JSONB NOT NULL,
            report_json JSONB NOT NULL,
            outcome VARCHAR(16),
            resolved_at BIGINT,
            resolved_price FLOAT,
            won BOOLEAN,
            prediction_id BIGINT,
            created_at BIGINT NOT NULL
        )
        """
    )
    # Additive migrations for ledgers created by the first checklist release.
    cur.execute(
        "ALTER TABLE ghost_checklist_snapshots "
        "ADD COLUMN IF NOT EXISTS prediction_id BIGINT"
    )
    cur.execute(
        "ALTER TABLE ghost_checklist_snapshots "
        "ADD COLUMN IF NOT EXISTS outcome_contract VARCHAR(120)"
    )
    # Legacy rows predate prospective contract identity. Keep them in an
    # explicit unusable cohort rather than laundering them into today's one.
    cur.execute(
        "UPDATE ghost_checklist_snapshots SET outcome_contract=%s "
        "WHERE outcome_contract IS NULL",
        (LEGACY_OUTCOME_CONTRACT,),
    )
    cur.execute(
        "ALTER TABLE ghost_checklist_snapshots "
        "ALTER COLUMN outcome_contract SET NOT NULL"
    )
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_checklist_snapshots_prediction "
        "ON ghost_checklist_snapshots (prediction_id) "
        "WHERE prediction_id IS NOT NULL"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_checklist_snapshots_symbol_time "
        "ON ghost_checklist_snapshots (symbol, issued_at DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_checklist_snapshots_score_outcome "
        "ON ghost_checklist_snapshots (score_pct, outcome) "
        "WHERE outcome IS NOT NULL"
    )


def _now() -> int:
    return int(time.time())


def store_snapshot_with_cursor(
    cur,
    *,
    symbol: str,
    direction: str,
    report: Dict[str, Any],
    evidence: Dict[str, Any],
    issued_at: int,
    entry_price: Optional[float] = None,
    target_price: Optional[float] = None,
    stop_price: Optional[float] = None,
    deadline_ts: Optional[int] = None,
    prediction_id: Optional[int] = None,
    outcome_contract: str = DEFAULT_OUTCOME_CONTRACT,
) -> int:
    """Insert one immutable issue-time snapshot using the caller's transaction.

    The caller supplies the exact prediction timestamp.  ``prediction_id`` is
    idempotent: a retry returns the already-linked row instead of recollecting
    or replacing evidence.
    """
    validate_outcome_contract()
    issued = int(issued_at)
    cur.execute(
        """
        INSERT INTO ghost_checklist_snapshots
            (symbol, direction, checklist_version, outcome_contract, issued_at,
             hold_bars, score_pct, blocked, entry_price, target_price,
             stop_price, deadline_ts, evidence_json, report_json,
             prediction_id, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (prediction_id) WHERE prediction_id IS NOT NULL
        DO NOTHING
        RETURNING id
        """,
        (
            (symbol or "").upper(),
            (direction or "").upper(),
            report.get("checklist_version"),
            outcome_contract,
            issued,
            report.get("hold_bars"),
            report.get("score_pct"),
            bool(report.get("blocked")),
            entry_price,
            target_price,
            stop_price,
            deadline_ts,
            json.dumps(evidence, default=str),
            json.dumps(report, default=str),
            prediction_id,
            issued,
        ),
    )
    inserted = cur.fetchone()
    if inserted is not None:
        return int(inserted[0])
    if prediction_id is None:
        raise RuntimeError("checklist snapshot insert returned no id")
    cur.execute(
        "SELECT id FROM ghost_checklist_snapshots WHERE prediction_id=%s",
        (prediction_id,),
    )
    existing = cur.fetchone()
    if existing is None:
        raise RuntimeError("checklist snapshot idempotency lookup failed")
    return int(existing[0])


def store_snapshot(
    *,
    symbol: str,
    direction: str,
    report: Dict[str, Any],
    evidence: Dict[str, Any],
    issued_at: int,
    entry_price: Optional[float] = None,
    target_price: Optional[float] = None,
    stop_price: Optional[float] = None,
    deadline_ts: Optional[int] = None,
    prediction_id: Optional[int] = None,
    outcome_contract: str = DEFAULT_OUTCOME_CONTRACT,
) -> int:
    """Persist one immutable snapshot with an exact caller-supplied issue time."""
    from core.db import db_conn

    with db_conn() as conn:
        cur = conn.cursor()
        row_id = store_snapshot_with_cursor(
            cur,
            symbol=symbol,
            direction=direction,
            report=report,
            evidence=evidence,
            issued_at=issued_at,
            entry_price=entry_price,
            target_price=target_price,
            stop_price=stop_price,
            deadline_ts=deadline_ts,
            prediction_id=prediction_id,
            outcome_contract=outcome_contract,
        )
        conn.commit()
        return row_id


def resolve_snapshot(row_id: int, *, outcome: str, resolved_price: Optional[float]) -> None:
    """Record what actually happened. Called once, after the hold window ends.

    ``outcome`` is one of WIN / LOSS / EXPIRED, matching the vocabulary the
    rest of Ghost's contract-70 machinery already uses (EXPIRED counts as a
    non-win, never dropped from the denominator).
    """
    from core.db import db_conn

    won = outcome == "WIN"
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE ghost_checklist_snapshots
               SET outcome=%s, resolved_at=%s, resolved_price=%s, won=%s
             WHERE id=%s AND outcome IS NULL
            """,
            (outcome, _now(), resolved_price, won, row_id),
        )
        conn.commit()
    _bust_calibration_cache()


def resolved_samples_for_calibration(
    *,
    checklist_version: str,
    hold_bars: int,
    outcome_contract: str,
    direction: str,
    symbol: Optional[str] = None,
    min_issued_before: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Resolved rows from one exact prospective calibration cohort only."""
    import time as _time

    cohort = (
        str(checklist_version), int(hold_bars), str(outcome_contract),
        str(direction).upper(), (symbol or "").upper() or None,
    )
    cache_key = repr(cohort)
    if min_issued_before is None:
        cached = _calib_cache.get(cache_key)
        if cached is not None and (_time.time() - cached[0]) < _CALIB_CACHE_TTL_S:
            return list(cached[1])

    from core.db import db_conn

    with db_conn() as conn:
        cur = conn.cursor()
        ensure_checklist_tables(cur)
        clauses = [
            "outcome IS NOT NULL",
            "checklist_version=%s",
            "hold_bars=%s",
            "outcome_contract=%s",
            "direction=%s",
        ]
        params: List[Any] = [cohort[0], cohort[1], cohort[2], cohort[3]]
        if cohort[4] is not None:
            clauses.append("symbol=%s")
            params.append(cohort[4])
        if min_issued_before is not None:
            # Historical confidence may use only information knowable when the
            # target pick was issued: both the sample and its outcome must
            # already have existed before that decision timestamp.
            clauses.append("issued_at < %s")
            params.append(int(min_issued_before))
            clauses.append("resolved_at < %s")
            params.append(int(min_issued_before))
        cur.execute(
            "SELECT score_pct, won FROM ghost_checklist_snapshots WHERE "
            + " AND ".join(clauses),
            tuple(params),
        )
        rows = [{"score_pct": row[0], "won": row[1]} for row in cur.fetchall()]
    if min_issued_before is None:
        _calib_cache[cache_key] = (_time.time(), list(rows))
    return rows


def snapshot_for_prediction(prediction_id: int) -> Optional[Dict[str, Any]]:
    """Return the immutable issue-time checklist linked to one prediction."""
    from core.db import db_conn

    with db_conn() as conn:
        cur = conn.cursor()
        ensure_checklist_tables(cur)
        cur.execute(
            """
            SELECT id, symbol, direction, checklist_version, outcome_contract,
                   issued_at, hold_bars, score_pct, blocked, entry_price,
                   target_price, stop_price, deadline_ts, evidence_json,
                   report_json, outcome, resolved_at, resolved_price, won,
                   prediction_id
              FROM ghost_checklist_snapshots
             WHERE prediction_id=%s
             LIMIT 1
            """,
            (int(prediction_id),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    cols = (
        "id", "symbol", "direction", "checklist_version", "outcome_contract",
        "issued_at", "hold_bars", "score_pct", "blocked", "entry_price",
        "target_price", "stop_price", "deadline_ts", "evidence", "report",
        "outcome", "resolved_at", "resolved_price", "won", "prediction_id",
    )
    return dict(zip(cols, row))


def recent_resolved_across_symbols(limit: int = 30) -> List[Dict[str, Any]]:
    """Every symbol's most recently resolved calls, newest first -- the Record tab."""
    from core.db import db_conn

    with db_conn() as conn:
        cur = conn.cursor()
        ensure_checklist_tables(cur)
        cur.execute(
            """
            SELECT symbol, direction, issued_at, score_pct, entry_price,
                   target_price, stop_price, outcome, resolved_at,
                   resolved_price, won, report_json
              FROM ghost_checklist_snapshots
             WHERE outcome IS NOT NULL
             ORDER BY resolved_at DESC
             LIMIT %s
            """,
            (max(1, min(200, int(limit))),),
        )
        cols = (
            "symbol", "direction", "issued_at", "score_pct", "entry_price",
            "target_price", "stop_price", "outcome", "resolved_at",
            "resolved_price", "won", "report",
        )
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def recent_snapshots(symbol: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Recent checklist history for one symbol, newest first -- for the Record tab."""
    from core.db import db_conn

    with db_conn() as conn:
        cur = conn.cursor()
        ensure_checklist_tables(cur)
        cur.execute(
            """
            SELECT id, direction, issued_at, score_pct, blocked, entry_price,
                   target_price, stop_price, deadline_ts, outcome, resolved_at,
                   resolved_price, won, report_json
              FROM ghost_checklist_snapshots
             WHERE symbol=%s
             ORDER BY issued_at DESC
             LIMIT %s
            """,
            ((symbol or "").upper(), max(1, min(200, int(limit)))),
        )
        cols = (
            "id", "direction", "issued_at", "score_pct", "blocked", "entry_price",
            "target_price", "stop_price", "deadline_ts", "outcome", "resolved_at",
            "resolved_price", "won", "report",
        )
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _norm_direction(raw: Optional[str]) -> Optional[str]:
    d = (raw or "").strip().upper()
    if d in ("UP", "LONG", "BUY", "CALL"):
        return "UP"
    if d in ("DOWN", "SHORT", "SELL", "PUT"):
        return "DOWN"
    return None


def snapshot_open_predictions(limit: int = 25) -> Dict[str, Any]:
    """Retired: delayed evidence reconstruction is intentionally forbidden.

    Snapshots are written synchronously inside the prediction transaction.
    Existing callers receive an honest no-op rather than a current-state
    reconstruction mislabeled as issue-time evidence.
    """
    return {
        "ok": False,
        "retired": True,
        "snapshotted": 0,
        "failed": 0,
        "scanned": 0,
        "reason": "checklists_are_frozen_at_prediction_issuance",
    }


def resolve_open_snapshots(limit: int = 100) -> Dict[str, Any]:
    """Scheduler job: copy resolved prediction outcomes onto their snapshots.

    The predictions table is the single source of outcome truth (WIN / LOSS /
    EXPIRED, resolved by the existing TP/SL machinery); this job never decides
    an outcome itself, it only records what that machinery already concluded.
    EXPIRED stays in the denominator as a non-win, matching contract-70.
    """
    from core.db import db_conn

    resolved = 0
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_checklist_tables(cur)
        cur.execute(
            """
            SELECT s.id, p.outcome, p.exit_price
              FROM ghost_checklist_snapshots s
              JOIN predictions p ON p.id = s.prediction_id
             WHERE s.outcome IS NULL
               AND p.outcome IN ('WIN', 'LOSS', 'EXPIRED')
             LIMIT %s
            """,
            (max(1, min(500, int(limit))),),
        )
        pairs = cur.fetchall()

    for snap_id, outcome, exit_price in pairs:
        try:
            resolve_snapshot(snap_id, outcome=outcome, resolved_price=exit_price)
            resolved += 1
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("checklist resolve failed for snapshot %s: %s", snap_id, str(exc)[:160])

    return {"ok": True, "resolved": resolved, "pending_checked": len(pairs)}
