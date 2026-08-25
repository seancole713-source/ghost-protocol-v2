"""Immutable advisory ledger for externally discovered symbols and market context.

External evidence can improve detection visibility, but every row is explicitly
ineligible for trade decisions and cannot expand Ghost's official watchlist.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from typing import Any, Dict, List, Optional

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")
_MAX_RAW_JSON_BYTES = 16_384


def _json_text(value: Any, *, limit: int = _MAX_RAW_JSON_BYTES) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return json.dumps({"truncated": True, "sha256": hashlib.sha256(encoded).hexdigest()})


def _finite(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def ensure_external_context_tables(cur) -> None:
    """Create module-owned append-only external context tables."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_external_observations (
            id BIGSERIAL PRIMARY KEY,
            provider TEXT NOT NULL,
            provider_family TEXT NOT NULL,
            observation_id TEXT NOT NULL,
            screen TEXT NOT NULL,
            raw_symbol TEXT NOT NULL,
            symbol VARCHAR(20),
            source_ts BIGINT,
            received_ts BIGINT NOT NULL,
            source_age_s BIGINT,
            freshness TEXT NOT NULL,
            delayed BOOLEAN NOT NULL DEFAULT FALSE,
            validation_valid BOOLEAN NOT NULL,
            validation_reasons JSONB NOT NULL,
            in_official_watchlist BOOLEAN NOT NULL,
            quarantined BOOLEAN NOT NULL,
            rank INT,
            price DOUBLE PRECISION,
            move_pct DOUBLE PRECISION,
            volume DOUBLE PRECISION,
            avg_volume DOUBLE PRECISION,
            external_score DOUBLE PRECISION,
            payload_sha256 CHAR(64) NOT NULL,
            raw_payload JSONB,
            advisory_only BOOLEAN NOT NULL DEFAULT TRUE,
            decision_eligible BOOLEAN NOT NULL DEFAULT FALSE,
            created_at BIGINT NOT NULL,
            UNIQUE(provider, observation_id)
        )
        """
    )
    cur.execute(
        """CREATE INDEX IF NOT EXISTS idx_external_obs_symbol_ts
           ON ghost_external_observations(symbol, source_ts DESC, received_ts DESC)"""
    )
    cur.execute(
        """CREATE INDEX IF NOT EXISTS idx_external_obs_screen_ts
           ON ghost_external_observations(screen, received_ts DESC)"""
    )
    cur.execute(
        """CREATE INDEX IF NOT EXISTS idx_external_obs_received_ts
           ON ghost_external_observations(received_ts DESC)"""
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_market_context_snapshots (
            id BIGSERIAL PRIMARY KEY,
            snapshot_id TEXT NOT NULL UNIQUE,
            observed_at BIGINT,
            received_at BIGINT NOT NULL,
            status TEXT NOT NULL,
            observations JSONB NOT NULL,
            provider_status JSONB NOT NULL,
            payload_sha256 CHAR(64) NOT NULL,
            display_only BOOLEAN NOT NULL DEFAULT TRUE,
            decision_eligible BOOLEAN NOT NULL DEFAULT FALSE,
            created_at BIGINT NOT NULL
        )
        """
    )
    cur.execute(
        """CREATE INDEX IF NOT EXISTS idx_market_context_received
           ON ghost_market_context_snapshots(received_at DESC)"""
    )


def normalize_external_observation(
    *,
    provider: str,
    provider_family: str,
    screen: str,
    raw_symbol: Any,
    source_ts: Any,
    payload: Dict[str, Any],
    observation_id: Optional[str] = None,
    received_ts: Optional[int] = None,
    rank: Any = None,
    price: Any = None,
    move_pct: Any = None,
    volume: Any = None,
    avg_volume: Any = None,
    external_score: Any = None,
    max_age_s: int = 1800,
    delayed: bool = False,
) -> Dict[str, Any]:
    """Normalize and validate one provider row without inventing timestamps."""
    now = int(received_ts or time.time())
    raw = str(raw_symbol or "").strip()
    symbol = raw.upper()
    reasons: List[str] = []
    if not _SYMBOL_RE.fullmatch(symbol):
        reasons.append("invalid_symbol")
        symbol = ""
    try:
        observed = int(source_ts)
    except (TypeError, ValueError, OverflowError):
        observed = None
        reasons.append("missing_source_timestamp")
    age = None if observed is None else now - observed
    if age is not None:
        if age < -60:
            reasons.append("future_source_timestamp")
        elif age > max_age_s:
            reasons.append("stale_source_timestamp")
    p = _finite(price)
    if p is None or p <= 0:
        reasons.append("invalid_price")
        p = None
    move = _finite(move_pct)
    vol = _finite(volume)
    avg_vol = _finite(avg_volume)
    score = _finite(external_score)
    try:
        normalized_rank = int(rank) if rank is not None else None
    except (TypeError, ValueError, OverflowError):
        normalized_rank = None
    from config.symbols import OFFICIAL_WATCHLIST

    in_watchlist = symbol in frozenset(OFFICIAL_WATCHLIST)
    quarantined = bool(symbol and not in_watchlist)
    raw_text = _json_text(payload)
    payload_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    oid = str(observation_id or "").strip()
    if not oid:
        oid = hashlib.sha256(
            f"{provider}|{screen}|{symbol or raw}|{observed}|{payload_hash}".encode()
        ).hexdigest()
    freshness = "unknown" if age is None else "fresh" if 0 <= age <= max_age_s else "stale"
    return {
        "provider": str(provider).strip().lower(),
        "provider_family": str(provider_family).strip().lower(),
        "observation_id": oid[:200],
        "screen": str(screen).strip().lower()[:80],
        "raw_symbol": raw[:80],
        "symbol": symbol or None,
        "source_ts": observed,
        "received_ts": now,
        "source_age_s": age,
        "freshness": freshness,
        "delayed": bool(delayed),
        "validation_valid": not reasons,
        "validation_reasons": reasons,
        "in_official_watchlist": in_watchlist,
        "quarantined": quarantined,
        "rank": normalized_rank,
        "price": p,
        "move_pct": move,
        "volume": vol,
        "avg_volume": avg_vol,
        "external_score": score,
        "payload_sha256": payload_hash,
        "raw_payload": json.loads(raw_text),
        "advisory_only": True,
        "decision_eligible": False,
    }


def store_external_observation(row: Dict[str, Any], *, cur=None) -> bool:
    """Append one immutable row; return False when already recorded."""
    def _write(cursor) -> bool:
        cursor.execute(
            """
            INSERT INTO ghost_external_observations (
                provider, provider_family, observation_id, screen, raw_symbol,
                symbol, source_ts, received_ts, source_age_s, freshness, delayed,
                validation_valid, validation_reasons, in_official_watchlist,
                quarantined, rank, price, move_pct, volume, avg_volume,
                external_score, payload_sha256, raw_payload, advisory_only,
                decision_eligible, created_at
            ) VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s::jsonb,TRUE,FALSE,%s
            ) ON CONFLICT (provider, observation_id) DO NOTHING
            RETURNING id
            """,
            (
                row["provider"], row["provider_family"], row["observation_id"],
                row["screen"], row["raw_symbol"], row.get("symbol"),
                row.get("source_ts"), row["received_ts"], row.get("source_age_s"),
                row["freshness"], row["delayed"], row["validation_valid"],
                _json_text(row.get("validation_reasons") or []),
                row["in_official_watchlist"], row["quarantined"], row.get("rank"),
                row.get("price"), row.get("move_pct"), row.get("volume"),
                row.get("avg_volume"), row.get("external_score"),
                row["payload_sha256"], _json_text(row.get("raw_payload")),
                row["received_ts"],
            ),
        )
        return cursor.fetchone() is not None

    if cur is not None:
        return _write(cur)
    from core.db import db_conn
    with db_conn() as conn:
        return _write(conn.cursor())


def _current_freshness(
    source_ts: Any,
    *,
    now_ts: Optional[int] = None,
    max_age_s: Optional[int] = None,
) -> tuple[Optional[int], str]:
    """Derive freshness at read time so persisted labels cannot age incorrectly."""
    if source_ts is None:
        return None, "unknown"
    now = int(time.time()) if now_ts is None else int(now_ts)
    try:
        age = now - int(source_ts)
    except (TypeError, ValueError, OverflowError):
        return None, "unknown"
    max_age = max(300, int(max_age_s or os.getenv("EXTERNAL_SCREENER_MAX_AGE_S", "1800")))
    return age, "fresh" if 0 <= age <= max_age else "stale"


def external_observations_for_symbol(symbol: str, cur, *, limit: int = 50) -> List[Dict[str, Any]]:
    """Read external discovery rows for the unified symbol timeline."""
    cur.execute(
        """SELECT source_ts, received_ts, provider, provider_family, screen,
                  freshness, delayed, validation_valid, validation_reasons,
                  in_official_watchlist, quarantined, rank, price, move_pct,
                  volume, avg_volume, external_score, observation_id
           FROM ghost_external_observations
           WHERE symbol=%s ORDER BY COALESCE(source_ts, received_ts) DESC LIMIT %s""",
        (symbol.upper(), max(1, min(200, int(limit)))),
    )
    rows = cur.fetchall()
    items: List[Dict[str, Any]] = []
    now = int(time.time())
    for row in rows:
        source_age_s, freshness = _current_freshness(row[0], now_ts=now)
        items.append({
            "ts": row[0] or row[1], "source_ts": row[0], "received_ts": row[1],
            "source_age_s": source_age_s,
            "provider": row[2], "provider_family": row[3], "screen": row[4],
            "freshness": freshness, "delayed": bool(row[6]),
            "validation_valid": bool(row[7]), "validation_reasons": row[8] or [],
            "in_official_watchlist": bool(row[9]), "quarantined": bool(row[10]),
            "rank": row[11], "price": row[12], "move_pct": row[13],
            "volume": row[14], "avg_volume": row[15], "external_score": row[16],
            "observation_id": row[17], "advisory_only": True,
            "decision_eligible": False,
        })
    return items


def recent_external_discoveries(*, limit: int = 50) -> Dict[str, Any]:
    """Latest validated discovery snapshot for UI/API display only."""
    from core.db import db_conn
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT provider, screen, symbol, source_ts, received_ts, rank,
                      price, move_pct, volume, avg_volume, external_score,
                      in_official_watchlist, quarantined, delayed, freshness
               FROM ghost_external_observations
               WHERE validation_valid=TRUE
               ORDER BY COALESCE(source_ts, received_ts) DESC, rank ASC
               LIMIT %s""",
            (max(1, min(200, int(limit))),),
        )
        rows = cur.fetchall()
    now = int(time.time())
    items = []
    for row in rows:
        source_age_s, freshness = _current_freshness(row[3], now_ts=now)
        items.append({
            "provider": row[0], "screen": row[1], "symbol": row[2],
            "source_ts": row[3], "received_ts": row[4],
            "source_age_s": source_age_s, "rank": row[5],
            "price": row[6], "move_pct": row[7], "volume": row[8],
            "avg_volume": row[9], "external_score": row[10],
            "in_official_watchlist": bool(row[11]), "quarantined": bool(row[12]),
            "delayed": bool(row[13]), "freshness": freshness,
            "advisory_only": True, "decision_eligible": False,
        })
    return {"ok": True, "items": items, "count": len(items), "advisory_only": True,
            "decision_eligible": False}


def prune_external_context(*, now_ts: Optional[int] = None, cur=None) -> Dict[str, int]:
    """Bound advisory storage while retaining enough history for forensics."""
    now = int(time.time()) if now_ts is None else int(now_ts)
    observation_days = max(7, int(os.getenv("EXTERNAL_CONTEXT_RETENTION_DAYS", "30")))
    snapshot_days = max(2, int(os.getenv("MARKET_CONTEXT_RETENTION_DAYS", "7")))

    def _prune(cursor) -> Dict[str, int]:
        cursor.execute(
            "DELETE FROM ghost_external_observations WHERE received_ts < %s",
            (now - observation_days * 86400,),
        )
        observations_deleted = max(0, int(cursor.rowcount or 0))
        cursor.execute(
            "DELETE FROM ghost_market_context_snapshots WHERE received_at < %s",
            (now - snapshot_days * 86400,),
        )
        snapshots_deleted = max(0, int(cursor.rowcount or 0))
        return {
            "observations_deleted": observations_deleted,
            "snapshots_deleted": snapshots_deleted,
        }

    if cur is not None:
        return _prune(cur)
    from core.db import db_conn
    with db_conn() as conn:
        return _prune(conn.cursor())


def store_market_context_snapshot(snapshot: Dict[str, Any], *, cur=None) -> bool:
    """Persist a display-only market snapshot, idempotently."""
    observations = snapshot.get("observations") or []
    provider_status = snapshot.get("provider_status") or {}
    canonical = _json_text({"observations": observations, "provider_status": provider_status})
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    received = int(snapshot.get("received_at") or time.time())
    snapshot_id = str(snapshot.get("snapshot_id") or f"market:{received // 60}:{digest[:16]}")

    def _write(cursor) -> bool:
        cursor.execute(
            """INSERT INTO ghost_market_context_snapshots (
                   snapshot_id, observed_at, received_at, status, observations,
                   provider_status, payload_sha256, display_only,
                   decision_eligible, created_at
               ) VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,TRUE,FALSE,%s)
               ON CONFLICT (snapshot_id) DO NOTHING RETURNING id""",
            (snapshot_id, snapshot.get("observed_at"), received,
             snapshot.get("status") or "unknown", _json_text(observations),
             _json_text(provider_status), digest, received),
        )
        return cursor.fetchone() is not None

    if cur is not None:
        return _write(cur)
    from core.db import db_conn
    with db_conn() as conn:
        return _write(conn.cursor())


def latest_market_context(*, now_ts: Optional[int] = None) -> Dict[str, Any]:
    """Read and re-age the last snapshot; never calls an external provider."""
    from core.db import db_conn
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT snapshot_id, observed_at, received_at, status,
                      observations, provider_status
               FROM ghost_market_context_snapshots ORDER BY received_at DESC LIMIT 1"""
        )
        row = cur.fetchone()
    if not row:
        return {"ok": False, "status": "unavailable", "error": "no_snapshot",
                "observations": [], "display_only": True, "decision_eligible": False}

    from core.broad_market_context import build_market_snapshot

    now = int(time.time()) if now_ts is None else int(now_ts)
    max_age_s = max(300, int(os.getenv("BROAD_MARKET_MAX_AGE_S", "1800")))
    observations: List[Dict[str, Any]] = []
    for stored in row[4] or []:
        item = dict(stored)
        source_ts = item.get("source_ts")
        source_age_s = None if source_ts is None else now - int(source_ts)
        item["source_age_s"] = source_age_s
        item["stale"] = (
            source_age_s is None or source_age_s < -60 or source_age_s > max_age_s
        )
        observations.append(item)
    current = build_market_snapshot(
        observations,
        provider_status=row[5] or {},
        received_at=now,
    )
    current.update({
        "snapshot_id": row[0],
        "observed_at": row[1],
        "received_at": row[2],
        "snapshot_age_s": max(0, now - int(row[2])),
    })
    return current
