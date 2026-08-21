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
            created_at BIGINT NOT NULL
        )
        """
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
                 deadline_ts, evidence_json, report_json, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                now,
            ),
        )
        row_id = cur.fetchone()[0]
        conn.commit()
        return int(row_id)


def resolve_snapshot(row_id: int, *, outcome: str, resolved_price: float) -> None:
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


def resolved_samples_for_calibration(*, min_issued_before: Optional[int] = None) -> List[Dict[str, Any]]:
    """Every resolved snapshot as ``{score_pct, won}`` for `checklist_calibration`.

    Only rows with a non-null outcome are returned -- a pick still inside its
    3-day hold window has no result yet and must not be counted as either.
    """
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
        return [{"score_pct": row[0], "won": row[1]} for row in cur.fetchall()]


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
