"""Phase 3 gate: point-in-time feature audit trail (feature_asof_ts).

Every v3 feature vector must record the timestamp of the last bar used to compute
it. The 12-column ingestion table builds on this — no external feature row may
land without a verified as-of timestamp.
"""
from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

LOGGER = logging.getLogger("ghost.feature_schema")

FEATURE_ASOF_KEY = "feature_asof_ts"


def feature_asof_unix(bar_ts: Any, *, default_now: bool = False) -> int:
    """Parse an OHLCV bar timestamp to Unix seconds (UTC).

    Naive ISO values are defined as UTC rather than host-local time, making
    historical evidence invariant across worker timezone configuration.

    Missing or malformed timestamps are INVALID EVIDENCE: by default they
    return 0 so point-in-time peer/fold filters fail closed (exclude the row)
    rather than admitting historical rows under a wall-clock "now" cutoff.

    Live issuance — where the last bar is genuinely current — may opt into a
    now-fallback via ``default_now=True``. Historical labeling/backtest paths
    must never do so, or a corrupt bar timestamp would leak future peers into
    an earlier fold.

    Millisecond-scale timestamps (>= 1e12) are rejected: they are almost
    certainly epoch-millis misused as seconds and would place the bar
    thousands of years in the future. Implausibly far-future timestamps
    (> now + 90 days) are also rejected so a single corrupt bar cannot
    manufacture a future peer cutoff.
    """
    if bar_ts is None:
        return int(time.time()) if default_now else 0
    if isinstance(bar_ts, bool):
        return 0
    if isinstance(bar_ts, (int, float)):
        v = float(bar_ts)
        if not math.isfinite(v) or v <= 1_000_000_000 or not v.is_integer():
            return 0
        if v >= 1e12:
            return 0  # millisecond-scale, not seconds
        if v > time.time() + 7_776_000:  # 90 days
            return 0
        return int(v)
    try:
        s = str(bar_ts).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(s)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        ts = int(parsed.timestamp())
        if ts <= 1_000_000_000 or ts > time.time() + 7_776_000:
            return 0
        return ts
    except Exception:
        return int(time.time()) if default_now else 0


def attach_feature_asof(
    features: Dict[str, Any], bar_ts: Any, *, default_now: bool = False,
) -> Dict[str, Any]:
    """Set feature_asof_ts on a feature dict in place and return it.

    ``default_now`` is forwarded to :func:`feature_asof_unix`; only live
    issuance should enable it. Historical/backtest callers keep the default so
    corrupt bar timestamps fail closed instead of stamping the current time.
    """
    features[FEATURE_ASOF_KEY] = feature_asof_unix(bar_ts, default_now=default_now)
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
