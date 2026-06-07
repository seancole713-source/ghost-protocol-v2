"""Phase 3 gate: point-in-time feature audit trail (feature_asof_ts).

Every v3 feature vector must record the timestamp of the last bar used to compute
it. The 12-column ingestion table builds on this — no external feature row may
land without a verified as-of timestamp.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

LOGGER = logging.getLogger("ghost.feature_schema")

FEATURE_ASOF_KEY = "feature_asof_ts"


def feature_asof_unix(bar_ts: Any) -> int:
    """Parse an OHLCV bar timestamp to Unix seconds (UTC)."""
    if bar_ts is None:
        return int(time.time())
    if isinstance(bar_ts, (int, float)) and bar_ts > 1_000_000_000:
        return int(bar_ts)
    try:
        s = str(bar_ts).replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:
        return int(time.time())


def attach_feature_asof(features: Dict[str, Any], bar_ts: Any) -> Dict[str, Any]:
    """Set feature_asof_ts on a feature dict in place and return it."""
    features[FEATURE_ASOF_KEY] = feature_asof_unix(bar_ts)
    return features


def ensure_feature_snapshot_table(cur) -> None:
    """DDL for future 12-column ingestion; snapshots v3 issuance vectors today."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_feature_snapshots (
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            feature_asof_ts BIGINT NOT NULL,
            source TEXT NOT NULL DEFAULT 'v3_live',
            payload JSONB,
            created_at BIGINT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_feature_snapshots_symbol_asof
        ON ghost_feature_snapshots (symbol, feature_asof_ts DESC)
        """
    )


def persist_feature_snapshot(
    cur,
    *,
    symbol: str,
    feature_asof_ts: int,
    payload: Optional[Dict[str, Any]] = None,
    source: str = "v3_live",
    prediction_id: Optional[int] = None,
) -> None:
    """Best-effort insert; never blocks pick save."""
    ensure_feature_snapshot_table(cur)
    body = dict(payload or {})
    if prediction_id is not None:
        body["prediction_id"] = prediction_id
    cur.execute(
        """
        INSERT INTO ghost_feature_snapshots
            (symbol, feature_asof_ts, source, payload, created_at)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            (symbol or "").upper(),
            int(feature_asof_ts),
            source,
            json.dumps(body),
            int(time.time()),
        ),
    )
