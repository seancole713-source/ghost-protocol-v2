"""Persistent backend performance log — full prediction-cycle detail for ops and improvement.

Three layers:
  ghost_perf_cycles       — one row per scan cycle (summary + JSON context)
  ghost_perf_symbol_evals — per-symbol gate outcome within a cycle
  ghost_perf_events       — pick lifecycle (saved, resolved, expired)

Read via GET /api/wolf/performance-log/* (wolf_app.py).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("ghost.performance_log")

_RETENTION_DAYS = max(7, int(os.getenv("GHOST_PERF_RETENTION_DAYS", "90")))


def perf_log_enabled() -> bool:
    return (os.getenv("GHOST_PERF_LOG", "on") or "on").strip().lower() not in (
        "0", "off", "false", "no",
    )


def ensure_perf_tables(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_perf_cycles (
            id SERIAL PRIMARY KEY,
            cycle_ts BIGINT NOT NULL,
            duration_ms INT,
            scanned INT NOT NULL DEFAULT 0,
            candidates INT NOT NULL DEFAULT 0,
            saved INT NOT NULL DEFAULT 0,
            dedup_blocked INT NOT NULL DEFAULT 0,
            would_fire BOOLEAN NOT NULL DEFAULT FALSE,
            binding_skip TEXT,
            paused BOOLEAN NOT NULL DEFAULT FALSE,
            pause_reason TEXT,
            suppressed INT NOT NULL DEFAULT 0,
            suppress_reason TEXT,
            skip_counts JSONB,
            near_miss JSONB,
            regime JSONB,
            circuit_breaker JSONB,
            objective_mode JSONB,
            risk_block JSONB,
            saved_prediction_ids JSONB,
            created_at BIGINT NOT NULL
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_perf_cycles_ts ON ghost_perf_cycles (cycle_ts DESC)"
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_perf_symbol_evals (
            id SERIAL PRIMARY KEY,
            cycle_id INT NOT NULL REFERENCES ghost_perf_cycles(id) ON DELETE CASCADE,
            symbol TEXT NOT NULL,
            skip_code TEXT,
            fired BOOLEAN NOT NULL DEFAULT FALSE,
            saved BOOLEAN NOT NULL DEFAULT FALSE,
            prediction_id INT,
            direction TEXT,
            up_prob FLOAT,
            confidence FLOAT,
            confidence_floor FLOAT,
            min_win_proba FLOAT,
            entry_price FLOAT,
            target_price FLOAT,
            stop_price FLOAT,
            regime_label TEXT,
            scores JSONB,
            eval_ts BIGINT NOT NULL
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_perf_evals_cycle ON ghost_perf_symbol_evals (cycle_id)"
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_perf_evals_symbol_ts
        ON ghost_perf_symbol_evals (symbol, eval_ts DESC)
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_perf_events (
            id SERIAL PRIMARY KEY,
            event_ts BIGINT NOT NULL,
            event_type TEXT NOT NULL,
            prediction_id INT,
            symbol TEXT,
            cycle_id INT,
            payload JSONB,
            created_at BIGINT NOT NULL
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_perf_events_ts ON ghost_perf_events (event_ts DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_perf_events_pred ON ghost_perf_events (prediction_id)"
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_perf_events_type_ts
        ON ghost_perf_events (event_type, event_ts DESC)
        """
    )


def _jsonb(val: Any) -> Optional[str]:
    if val is None:
        return None
    return json.dumps(val, default=str)


def _trim_scores(scores: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(scores, dict):
        return {}
    out = dict(scores)
    feats = out.get("features")
    if isinstance(feats, dict) and len(feats) > 24:
        out["features"] = {k: feats[k] for k in list(feats)[:24]}
    return out


def symbol_eval_from_scan(
    symbol: str,
    pick: Optional[Dict[str, Any]],
    skip: Optional[str],
    scores: Dict[str, Any],
    eval_ts: int,
) -> Dict[str, Any]:
    meta = scores.get("model_meta") if isinstance(scores.get("model_meta"), dict) else {}
    regime = scores.get("regime") if isinstance(scores.get("regime"), dict) else {}
    return {
        "symbol": symbol,
        "skip_code": skip,
        "fired": pick is not None,
        "saved": False,
        "prediction_id": None,
        "direction": pick.get("direction") if pick else None,
        "up_prob": scores.get("up_prob"),
        "confidence": scores.get("confidence") or (pick.get("confidence") if pick else None),
        "confidence_floor": scores.get("confidence_floor"),
        "min_win_proba": meta.get("min_win_proba"),
        "entry_price": pick.get("entry_price") if pick else None,
        "target_price": pick.get("target_price") if pick else None,
        "stop_price": pick.get("stop_price") if pick else None,
        "regime_label": regime.get("label"),
        "scores": _trim_scores(scores),
        "eval_ts": eval_ts,
    }


def log_prediction_cycle(
    cur,
    *,
    cycle_ts: int,
    duration_ms: Optional[int],
    scanned: int,
    candidates: int,
    saved: int,
    dedup_blocked: int,
    would_fire: bool,
    binding_skip: Optional[str],
    paused: bool,
    pause_reason: Optional[str],
    suppressed: int,
    suppress_reason: Optional[str],
    skip_counts: Dict[str, int],
    near_miss: Optional[Dict[str, Any]],
    regime: Dict[str, Any],
    circuit_breaker: Dict[str, Any],
    objective_mode: Dict[str, Any],
    risk_block: Optional[Dict[str, Any]],
    saved_prediction_ids: List[int],
    symbol_evals: List[Dict[str, Any]],
) -> Optional[int]:
    """Persist one cycle + symbol evals. Returns cycle_id."""
    if not perf_log_enabled():
        return None
    ensure_perf_tables(cur)
    now = int(time.time())
    cur.execute(
        """
        INSERT INTO ghost_perf_cycles (
            cycle_ts, duration_ms, scanned, candidates, saved, dedup_blocked,
            would_fire, binding_skip, paused, pause_reason, suppressed, suppress_reason,
            skip_counts, near_miss, regime, circuit_breaker, objective_mode, risk_block,
            saved_prediction_ids, created_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id
        """,
        (
            cycle_ts,
            duration_ms,
            scanned,
            candidates,
            saved,
            dedup_blocked,
            would_fire,
            binding_skip,
            paused,
            pause_reason,
            suppressed,
            suppress_reason,
            _jsonb(skip_counts or {}),
            _jsonb(near_miss),
            _jsonb(regime),
            _jsonb(circuit_breaker),
            _jsonb(objective_mode),
            _jsonb(risk_block),
            _jsonb(saved_prediction_ids),
            now,
        ),
    )
    cycle_id = cur.fetchone()[0]
    for ev in symbol_evals:
        cur.execute(
            """
            INSERT INTO ghost_perf_symbol_evals (
                cycle_id, symbol, skip_code, fired, saved, prediction_id, direction,
                up_prob, confidence, confidence_floor, min_win_proba,
                entry_price, target_price, stop_price, regime_label, scores, eval_ts
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                cycle_id,
                ev.get("symbol"),
                ev.get("skip_code"),
                bool(ev.get("fired")),
                bool(ev.get("saved")),
                ev.get("prediction_id"),
                ev.get("direction"),
                ev.get("up_prob"),
                ev.get("confidence"),
                ev.get("confidence_floor"),
                ev.get("min_win_proba"),
                ev.get("entry_price"),
                ev.get("target_price"),
                ev.get("stop_price"),
                ev.get("regime_label"),
                _jsonb(ev.get("scores") or {}),
                ev.get("eval_ts") or cycle_ts,
            ),
        )
    for pid in saved_prediction_ids:
        log_prediction_event(
            cur,
            event_type="pick_saved",
            event_ts=cycle_ts,
            prediction_id=pid,
            symbol=_symbol_for_prediction(cur, pid),
            cycle_id=cycle_id,
            payload={"source": "prediction_cycle"},
            skip_ensure=True,
        )
    log_prediction_event(
        cur,
        event_type="cycle_complete",
        event_ts=cycle_ts,
        prediction_id=None,
        symbol=None,
        cycle_id=cycle_id,
        payload={
            "scanned": scanned,
            "candidates": candidates,
            "saved": saved,
            "binding_skip": binding_skip,
        },
        skip_ensure=True,
    )
    maybe_prune(cur)
    return cycle_id


def _symbol_for_prediction(cur, prediction_id: int) -> Optional[str]:
    cur.execute("SELECT symbol FROM predictions WHERE id=%s", (prediction_id,))
    row = cur.fetchone()
    return row[0] if row else None


def log_prediction_event(
    cur,
    *,
    event_type: str,
    event_ts: int,
    prediction_id: Optional[int] = None,
    symbol: Optional[str] = None,
    cycle_id: Optional[int] = None,
    payload: Optional[Dict[str, Any]] = None,
    skip_ensure: bool = False,
) -> None:
    if not perf_log_enabled():
        return
    if not skip_ensure:
        ensure_perf_tables(cur)
    cur.execute(
        """
        INSERT INTO ghost_perf_events
            (event_ts, event_type, prediction_id, symbol, cycle_id, payload, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            event_ts,
            event_type,
            prediction_id,
            symbol,
            cycle_id,
            _jsonb(payload or {}),
            int(time.time()),
        ),
    )


def record_pick_resolution(
    prediction_id: int,
    symbol: str,
    outcome: str,
    *,
    exit_price: Optional[float] = None,
    pnl_pct: Optional[float] = None,
    source: str = "reconcile",
) -> None:
    """Log pick resolved/expired after predictions row is updated."""
    if not perf_log_enabled():
        return
    try:
        from core.db import db_conn

        event_type = "pick_withdrawn" if outcome == "WITHDRAWN" else (
            "pick_expired" if outcome == "EXPIRED" else "pick_resolved"
        )
        with db_conn() as conn:
            cur = conn.cursor()
            log_prediction_event(
                cur,
                event_type=event_type,
                event_ts=int(time.time()),
                prediction_id=int(prediction_id),
                symbol=symbol,
                payload={
                    "outcome": outcome,
                    "exit_price": exit_price,
                    "pnl_pct": pnl_pct,
                    "source": source,
                },
            )
    except Exception as exc:
        LOGGER.warning("perf log resolution skipped: %s", str(exc)[:80])


def maybe_prune(cur) -> None:
    """Drop rows older than retention window (cheap, runs occasionally)."""
    if int(time.time()) % 17 != 0:
        return
    cutoff = int(time.time()) - _RETENTION_DAYS * 86400
    cur.execute("DELETE FROM ghost_perf_cycles WHERE cycle_ts < %s", (cutoff,))


def _coerce_json(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return v


def fetch_cycles(limit: int = 50, offset: int = 0, since_ts: Optional[int] = None) -> Dict[str, Any]:
    from core.db import db_conn

    lim = max(1, min(200, int(limit)))
    off = max(0, int(offset))
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_perf_tables(cur)
        if since_ts:
            cur.execute(
                "SELECT COUNT(*) FROM ghost_perf_cycles WHERE cycle_ts >= %s",
                (since_ts,),
            )
            total = cur.fetchone()[0]
            cur.execute(
                """
                SELECT id, cycle_ts, duration_ms, scanned, candidates, saved, dedup_blocked,
                       would_fire, binding_skip, paused, pause_reason, suppressed, suppress_reason,
                       skip_counts, near_miss, saved_prediction_ids
                FROM ghost_perf_cycles
                WHERE cycle_ts >= %s
                ORDER BY cycle_ts DESC, id DESC
                LIMIT %s OFFSET %s
                """,
                (since_ts, lim, off),
            )
        else:
            cur.execute("SELECT COUNT(*) FROM ghost_perf_cycles")
            total = cur.fetchone()[0]
            cur.execute(
                """
                SELECT id, cycle_ts, duration_ms, scanned, candidates, saved, dedup_blocked,
                       would_fire, binding_skip, paused, pause_reason, suppressed, suppress_reason,
                       skip_counts, near_miss, saved_prediction_ids
                FROM ghost_perf_cycles
                ORDER BY cycle_ts DESC, id DESC
                LIMIT %s OFFSET %s
                """,
                (lim, off),
            )
        rows = cur.fetchall()
    cycles = []
    for r in rows:
        cycles.append({
            "id": r[0],
            "cycle_ts": r[1],
            "duration_ms": r[2],
            "scanned": r[3],
            "candidates": r[4],
            "saved": r[5],
            "dedup_blocked": r[6],
            "would_fire": bool(r[7]),
            "binding_skip": r[8],
            "paused": bool(r[9]),
            "pause_reason": r[10],
            "suppressed": r[11],
            "suppress_reason": r[12],
            "skip_counts": _coerce_json(r[13]) or {},
            "near_miss": _coerce_json(r[14]),
            "saved_prediction_ids": _coerce_json(r[15]) or [],
        })
    return {"total": total, "limit": lim, "offset": off, "cycles": cycles}


def fetch_cycle_detail(cycle_id: int, symbol_limit: int = 200) -> Optional[Dict[str, Any]]:
    from core.db import db_conn

    with db_conn() as conn:
        cur = conn.cursor()
        ensure_perf_tables(cur)
        cur.execute(
            """
            SELECT id, cycle_ts, duration_ms, scanned, candidates, saved, dedup_blocked,
                   would_fire, binding_skip, paused, pause_reason, suppressed, suppress_reason,
                   skip_counts, near_miss, regime, circuit_breaker, objective_mode, risk_block,
                   saved_prediction_ids, created_at
            FROM ghost_perf_cycles WHERE id=%s
            """,
            (int(cycle_id),),
        )
        row = cur.fetchone()
        if not row:
            return None
        cur.execute(
            """
            SELECT id, symbol, skip_code, fired, saved, prediction_id, direction,
                   up_prob, confidence, confidence_floor, min_win_proba,
                   entry_price, target_price, stop_price, regime_label, scores, eval_ts
            FROM ghost_perf_symbol_evals
            WHERE cycle_id=%s
            ORDER BY up_prob DESC NULLS LAST, symbol ASC
            LIMIT %s
            """,
            (int(cycle_id), max(1, min(500, int(symbol_limit)))),
        )
        eval_rows = cur.fetchall()
    cycle = {
        "id": row[0],
        "cycle_ts": row[1],
        "duration_ms": row[2],
        "scanned": row[3],
        "candidates": row[4],
        "saved": row[5],
        "dedup_blocked": row[6],
        "would_fire": bool(row[7]),
        "binding_skip": row[8],
        "paused": bool(row[9]),
        "pause_reason": row[10],
        "suppressed": row[11],
        "suppress_reason": row[12],
        "skip_counts": _coerce_json(row[13]) or {},
        "near_miss": _coerce_json(row[14]),
        "regime": _coerce_json(row[15]) or {},
        "circuit_breaker": _coerce_json(row[16]) or {},
        "objective_mode": _coerce_json(row[17]) or {},
        "risk_block": _coerce_json(row[18]),
        "saved_prediction_ids": _coerce_json(row[19]) or [],
        "created_at": row[20],
    }
    evals = []
    for e in eval_rows:
        evals.append({
            "id": e[0],
            "symbol": e[1],
            "skip_code": e[2],
            "fired": bool(e[3]),
            "saved": bool(e[4]),
            "prediction_id": e[5],
            "direction": e[6],
            "up_prob": e[7],
            "confidence": e[8],
            "confidence_floor": e[9],
            "min_win_proba": e[10],
            "entry_price": e[11],
            "target_price": e[12],
            "stop_price": e[13],
            "regime_label": e[14],
            "scores": _coerce_json(e[15]) or {},
            "eval_ts": e[16],
        })
    cycle["symbol_evals"] = evals
    cycle["symbol_eval_count"] = len(evals)
    return cycle


def fetch_symbol_eval_history(symbol: str, limit: int = 100, since_ts: Optional[int] = None) -> Dict[str, Any]:
    from core.db import db_conn

    sym = (symbol or "").strip().upper()
    lim = max(1, min(500, int(limit)))
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_perf_tables(cur)
        if since_ts:
            cur.execute(
                """
                SELECT e.id, e.cycle_id, e.symbol, e.skip_code, e.fired, e.saved, e.prediction_id,
                       e.direction, e.up_prob, e.confidence, e.confidence_floor, e.min_win_proba,
                       e.regime_label, e.eval_ts, c.binding_skip, c.saved
                FROM ghost_perf_symbol_evals e
                JOIN ghost_perf_cycles c ON c.id = e.cycle_id
                WHERE e.symbol=%s AND e.eval_ts >= %s
                ORDER BY e.eval_ts DESC, e.id DESC
                LIMIT %s
                """,
                (sym, since_ts, lim),
            )
        else:
            cur.execute(
                """
                SELECT e.id, e.cycle_id, e.symbol, e.skip_code, e.fired, e.saved, e.prediction_id,
                       e.direction, e.up_prob, e.confidence, e.confidence_floor, e.min_win_proba,
                       e.regime_label, e.eval_ts, c.binding_skip, c.saved
                FROM ghost_perf_symbol_evals e
                JOIN ghost_perf_cycles c ON c.id = e.cycle_id
                WHERE e.symbol=%s
                ORDER BY e.eval_ts DESC, e.id DESC
                LIMIT %s
                """,
                (sym, lim),
            )
        rows = cur.fetchall()
    evals = []
    for r in rows:
        evals.append({
            "id": r[0],
            "cycle_id": r[1],
            "symbol": r[2],
            "skip_code": r[3],
            "fired": bool(r[4]),
            "saved": bool(r[5]),
            "prediction_id": r[6],
            "direction": r[7],
            "up_prob": r[8],
            "confidence": r[9],
            "confidence_floor": r[10],
            "min_win_proba": r[11],
            "regime_label": r[12],
            "eval_ts": r[13],
            "cycle_binding_skip": r[14],
            "cycle_saved": r[15],
        })
    return {"symbol": sym, "count": len(evals), "evals": evals}


def fetch_events(
    limit: int = 100,
    offset: int = 0,
    event_type: Optional[str] = None,
    prediction_id: Optional[int] = None,
    since_ts: Optional[int] = None,
) -> Dict[str, Any]:
    from core.db import db_conn

    lim = max(1, min(500, int(limit)))
    off = max(0, int(offset))
    clauses = []
    params: List[Any] = []
    if event_type:
        clauses.append("event_type=%s")
        params.append(event_type.strip())
    if prediction_id is not None:
        clauses.append("prediction_id=%s")
        params.append(int(prediction_id))
    if since_ts:
        clauses.append("event_ts >= %s")
        params.append(since_ts)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_perf_tables(cur)
        cur.execute(f"SELECT COUNT(*) FROM ghost_perf_events{where}", tuple(params))
        total = cur.fetchone()[0]
        cur.execute(
            f"""
            SELECT id, event_ts, event_type, prediction_id, symbol, cycle_id, payload
            FROM ghost_perf_events{where}
            ORDER BY event_ts DESC, id DESC
            LIMIT %s OFFSET %s
            """,
            tuple(params) + (lim, off),
        )
        rows = cur.fetchall()
    events = []
    for r in rows:
        events.append({
            "id": r[0],
            "event_ts": r[1],
            "event_type": r[2],
            "prediction_id": r[3],
            "symbol": r[4],
            "cycle_id": r[5],
            "payload": _coerce_json(r[6]) or {},
        })
    return {"total": total, "limit": lim, "offset": off, "events": events}


def fetch_progress_summary(days: int = 7) -> Dict[str, Any]:
    """Open picks + recent lifecycle events + cycle rollups for tracking progress."""
    from core.db import db_conn

    days = max(1, min(90, int(days)))
    since = int(time.time()) - days * 86400
    from core.prediction_filters import V32_ERA_MIN_ID as v32_min
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_perf_tables(cur)
        cur.execute(
            """
            SELECT id, symbol, direction, confidence, entry_price, target_price, stop_price,
                   predicted_at, expires_at, outcome
            FROM predictions
            WHERE id >= %s AND outcome IS NULL AND expires_at > extract(epoch from now())
            ORDER BY predicted_at DESC NULLS LAST, id DESC
            LIMIT 50
            """,
            (v32_min,),
        )
        open_rows = cur.fetchall()
        cur.execute(
            """
            SELECT id, symbol, direction, confidence, outcome, pnl_pct, predicted_at, resolved_at
            FROM predictions
            WHERE id >= %s AND outcome IS NOT NULL AND COALESCE(resolved_at, predicted_at) >= %s
            ORDER BY resolved_at DESC NULLS LAST, id DESC
            LIMIT 100
            """,
            (v32_min, since),
        )
        resolved_rows = cur.fetchall()
        cur.execute(
            """
            SELECT
                COUNT(*) AS cycles,
                COALESCE(SUM(saved), 0) AS picks_saved,
                COALESCE(SUM(candidates), 0) AS candidates,
                COALESCE(SUM(CASE WHEN would_fire THEN 1 ELSE 0 END), 0) AS would_fire_cycles,
                COALESCE(AVG(duration_ms), 0) AS avg_duration_ms
            FROM ghost_perf_cycles
            WHERE cycle_ts >= %s
            """,
            (since,),
        )
        cycle_agg = cur.fetchone()
        cur.execute(
            """
            SELECT binding_skip, COUNT(*) AS n
            FROM ghost_perf_cycles
            WHERE cycle_ts >= %s AND binding_skip IS NOT NULL
            GROUP BY binding_skip
            ORDER BY n DESC
            LIMIT 12
            """,
            (since,),
        )
        binding_rows = cur.fetchall()
        cur.execute(
            """
            SELECT event_type, COUNT(*) AS n
            FROM ghost_perf_events
            WHERE event_ts >= %s
            GROUP BY event_type
            ORDER BY n DESC
            """,
            (since,),
        )
        event_agg = cur.fetchall()
    open_picks = [
        {
            "id": r[0], "symbol": r[1], "direction": r[2], "confidence": r[3],
            "entry_price": r[4], "target_price": r[5], "stop_price": r[6],
            "predicted_at": r[7], "expires_at": r[8], "outcome": r[9],
        }
        for r in open_rows
    ]
    recent_resolved = [
        {
            "id": r[0], "symbol": r[1], "direction": r[2], "confidence": r[3],
            "outcome": r[4], "pnl_pct": float(r[5]) if r[5] is not None else None,
            "predicted_at": r[6], "resolved_at": r[7],
        }
        for r in resolved_rows
    ]
    wins = sum(1 for r in recent_resolved if r["outcome"] == "WIN")
    losses = sum(1 for r in recent_resolved if r["outcome"] in ("LOSS", "EXPIRED"))
    n_res = wins + losses
    return {
        "days": days,
        "since_ts": since,
        "open_picks": open_picks,
        "open_count": len(open_picks),
        "recent_resolved": recent_resolved,
        "recent_resolved_count": len(recent_resolved),
        "recent_win_rate": round(wins / n_res, 4) if n_res else None,
        "cycles": {
            "count": int(cycle_agg[0] or 0),
            "picks_saved": int(cycle_agg[1] or 0),
            "candidates": int(cycle_agg[2] or 0),
            "would_fire_cycles": int(cycle_agg[3] or 0),
            "avg_duration_ms": round(float(cycle_agg[4] or 0), 1),
            "binding_gates": {r[0]: int(r[1]) for r in binding_rows},
        },
        "events_by_type": {r[0]: int(r[1]) for r in event_agg},
        "retention_days": _RETENTION_DAYS,
        "enabled": perf_log_enabled(),
    }
