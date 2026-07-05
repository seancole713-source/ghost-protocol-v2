"""Super Ghost Point-in-Time Feature Store (PR #100).

Guarantees that learning, lab comparisons, feature attribution, and future model
training can trace predictions back to what Ghost knew *at prediction time*.

Why this matters
----------------
A self-improving market intelligence system is only trustworthy if it never
learns from future information by accident. This module stores immutable feature
snapshots and audits timestamps for leakage.

It does not trade. It does not predict. It preserves evidence.
"""
from __future__ import annotations
from core.quiet import note_suppressed

import json
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

LOGGER = logging.getLogger("ghost.super_ghost_feature_store")

TIMESTAMP_KEYS = {
    "ts", "timestamp", "time", "created_at", "published_at", "providerPublishTime",
    "provider_publish_time", "as_of", "as_of_ts", "checked_at", "updated_at",
    "filed_at", "filing_date", "filingDate", "date", "market_date",
}

DEFAULT_MODEL_ID = "super_ghost_checklist_v1"
DEFAULT_FEATURE_SET_ID = "super_ghost_25_point_v1"


def _now() -> int:
    return int(time.time())


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


def parse_source_ts(v: Any) -> Optional[int]:
    """Parse a source timestamp into Unix seconds UTC.

    Handles Unix seconds, milliseconds, ISO datetimes, and YYYY-MM-DD dates.
    Returns None if the value cannot be safely interpreted as a timestamp.
    """
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        if not math.isfinite(float(v)):
            return None
        n = float(v)
        if n > 10_000_000_000:  # milliseconds
            n = n / 1000.0
        if n < 0:
            return None
        return int(n)
    s = str(v).strip()
    if not s:
        return None
    # Numeric string.
    try:
        if s.replace(".", "", 1).isdigit():
            return parse_source_ts(float(s))
    except Exception:
        note_suppressed()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        note_suppressed()
    try:
        dt = datetime.fromisoformat(s[:10]).replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def _walk_timestamps(obj: Any, *, path: str = "") -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if str(k) in TIMESTAMP_KEYS:
                ts = parse_source_ts(v)
                if ts is not None:
                    out.append({"path": p, "key": str(k), "value": v, "ts": ts})
            out.extend(_walk_timestamps(v, path=p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_walk_timestamps(v, path=f"{path}[{i}]"))
    return out


def _compact_checklist(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for item in report.get("checklist") or []:
        if not isinstance(item, dict):
            continue
        rows.append({
            "id": item.get("id"),
            "key": item.get("key"),
            "category": item.get("category"),
            "source": item.get("source"),
            "available": item.get("available"),
            "status": item.get("status"),
            "score": item.get("score"),
            "weight": item.get("weight"),
            "confidence": item.get("confidence"),
            "value": item.get("value"),
            "evidence": item.get("evidence"),
        })
    return rows


def build_feature_snapshot(report: Dict[str, Any], *, prediction_ts: Optional[int] = None, extra_sources: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build an immutable point-in-time snapshot from a Super Ghost report."""
    pred_ts = int(prediction_ts or report.get("ts") or _now())
    symbol = str(report.get("symbol") or "").upper()
    payload = {
        "symbol": symbol,
        "prediction_ts": pred_ts,
        "model_id": DEFAULT_MODEL_ID,
        "feature_set_id": DEFAULT_FEATURE_SET_ID,
        "engine": report.get("engine"),
        "prediction": report.get("prediction"),
        "coverage": report.get("coverage"),
        "risk_plan": report.get("risk_plan"),
        "market_regime": report.get("market_regime"),
        "top_drivers": report.get("top_drivers"),
        "learning_adjustment": report.get("learning_adjustment"),
        "checklist": _compact_checklist(report),
        "extra_sources": extra_sources or {},
    }
    stamps = _walk_timestamps(payload)
    # Source timestamps are allowed to equal prediction_ts; anything later is leakage.
    future = [s for s in stamps if int(s["ts"]) > pred_ts]
    max_source_ts = max([int(s["ts"]) for s in stamps], default=pred_ts)
    # Feature-as-of is the latest source timestamp not after prediction_ts; if no
    # source timestamps exist, use prediction_ts (snapshot still point-in-time).
    valid_ts = [int(s["ts"]) for s in stamps if int(s["ts"]) <= pred_ts]
    feature_asof_ts = max(valid_ts, default=pred_ts)
    return {
        "ok": True,
        "symbol": symbol,
        "prediction_ts": pred_ts,
        "feature_asof_ts": feature_asof_ts,
        "max_source_ts": max_source_ts,
        "source_time_ok": len(future) == 0,
        "leak_count": len(future),
        "source_count": len(stamps),
        "future_sources": future[:20],
        "source_index": stamps[:200],
        "model_id": DEFAULT_MODEL_ID,
        "feature_set_id": DEFAULT_FEATURE_SET_ID,
        "snapshot": payload,
    }


def ensure_feature_store_tables(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS super_ghost_feature_snapshots (
            id SERIAL PRIMARY KEY,
            ledger_id INT,
            symbol VARCHAR(20) NOT NULL,
            prediction_ts BIGINT NOT NULL,
            feature_asof_ts BIGINT NOT NULL,
            max_source_ts BIGINT,
            source_time_ok BOOLEAN NOT NULL,
            leak_count INT NOT NULL DEFAULT 0,
            source_count INT NOT NULL DEFAULT 0,
            model_id VARCHAR(80) NOT NULL,
            feature_set_id VARCHAR(80) NOT NULL,
            snapshot_json JSONB,
            source_index_json JSONB,
            created_at BIGINT NOT NULL,
            UNIQUE (ledger_id)
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sg_feature_snapshots_symbol ON super_ghost_feature_snapshots(symbol, prediction_ts DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sg_feature_snapshots_leak ON super_ghost_feature_snapshots(source_time_ok, leak_count)")


def persist_feature_snapshot(cur, report: Dict[str, Any], *, ledger_id: Optional[int] = None, prediction_ts: Optional[int] = None, extra_sources: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    ensure_feature_store_tables(cur)
    snap = build_feature_snapshot(report, prediction_ts=prediction_ts, extra_sources=extra_sources)
    cur.execute(
        """
        INSERT INTO super_ghost_feature_snapshots (
            ledger_id, symbol, prediction_ts, feature_asof_ts, max_source_ts,
            source_time_ok, leak_count, source_count, model_id, feature_set_id,
            snapshot_json, source_index_json, created_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)
        ON CONFLICT (ledger_id) DO UPDATE SET
            prediction_ts=EXCLUDED.prediction_ts,
            feature_asof_ts=EXCLUDED.feature_asof_ts,
            max_source_ts=EXCLUDED.max_source_ts,
            source_time_ok=EXCLUDED.source_time_ok,
            leak_count=EXCLUDED.leak_count,
            source_count=EXCLUDED.source_count,
            snapshot_json=EXCLUDED.snapshot_json,
            source_index_json=EXCLUDED.source_index_json
        RETURNING id
        """,
        (
            ledger_id, snap["symbol"], snap["prediction_ts"], snap["feature_asof_ts"], snap["max_source_ts"],
            snap["source_time_ok"], snap["leak_count"], snap["source_count"], snap["model_id"], snap["feature_set_id"],
            _jsonb(snap["snapshot"]), _jsonb(snap["source_index"]), _now(),
        ),
    )
    row = cur.fetchone()
    snap["snapshot_id"] = int(row[0]) if row else None
    return snap


def latest_snapshots(*, symbol: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_feature_store_tables(cur)
            where = "1=1"
            params: List[Any] = []
            if symbol:
                where += " AND symbol=%s"
                params.append(symbol.upper())
            cur.execute(
                f"""
                SELECT id, ledger_id, symbol, prediction_ts, feature_asof_ts, max_source_ts,
                       source_time_ok, leak_count, source_count, model_id, feature_set_id, created_at
                FROM super_ghost_feature_snapshots
                WHERE {where}
                ORDER BY prediction_ts DESC
                LIMIT %s
                """,
                params + [max(1, min(500, int(limit)))],
            )
            rows = cur.fetchall()
        return {"ok": True, "symbol": (symbol or "ALL").upper(), "count": len(rows), "snapshots": [
            {"id": r[0], "ledger_id": r[1], "symbol": r[2], "prediction_ts": r[3], "feature_asof_ts": r[4], "max_source_ts": r[5], "source_time_ok": r[6], "leak_count": r[7], "source_count": r[8], "model_id": r[9], "feature_set_id": r[10], "created_at": r[11]}
            for r in rows
        ]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160], "snapshots": []}


def leakage_audit(*, symbol: Optional[str] = None, limit: int = 200) -> Dict[str, Any]:
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_feature_store_tables(cur)
            where = "1=1"
            params: List[Any] = []
            if symbol:
                where += " AND symbol=%s"
                params.append(symbol.upper())
            cur.execute(
                f"""
                SELECT id, ledger_id, symbol, prediction_ts, feature_asof_ts, max_source_ts,
                       source_time_ok, leak_count, source_count, source_index_json
                FROM super_ghost_feature_snapshots
                WHERE {where}
                ORDER BY prediction_ts DESC
                LIMIT %s
                """,
                params + [max(1, min(1000, int(limit)))],
            )
            rows = cur.fetchall()
        total = len(rows)
        leaks = []
        for r in rows:
            if not r[6] or int(r[7] or 0) > 0:
                leaks.append({
                    "id": r[0], "ledger_id": r[1], "symbol": r[2], "prediction_ts": r[3],
                    "feature_asof_ts": r[4], "max_source_ts": r[5], "leak_count": r[7],
                    "source_index": _coerce_json(r[9])[:20] if isinstance(_coerce_json(r[9]), list) else _coerce_json(r[9]),
                })
        return {"ok": True, "symbol": (symbol or "ALL").upper(), "checked": total, "leak_count": len(leaks), "status": "leak" if leaks else "clean", "leaks": leaks[:20]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160], "checked": 0, "leak_count": 0, "leaks": []}
