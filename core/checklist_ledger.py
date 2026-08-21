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

LOGGER = logging.getLogger("ghost.checklist_ledger")

# Calibration inputs change only when a snapshot resolves, so a short TTL
# cache spares the checklist endpoint a full-table scan per request (the
# Today tab fires up to 3 checklist reads in parallel per page load).
_CALIB_CACHE_TTL_S = 120
_calib_cache: Dict[str, Any] = {"ts": 0.0, "rows": None}


def _bust_calibration_cache() -> None:
    _calib_cache["ts"] = 0.0
    _calib_cache["rows"] = None


def ensure_checklist_tables(cur) -> None:
    """Create the snapshot table. Safe and idempotent."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_checklist_snapshots (
            id BIGSERIAL PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            direction VARCHAR(8) NOT NULL,
            checklist_version VARCHAR(40) NOT NULL,
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
    # Additive migration for tables created before prediction linkage existed.
    cur.execute(
        "ALTER TABLE ghost_checklist_snapshots "
        "ADD COLUMN IF NOT EXISTS prediction_id BIGINT"
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


def store_snapshot(
    *,
    symbol: str,
    direction: str,
    report: Dict[str, Any],
    evidence: Dict[str, Any],
    entry_price: Optional[float] = None,
    target_price: Optional[float] = None,
    stop_price: Optional[float] = None,
    deadline_ts: Optional[int] = None,
    prediction_id: Optional[int] = None,
) -> int:
    """Persist one checklist evaluation at issue time. Returns the row id.

    ``report`` is the dict `catalyst_checklist.evaluate_checklist` returned --
    stored verbatim so the exact evidence Ghost acted on can always be
    re-read later, independent of how the checklist logic evolves afterward.
    """
    from core.db import db_conn

    now = _now()
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_checklist_tables(cur)
        cur.execute(
            """
            INSERT INTO ghost_checklist_snapshots
                (symbol, direction, checklist_version, issued_at, hold_bars,
                 score_pct, blocked, entry_price, target_price, stop_price,
                 deadline_ts, evidence_json, report_json, prediction_id,
                 created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                (symbol or "").upper(),
                (direction or "").upper(),
                report.get("checklist_version"),
                now,
                report.get("hold_bars"),
                report.get("score_pct"),
                bool(report.get("blocked")),
                entry_price,
                target_price,
                stop_price,
                deadline_ts,
                json.dumps(evidence),
                json.dumps(report),
                prediction_id,
                now,
            ),
        )
        row_id = cur.fetchone()[0]
        conn.commit()
        return int(row_id)


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


def resolved_samples_for_calibration(*, min_issued_before: Optional[int] = None) -> List[Dict[str, Any]]:
    """Every resolved snapshot as ``{score_pct, won}`` for `checklist_calibration`.

    Only rows with a non-null outcome are returned -- a pick still inside its
    3-day hold window has no result yet and must not be counted as either.
    """
    import time as _time

    if min_issued_before is None:
        cached = _calib_cache["rows"]
        if cached is not None and (_time.time() - _calib_cache["ts"]) < _CALIB_CACHE_TTL_S:
            return list(cached)

    from core.db import db_conn

    with db_conn() as conn:
        cur = conn.cursor()
        ensure_checklist_tables(cur)
        if min_issued_before is not None:
            cur.execute(
                """
                SELECT score_pct, won FROM ghost_checklist_snapshots
                 WHERE outcome IS NOT NULL AND issued_at < %s
                """,
                (min_issued_before,),
            )
        else:
            cur.execute(
                "SELECT score_pct, won FROM ghost_checklist_snapshots WHERE outcome IS NOT NULL"
            )
        rows = [{"score_pct": row[0], "won": row[1]} for row in cur.fetchall()]
    if min_issued_before is None:
        _calib_cache["rows"] = list(rows)
        _calib_cache["ts"] = _time.time()
    return rows


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
    """Scheduler job: capture a point-in-time checklist for every open pick.

    Finds unresolved stock predictions that have no snapshot yet, evaluates
    the checklist against evidence collected *now* (as close to issue time as
    the 15-minute scheduler allows), and stores one immutable snapshot per
    prediction. This is the write half of the calibration loop -- without it
    the completeness->win-rate table never accrues a sample.

    Never raises past its own boundary; one symbol failing must not starve
    the rest of the pass.
    """
    from core.db import db_conn

    snapshotted = 0
    failed = 0
    rows: List[tuple] = []
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_checklist_tables(cur)
        cur.execute(
            """
            SELECT p.id, p.symbol, p.direction, p.entry_price, p.target_price,
                   p.stop_price, p.expires_at
              FROM predictions p
             WHERE p.outcome IS NULL
               AND COALESCE(p.entry_price, 0) > 0
               AND COALESCE(p.asset_type, 'stock') = 'stock'
               AND NOT EXISTS (
                   SELECT 1 FROM ghost_checklist_snapshots s
                    WHERE s.prediction_id = p.id
               )
             ORDER BY p.predicted_at DESC NULLS LAST
             LIMIT %s
            """,
            (max(1, min(100, int(limit))),),
        )
        rows = cur.fetchall()

    for pid, sym, direction, entry, target, stop, expires in rows:
        norm = _norm_direction(direction)
        if norm is None:
            failed += 1
            LOGGER.warning("checklist snapshot: unknown direction %r for %s", direction, sym)
            continue
        try:
            from core.catalyst_checklist import evaluate_checklist
            from core.checklist_evidence import collect_evidence

            evidence = collect_evidence(sym)
            report = evaluate_checklist(sym, norm, evidence)
            store_snapshot(
                symbol=sym,
                direction=norm,
                report=report,
                evidence=evidence,
                entry_price=entry,
                target_price=target,
                stop_price=stop,
                deadline_ts=expires,
                prediction_id=pid,
            )
            snapshotted += 1
        except Exception as exc:  # noqa: BLE001 - isolate one symbol's failure
            failed += 1
            LOGGER.warning("checklist snapshot failed for %s: %s", sym, str(exc)[:160])

    return {"ok": True, "snapshotted": snapshotted, "failed": failed, "scanned": len(rows)}


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
