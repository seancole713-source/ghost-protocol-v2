"""core/research_point_in_time.py — point-in-time dataset validation (Phase 2).

Every research dataset sample must prove its features were knowable at the
prediction timestamp. This module provides frozen observation types, strict
timestamp enforcement, source-specific availability adapters, and immutable
dataset manifests.

Key rules:
  - Every source carries both event_ts (when the data happened) and
    available_ts (when it became knowable).
  - 0 < event_ts <= available_ts <= prediction_ts
  - Source-specific staleness limits are enforced.
  - Missing availability metadata fails closed (DATA_INVALID), never neutral.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

LOGGER = logging.getLogger("ghost.research_point_in_time")

# ── frozen observation types ────────────────────────────────────────────────

@dataclass(frozen=True)
class SourceObservation:
    """One point-in-time observation from a single data source."""
    source_id: str          # e.g. "daily_ohlcv", "sec_fundamentals"
    event_ts: int           # when the data event occurred (Unix seconds)
    available_ts: int       # when the data became knowable (Unix seconds)
    values: Dict[str, float]  # feature values from this source
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.source_id:
            raise ValueError("source_id is required")
        if self.event_ts <= 0:
            raise ValueError(f"event_ts must be > 0, got {self.event_ts}")
        if self.available_ts <= 0:
            raise ValueError(f"available_ts must be > 0, got {self.available_ts}")
        if self.available_ts < self.event_ts:
            raise ValueError(
                f"available_ts ({self.available_ts}) < event_ts ({self.event_ts})"
            )
        for k, v in self.values.items():
            if not math.isfinite(v):
                raise ValueError(f"SourceObservation value '{k}' is non-finite: {v}")


@dataclass(frozen=True)
class DatasetSample:
    """One labelled research sample with point-in-time features."""
    symbol: str
    prediction_ts: int      # when the prediction would be issued
    features: Dict[str, float]  # merged feature vector
    feature_asof_ts: int    # latest source available_ts (must be <= prediction_ts)
    label: int              # 1 = WIN, 0 = non-WIN (LOSS/EXPIRED)
    outcome: str            # WIN, LOSS, EXPIRED, DATA_INVALID
    label_ts: int            # when the outcome became knowable
    contract_id: str         # which contract this sample belongs to
    sources: Tuple[SourceObservation, ...]
    sample_id: str = ""      # SHA-256 of canonical payload

    def __post_init__(self):
        if self.prediction_ts <= 0:
            raise ValueError(f"prediction_ts must be > 0, got {self.prediction_ts}")
        if self.feature_asof_ts > self.prediction_ts:
            raise ValueError(
                f"feature_asof_ts ({self.feature_asof_ts}) > prediction_ts ({self.prediction_ts})"
            )
        if self.label_ts <= self.prediction_ts:
            raise ValueError(
                f"label_ts ({self.label_ts}) must be > prediction_ts ({self.prediction_ts})"
            )
        if self.outcome not in ("WIN", "LOSS", "EXPIRED", "DATA_INVALID"):
            raise ValueError(f"Invalid outcome: {self.outcome}")
        if self.label not in (0, 1):
            raise ValueError(f"label must be 0 or 1, got {self.label}")
        if not self.contract_id:
            raise ValueError("contract_id is required")
        if not self.symbol:
            raise ValueError("symbol is required")

    def compute_sample_id(self) -> str:
        """SHA-256 of the canonical payload."""
        payload = {
            "symbol": self.symbol,
            "prediction_ts": self.prediction_ts,
            "feature_asof_ts": self.feature_asof_ts,
            "label": self.label,
            "outcome": self.outcome,
            "label_ts": self.label_ts,
            "contract_id": self.contract_id,
            "features": dict(sorted(self.features.items())),
            "sources": sorted(
                (
                    {
                        "source_id": s.source_id,
                        "event_ts": s.event_ts,
                        "available_ts": s.available_ts,
                    }
                    for s in self.sources
                ),
                key=lambda d: d["source_id"],
            ),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True)
class DatasetManifest:
    """Immutable record of a dataset build."""
    manifest_sha: str       # SHA-256 of the canonical payload
    contract_id: str
    symbols: Tuple[str, ...]
    date_range_start: str   # YYYY-MM-DD
    date_range_end: str     # YYYY-MM-DD
    source_watermark_ts: int  # latest available_ts across all sources
    feature_schema: str
    feature_order: Tuple[str, ...]
    sample_count: int
    accepted_count: int
    rejected_count: int
    rejection_reasons: Dict[str, int]  # reason → count
    code_revision: str = ""
    created_at: int = field(default_factory=lambda: int(time.time()))

    def __post_init__(self):
        if not self.manifest_sha or len(self.manifest_sha) != 64:
            raise ValueError("manifest_sha must be a 64-char hex string")
        if self.accepted_count + self.rejected_count != self.sample_count:
            raise ValueError(
                f"accepted ({self.accepted_count}) + rejected ({self.rejected_count}) "
                f"!= sample_count ({self.sample_count})"
            )


# ── timestamp validation ────────────────────────────────────────────────────

def validate_source_timestamp(
    event_ts: Any,
    available_ts: Any,
    *,
    source_id: str = "",
    max_staleness_s: int = 86400,
    prediction_ts: Optional[int] = None,
) -> Tuple[Optional[int], Optional[int], Optional[str]]:
    """Validate and return (event_ts, available_ts, error).

    Returns (None, None, error_message) on any failure.
    """
    from core.feature_schema import feature_asof_unix

    # Parse both timestamps
    parsed_event = feature_asof_unix(event_ts, default_now=False)
    parsed_available = feature_asof_unix(available_ts, default_now=False)

    if parsed_event == 0:
        return None, None, f"{source_id}: invalid event_ts"
    if parsed_available == 0:
        return None, None, f"{source_id}: invalid available_ts"

    # Chronology: event must happen before or at availability
    if parsed_event > parsed_available:
        return None, None, (
            f"{source_id}: event_ts ({parsed_event}) > available_ts ({parsed_available})"
        )

    # Availability must be before prediction
    if prediction_ts is not None and parsed_available > prediction_ts:
        return None, None, (
            f"{source_id}: available_ts ({parsed_available}) > prediction_ts ({prediction_ts})"
        )

    # Staleness check
    if prediction_ts is not None:
        age = prediction_ts - parsed_available
        if age > max_staleness_s:
            return None, None, (
                f"{source_id}: data is {age}s old, max staleness is {max_staleness_s}s"
            )

    return parsed_event, parsed_available, None


# ── source-specific adapters ────────────────────────────────────────────────

def daily_ohlcv_observation(
    bar: Dict[str, Any],
    *,
    source_id: str = "daily_ohlcv",
    prediction_ts: Optional[int] = None,
) -> Optional[SourceObservation]:
    """Create a SourceObservation from a daily OHLCV bar.

    The bar's timestamp is the event_ts. available_ts is the conservative
    daily-bar-available timestamp (21:00 UTC).
    """
    from core.feature_schema import feature_asof_unix
    from core.tp_sl_resolve import _daily_bar_available_ts

    bar_ts = bar.get("ts")
    event_ts = feature_asof_unix(bar_ts, default_now=False)
    if event_ts == 0:
        return None
    available_ts = _daily_bar_available_ts(bar_ts)
    if available_ts == 0:
        return None

    if prediction_ts is not None and available_ts > prediction_ts:
        return None

    # Extract OHLCV values
    values: Dict[str, float] = {}
    for key in ("open", "high", "low", "close", "volume"):
        try:
            v = float(bar.get(key, 0))
            if math.isfinite(v):
                values[key] = v
        except (TypeError, ValueError):
            pass

    return SourceObservation(
        source_id=source_id,
        event_ts=event_ts,
        available_ts=available_ts,
        values=values,
        metadata={"bar_ts_raw": str(bar_ts)},
    )


def news_event_observation(
    event: Dict[str, Any],
    *,
    source_id: str = "news_events",
    prediction_ts: Optional[int] = None,
) -> Optional[SourceObservation]:
    """Create a direct-event SourceObservation from a news event row.

    Peer-derived rows are advisory context and cannot enter target-symbol
    decision datasets under the ``news_events`` source identity.
    """
    if event.get("derived") is True or event.get("decision_eligible") is False:
        return None
    asof_ts = event.get("asof_ts")
    ingested_at = event.get("ingested_at") or event.get("extracted_at") or asof_ts

    event_ts, available_ts, err = validate_source_timestamp(
        asof_ts, ingested_at,
        source_id=source_id,
        prediction_ts=prediction_ts,
    )
    if err:
        return None

    return SourceObservation(
        source_id=source_id,
        event_ts=event_ts,  # type: ignore[arg-type]
        available_ts=available_ts,  # type: ignore[arg-type]
        values={
            "materiality": float(event.get("materiality", 0)),
            "confidence": float(event.get("confidence", 0)),
        },
        metadata={
            "event_type": event.get("event_type", ""),
            "direction_hint": event.get("direction_hint", ""),
            "derived": False,
            "origin_symbol": event.get("origin_symbol"),
            "provenance_policy": "direct_only_v1",
        },
    )


# ── dataset building ────────────────────────────────────────────────────────

def build_dataset_manifest(
    contract_id: str,
    symbols: Sequence[str],
    date_range_start: str,
    date_range_end: str,
    feature_schema: str,
    feature_order: Sequence[str],
    samples: Sequence[DatasetSample],
    rejected: Dict[str, int],
    *,
    source_watermark_ts: int = 0,
    code_revision: str = "",
) -> DatasetManifest:
    """Build an immutable manifest for a completed dataset."""
    accepted = [s for s in samples if s.outcome != "DATA_INVALID"]
    total = len(samples)

    # Build canonical payload for hashing
    payload = {
        "contract_id": contract_id,
        "symbols": sorted(set(symbols)),
        "date_range_start": date_range_start,
        "date_range_end": date_range_end,
        "source_watermark_ts": source_watermark_ts,
        "feature_schema": feature_schema,
        "feature_order": sorted(set(feature_order)),
        "sample_count": total,
        "accepted_count": len(accepted),
        "rejected_count": total - len(accepted),
        "rejection_reasons": dict(sorted(rejected.items())),
        "code_revision": code_revision,
        "created_at": int(time.time()),
    }
    manifest_sha = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    return DatasetManifest(
        manifest_sha=manifest_sha,
        contract_id=contract_id,
        symbols=tuple(sorted(set(symbols))),
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        source_watermark_ts=source_watermark_ts,
        feature_schema=feature_schema,
        feature_order=tuple(sorted(set(feature_order))),
        sample_count=total,
        accepted_count=len(accepted),
        rejected_count=total - len(accepted),
        rejection_reasons=dict(sorted(rejected.items())),
        code_revision=code_revision,
    )


# ── persistence ─────────────────────────────────────────────────────────────

def ensure_research_dataset_tables(cur) -> None:
    """Create research dataset tables if they don't exist."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_research_dataset_manifests (
            manifest_sha TEXT PRIMARY KEY,
            contract_id TEXT NOT NULL,
            symbols TEXT NOT NULL,
            date_range_start TEXT NOT NULL,
            date_range_end TEXT NOT NULL,
            source_watermark_ts BIGINT NOT NULL,
            feature_schema TEXT NOT NULL,
            feature_order TEXT NOT NULL,
            sample_count INT NOT NULL,
            accepted_count INT NOT NULL,
            rejected_count INT NOT NULL,
            rejection_reasons JSONB,
            code_revision TEXT DEFAULT '',
            created_at BIGINT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_research_dataset_samples (
            sample_id TEXT PRIMARY KEY,
            manifest_sha TEXT NOT NULL REFERENCES ghost_research_dataset_manifests(manifest_sha),
            contract_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            prediction_ts BIGINT NOT NULL,
            feature_asof_ts BIGINT NOT NULL,
            label INT NOT NULL,
            outcome TEXT NOT NULL,
            label_ts BIGINT NOT NULL,
            features JSONB NOT NULL,
            sources JSONB,
            created_at BIGINT NOT NULL
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_samples_manifest "
        "ON ghost_research_dataset_samples (manifest_sha)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_samples_symbol_time "
        "ON ghost_research_dataset_samples (symbol, prediction_ts)"
    )


def persist_dataset_manifest(manifest: DatasetManifest, cur=None) -> bool:
    """Insert a dataset manifest. Idempotent by manifest_sha."""
    if cur is not None:
        return _persist_manifest_impl(cur, manifest)
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        ensure_research_dataset_tables(c)
        result = _persist_manifest_impl(c, manifest)
        conn.commit()
        return result


def _persist_manifest_impl(cur, manifest: DatasetManifest) -> bool:
    cur.execute(
        """
        INSERT INTO ghost_research_dataset_manifests
            (manifest_sha, contract_id, symbols, date_range_start, date_range_end,
             source_watermark_ts, feature_schema, feature_order, sample_count,
             accepted_count, rejected_count, rejection_reasons, code_revision, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (manifest_sha) DO NOTHING
        """,
        (
            manifest.manifest_sha,
            manifest.contract_id,
            json.dumps(list(manifest.symbols)),
            manifest.date_range_start,
            manifest.date_range_end,
            manifest.source_watermark_ts,
            manifest.feature_schema,
            json.dumps(list(manifest.feature_order)),
            manifest.sample_count,
            manifest.accepted_count,
            manifest.rejected_count,
            json.dumps(manifest.rejection_reasons),
            manifest.code_revision,
            manifest.created_at,
        ),
    )
    return cur.rowcount > 0


def persist_dataset_sample(sample: DatasetSample, manifest_sha: str, cur=None) -> bool:
    """Insert a dataset sample. Idempotent by sample_id."""
    sid = sample.compute_sample_id()
    if cur is not None:
        return _persist_sample_impl(cur, sample, sid, manifest_sha)
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        ensure_research_dataset_tables(c)
        result = _persist_sample_impl(c, sample, sid, manifest_sha)
        conn.commit()
        return result


def _persist_sample_impl(cur, sample: DatasetSample, sample_id: str, manifest_sha: str) -> bool:
    cur.execute(
        """
        INSERT INTO ghost_research_dataset_samples
            (sample_id, manifest_sha, contract_id, symbol, prediction_ts,
             feature_asof_ts, label, outcome, label_ts, features, sources, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (sample_id) DO NOTHING
        """,
        (
            sample_id,
            manifest_sha,
            sample.contract_id,
            sample.symbol,
            sample.prediction_ts,
            sample.feature_asof_ts,
            sample.label,
            sample.outcome,
            sample.label_ts,
            json.dumps(sample.features),
            json.dumps(
                [
                    {
                        "source_id": s.source_id,
                        "event_ts": s.event_ts,
                        "available_ts": s.available_ts,
                    }
                    for s in sample.sources
                ]
            ),
            int(time.time()),
        ),
    )
    return cur.rowcount > 0
