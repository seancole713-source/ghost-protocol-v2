"""Append-only evidence ledger for the YMM earnings bull-case scenario.

The checklist remains a shadow research surface. This module freezes evidence
at preregistered event phases, preserves transient premarket/operator evidence,
and resolves the five-trading-day target outcome without changing trade gates.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from core.bull_run_checklist import (
    OPERATOR_PROVENANCE,
    SCENARIO,
    _OPERATOR_INPUTS,
    build_ymm_12_checklist,
    fetch_auto_evidence,
)
from core.evidence_integrity import (
    conflict_records,
    merge_evidence_sets,
)


LOGGER = logging.getLogger("ghost.bull_run_ledger")
CT = ZoneInfo("America/Chicago")


class BullRunDatabaseError(RuntimeError):
    """Raised when the immutable scenario ledger is unavailable."""


# Windows are deliberately broad enough for the 15-minute scheduler, but each
# phase is inserted once. No past phase is backfilled with later information.
_PHASE_WINDOWS: Tuple[Tuple[str, str, int, int], ...] = (
    ("pre_release_close", "2026-08-18", 15 * 60 + 5, 16 * 60),
    ("post_release", "2026-08-19", 6 * 60 + 5, 6 * 60 + 30),
    ("premarket", "2026-08-19", 6 * 60 + 45, 8 * 60 + 25),
    ("open_15", "2026-08-19", 8 * 60 + 45, 9 * 60 + 5),
    ("open_60", "2026-08-19", 9 * 60 + 30, 9 * 60 + 50),
    ("close", "2026-08-19", 15 * 60 + 5, 16 * 60),
)
SCHEDULED_PHASES = tuple(row[0] for row in _PHASE_WINDOWS)
_STICKY_KEYS = {"premarket_gap_pct"}
_OPERATOR_KEYS = set(_OPERATOR_INPUTS)
_RESOLUTION_NOT_BEFORE_TS = int(
    datetime(2026, 8, 25, 15, 15, tzinfo=CT).timestamp()
)
_RESOLUTION_DATES = (
    "2026-08-19",
    "2026-08-20",
    "2026-08-21",
    "2026-08-24",
    "2026-08-25",
)
_TRAINING_CONFLICT_CAPTURED_AT = int(
    datetime(2026, 8, 19, 8, 13, 5, tzinfo=ZoneInfo("UTC")).timestamp()
)


def _training_quote_conflict() -> Dict[str, Dict[str, Any]]:
    """Return the immutable AI-vs-Ghost quote discrepancy captured on event day."""
    base = {
        "expected_value": 8.80,
        "reporting_period": "2026-08-19 premarket",
        "currency": "USD",
        "unit": "USD per share",
        "basis": "market_quote",
        "status": "UNVERIFIED",
        "confidence_status": "UNVERIFIED",
    }
    external = {
        **base,
        "value": 9.04,
        "actual_value": 9.04,
        "source": "external_ai_conversation_claim",
        "source_timestamp": None,
        "observation_timestamp": None,
        "calculation_methodology": "AI narrative claimed a premarket quote; original provider and observation time unavailable",
        "provenance": "external_ai_claim",
    }
    ghost = {
        **base,
        "value": 8.79,
        "actual_value": 8.79,
        "source": "ghost_production_market_session_alpaca_observation",
        "source_timestamp": None,
        "observation_timestamp": None,
        "calculation_methodology": "Ghost production market-session response observed during comparison; provider timestamp not preserved in the comparison record",
        "provenance": "ghost_comparison_observation",
    }
    return merge_evidence_sets({"premarket_price": external}, {"premarket_price": ghost})


_TRAINING_QUOTE_CONFLICT = _training_quote_conflict()


def _now() -> int:
    return int(time.time())


def _jsonb(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _coerce_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def ensure_bull_run_tables(cur) -> None:
    """Create scenario snapshot and outcome tables. Safe and idempotent."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_bull_run_scenario_snapshots (
            id BIGSERIAL PRIMARY KEY,
            scenario_id VARCHAR(100) NOT NULL,
            scoring_version VARCHAR(100) NOT NULL,
            symbol VARCHAR(20) NOT NULL,
            phase VARCHAR(40) NOT NULL,
            slot_key VARCHAR(180) NOT NULL,
            scheduled_for_ts BIGINT,
            captured_at BIGINT NOT NULL,
            observed_price FLOAT,
            observed_price_ts BIGINT,
            reference_price FLOAT NOT NULL,
            target_price FLOAT NOT NULL,
            horizon_days INT NOT NULL,
            observation_status VARCHAR(40) NOT NULL DEFAULT 'observed',
            data_status VARCHAR(32),
            decision VARCHAR(40),
            confirmed_boxes INT,
            known_boxes INT,
            unknown_boxes INT,
            pending_boxes INT,
            red_boxes INT,
            independent_confirmations INT,
            observed_evidence_json JSONB NOT NULL,
            effective_evidence_json JSONB NOT NULL,
            report_json JSONB NOT NULL,
            source_status_json JSONB,
            created_at BIGINT NOT NULL,
            UNIQUE (scenario_id, scoring_version, slot_key)
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_bull_run_snapshots_symbol_time "
        "ON ghost_bull_run_scenario_snapshots (symbol, captured_at DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_bull_run_snapshots_scenario_phase "
        "ON ghost_bull_run_scenario_snapshots (scenario_id, phase, captured_at DESC)"
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_bull_run_evidence_claims (
            id BIGSERIAL PRIMARY KEY,
            claim_id VARCHAR(64) NOT NULL UNIQUE,
            scenario_id VARCHAR(100) NOT NULL,
            scoring_version VARCHAR(100) NOT NULL,
            symbol VARCHAR(20) NOT NULL,
            signal_key VARCHAR(100) NOT NULL,
            source TEXT,
            source_timestamp BIGINT,
            observation_timestamp BIGINT,
            actual_value_json JSONB,
            status VARCHAR(40) NOT NULL,
            claim_json JSONB NOT NULL,
            captured_at BIGINT NOT NULL,
            created_at BIGINT NOT NULL
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_bull_run_claims_signal_time "
        "ON ghost_bull_run_evidence_claims (scenario_id, scoring_version, signal_key, captured_at ASC)"
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_bull_run_evidence_conflicts (
            id BIGSERIAL PRIMARY KEY,
            conflict_id VARCHAR(64) NOT NULL UNIQUE,
            scenario_id VARCHAR(100) NOT NULL,
            scoring_version VARCHAR(100) NOT NULL,
            symbol VARCHAR(20) NOT NULL,
            signal_key VARCHAR(100) NOT NULL,
            status VARCHAR(40) NOT NULL,
            resolution_status VARCHAR(40) NOT NULL,
            conflict_json JSONB NOT NULL,
            captured_at BIGINT NOT NULL,
            created_at BIGINT NOT NULL
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_bull_run_conflicts_signal_time "
        "ON ghost_bull_run_evidence_conflicts (scenario_id, scoring_version, signal_key, captured_at ASC)"
    )
    _persist_evidence_cur(
        cur,
        _TRAINING_QUOTE_CONFLICT,
        captured_at=_TRAINING_CONFLICT_CAPTURED_AT,
    )
    cur.execute(
        "ALTER TABLE ghost_bull_run_scenario_snapshots "
        "ADD COLUMN IF NOT EXISTS observation_status VARCHAR(40) NOT NULL DEFAULT 'observed'"
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_bull_run_scenario_resolutions (
            id BIGSERIAL PRIMARY KEY,
            scenario_id VARCHAR(100) NOT NULL,
            scoring_version VARCHAR(100) NOT NULL,
            symbol VARCHAR(20) NOT NULL,
            event_date VARCHAR(10) NOT NULL,
            reference_price FLOAT NOT NULL,
            target_price FLOAT NOT NULL,
            horizon_days INT NOT NULL,
            price_1d FLOAT,
            return_1d_pct FLOAT,
            price_horizon FLOAT,
            return_horizon_pct FLOAT,
            hit_target BOOLEAN,
            max_favorable_pct FLOAT,
            max_adverse_pct FLOAT,
            resolved_at BIGINT NOT NULL,
            evidence_available_ts BIGINT NOT NULL,
            reason VARCHAR(160),
            created_at BIGINT NOT NULL,
            UNIQUE (scenario_id, scoring_version)
        )
        """
    )


def phase_for_ts(now_ts: Optional[int] = None) -> Optional[str]:
    """Return the preregistered phase for a CT timestamp, or None."""
    ts = int(now_ts or _now())
    dt = datetime.fromtimestamp(ts, CT)
    date_key = dt.date().isoformat()
    minute = dt.hour * 60 + dt.minute
    for phase, phase_date, start_min, end_min in _PHASE_WINDOWS:
        if date_key == phase_date and start_min <= minute < end_min:
            return phase
    return None


def _scheduled_for_ts(phase: str) -> Optional[int]:
    row = next((item for item in _PHASE_WINDOWS if item[0] == phase), None)
    if row is None:
        return None
    _, date_key, start_min, _ = row
    local = datetime.fromisoformat(date_key).replace(
        hour=start_min // 60,
        minute=start_min % 60,
        tzinfo=CT,
    )
    return int(local.timestamp())


def _operator_slot_key(evidence: Dict[str, Dict[str, Any]]) -> str:
    digest = hashlib.sha256(_jsonb(evidence).encode("utf-8")).hexdigest()[:24]
    return f"operator:{digest}"


def _phase_ready(phase: str, observed: Dict[str, Dict[str, Any]]) -> Tuple[bool, str]:
    if phase == "operator_evidence":
        return (bool(set(observed) & _OPERATOR_KEYS), "operator evidence required")
    if phase == "premarket":
        ready = "premarket_gap_pct" in observed and "live_price" in observed
        return ready, "premarket gap and live price required"
    ready = "live_price" in observed
    return ready, "live point-in-time price required"


def _effective_source_status(
    source_status: Dict[str, Any],
    effective_evidence: Dict[str, Dict[str, Any]],
    *,
    preserved_keys: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Describe effective evidence, including values restored from the ledger."""
    status = dict(source_status)
    operator_keys = sorted(set(effective_evidence) & _OPERATOR_KEYS)
    status["operator_evidence"] = {
        "available": bool(operator_keys),
        "keys": operator_keys,
        "provenance": OPERATOR_PROVENANCE if operator_keys else None,
    }
    earnings_keys = sorted(
        set(operator_keys) & {"revenue_actual_usd_m", "eps_adjusted_ads_usd"}
    )
    if earnings_keys:
        status["earnings"] = {
            "available": True,
            "keys": earnings_keys,
            "provenance": OPERATOR_PROVENANCE,
        }
    if preserved_keys is not None:
        status["ledger"] = {
            "preserved_keys": sorted(preserved_keys),
            "available": bool(preserved_keys),
        }
    return status


def _claim_id(signal_key: str, claim: Dict[str, Any]) -> str:
    return hashlib.sha256(
        f"{SCENARIO['scenario_id']}|{SCENARIO['scoring_version']}|{signal_key}|{_jsonb(claim)}".encode()
    ).hexdigest()


def _persist_evidence_cur(
    cur,
    evidence: Dict[str, Dict[str, Any]],
    *,
    captured_at: int,
) -> None:
    for signal_key, record in (evidence or {}).items():
        if not isinstance(record, dict):
            continue
        nested = record.get("claims")
        rows = nested if isinstance(nested, list) else [record]
        for claim in rows:
            if not isinstance(claim, dict):
                continue
            claim_id = _claim_id(signal_key, claim)
            cur.execute(
                """
                INSERT INTO ghost_bull_run_evidence_claims (
                    claim_id, scenario_id, scoring_version, symbol, signal_key,
                    source, source_timestamp, observation_timestamp,
                    actual_value_json, status, claim_json, captured_at, created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s)
                ON CONFLICT (claim_id) DO NOTHING
                """,
                (
                    claim_id, SCENARIO["scenario_id"], SCENARIO["scoring_version"],
                    SCENARIO["symbol"], signal_key, claim.get("source"),
                    claim.get("source_timestamp", claim.get("as_of_ts")),
                    claim.get("observation_timestamp"),
                    _jsonb(claim.get("actual_value", claim.get("value"))),
                    str(claim.get("status") or claim.get("confidence_status") or "UNVERIFIED"),
                    _jsonb(claim), captured_at, _now(),
                ),
            )
    for conflict in conflict_records(evidence):
        cur.execute(
            """
            INSERT INTO ghost_bull_run_evidence_conflicts (
                conflict_id, scenario_id, scoring_version, symbol, signal_key,
                status, resolution_status, conflict_json, captured_at, created_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (conflict_id) DO NOTHING
            """,
            (
                conflict["conflict_id"], SCENARIO["scenario_id"], SCENARIO["scoring_version"],
                SCENARIO["symbol"], conflict["signal_key"], conflict["status"],
                conflict["resolution_status"], _jsonb(conflict), captured_at, _now(),
            ),
        )


def _load_preserved_evidence_cur(cur) -> Dict[str, Dict[str, Any]]:
    cur.execute(
        """
        SELECT signal_key, claim_json
        FROM ghost_bull_run_evidence_claims
        WHERE scenario_id = %s AND scoring_version = %s
        ORDER BY captured_at ASC, id ASC
        """,
        (SCENARIO["scenario_id"], SCENARIO["scoring_version"]),
    )
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    claim_rows = cur.fetchall() or []
    for signal_key, raw_claim in claim_rows:
        if signal_key not in _STICKY_KEYS and signal_key not in _OPERATOR_KEYS:
            continue
        claim = _coerce_json(raw_claim)
        if isinstance(claim, dict):
            grouped.setdefault(signal_key, []).append(claim)
    if not claim_rows:
        # Compatibility for deployments created before the append-only claims table.
        # Historical snapshot claims remain non-confirming unless their own stored
        # evidence chain satisfies the current integrity contract.
        cur.execute(
            """
            SELECT observed_evidence_json
            FROM ghost_bull_run_scenario_snapshots
            WHERE scenario_id = %s AND scoring_version = %s
            ORDER BY captured_at ASC, id ASC
            """,
            (SCENARIO["scenario_id"], SCENARIO["scoring_version"]),
        )
        for row in cur.fetchall() or []:
            evidence = _coerce_json(row[0]) or {}
            if not isinstance(evidence, dict):
                continue
            for signal_key, claim in evidence.items():
                if (
                    signal_key in _STICKY_KEYS or signal_key in _OPERATOR_KEYS
                ) and isinstance(claim, dict):
                    grouped.setdefault(signal_key, []).append(claim)
    return merge_evidence_sets(*({key: claim} for key, rows in grouped.items() for claim in rows))


def load_preserved_evidence() -> Dict[str, Dict[str, Any]]:
    """Load sticky premarket and validated operator evidence from prior rows."""
    try:
        from core.db import db_conn

        with db_conn() as conn:
            cur = conn.cursor()
            return _load_preserved_evidence_cur(cur)
    except Exception as exc:
        LOGGER.exception("load bull-run preserved evidence")
        raise BullRunDatabaseError("database_unavailable") from exc


def _insert_snapshot_cur(
    cur,
    *,
    phase: str,
    slot_key: str,
    scheduled_for_ts: Optional[int],
    captured_at: int,
    observation_status: str,
    observed_evidence: Dict[str, Dict[str, Any]],
    effective_evidence: Dict[str, Dict[str, Any]],
    report: Dict[str, Any],
    source_status: Dict[str, Any],
) -> Optional[int]:
    live = observed_evidence.get("live_price") or {}
    cur.execute(
        """
        INSERT INTO ghost_bull_run_scenario_snapshots (
            scenario_id, scoring_version, symbol, phase, slot_key,
            scheduled_for_ts, captured_at, observed_price, observed_price_ts,
            reference_price, target_price, horizon_days, observation_status,
            data_status, decision, confirmed_boxes, known_boxes, unknown_boxes,
            pending_boxes, red_boxes, independent_confirmations,
            observed_evidence_json, effective_evidence_json, report_json,
            source_status_json, created_at
        ) VALUES (
            %s,%s,%s,%s,%s,
            %s,%s,%s,%s,
            %s,%s,%s,%s,
            %s,%s,%s,%s,%s,
            %s,%s,%s,
            %s::jsonb,%s::jsonb,%s::jsonb,
            %s::jsonb,%s
        )
        ON CONFLICT (scenario_id, scoring_version, slot_key) DO NOTHING
        RETURNING id
        """,
        (
            SCENARIO["scenario_id"], SCENARIO["scoring_version"], SCENARIO["symbol"],
            phase, slot_key, scheduled_for_ts, captured_at, live.get("value"),
            live.get("as_of_ts"), SCENARIO["reference_price"], SCENARIO["target_price"],
            SCENARIO["target_horizon_trading_days"], observation_status,
            report.get("data_status"),
            report.get("decision"), report.get("confirmed"), report.get("known"),
            report.get("unknown"), report.get("pending"), report.get("red"),
            report.get("independent_confirmations"), _jsonb(observed_evidence),
            _jsonb(effective_evidence), _jsonb(report), _jsonb(source_status), _now(),
        ),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def capture_snapshot(
    *,
    phase: Optional[str] = None,
    operator_evidence: Optional[Dict[str, Dict[str, Any]]] = None,
    now_ts: Optional[int] = None,
    force: bool = False,
    observation_status: Optional[str] = None,
    fetch_evidence: bool = True,
    cur=None,
) -> Dict[str, Any]:
    """Capture one immutable phase or operator evidence snapshot."""
    captured_at = int(now_ts or _now())
    selected_phase = "operator_evidence" if operator_evidence else (phase or phase_for_ts(captured_at))
    if selected_phase is None:
        return {"ok": True, "inserted": False, "phase": None, "note": "outside snapshot windows"}
    if selected_phase not in SCHEDULED_PHASES and selected_phase != "operator_evidence":
        return {"ok": False, "inserted": False, "error": "invalid phase"}
    if not force and selected_phase in SCHEDULED_PHASES and phase_for_ts(captured_at) != selected_phase:
        return {"ok": True, "inserted": False, "phase": selected_phase, "note": "phase is not currently due"}

    auto_evidence: Dict[str, Dict[str, Any]] = {}
    source_status: Dict[str, Any] = {}
    if fetch_evidence:
        auto_evidence, source_status = fetch_auto_evidence(SCENARIO["symbol"])
    observed = merge_evidence_sets(auto_evidence, operator_evidence or {})
    ready, requirement = _phase_ready(selected_phase, observed)
    status = observation_status or ("observed" if ready else "data_unavailable")
    if status == "missed_no_observation":
        observed = {}
        source_status = {"ledger": {"reason": "scheduled window elapsed without observation"}}
    elif status not in {"observed", "data_unavailable"}:
        return {"ok": False, "inserted": False, "error": "invalid observation status"}

    slot_key = (
        _operator_slot_key(operator_evidence or {})
        if selected_phase == "operator_evidence"
        else f"scheduled:{selected_phase}"
    )

    def _write(c) -> Dict[str, Any]:
        preserved = _load_preserved_evidence_cur(c)
        effective = merge_evidence_sets(preserved, observed)
        _persist_evidence_cur(c, observed, captured_at=captured_at)
        _persist_evidence_cur(c, effective, captured_at=captured_at)
        effective_sources = _effective_source_status(
            source_status,
            effective,
            preserved_keys=list(preserved),
        )
        report = build_ymm_12_checklist(evidence=effective, source_status=effective_sources)
        snapshot_id = _insert_snapshot_cur(
            c,
            phase=selected_phase,
            slot_key=slot_key,
            scheduled_for_ts=_scheduled_for_ts(selected_phase),
            captured_at=captured_at,
            observation_status=status,
            observed_evidence=observed,
            effective_evidence=effective,
            report=report,
            source_status=effective_sources,
        )
        return {
            "ok": True,
            "inserted": snapshot_id is not None,
            "duplicate": snapshot_id is None,
            "snapshot_id": snapshot_id,
            "phase": selected_phase,
            "observation_status": status,
            "note": None if ready else requirement,
            "slot_key": slot_key,
            "report": report,
        }

    try:
        if cur is not None:
            return _write(cur)
        from core.db import db_conn

        with db_conn() as conn:
            result = _write(conn.cursor())
            conn.commit()
            return result
    except Exception as exc:
        LOGGER.exception("capture bull-run snapshot %s", selected_phase)
        raise BullRunDatabaseError("database_unavailable") from exc


def run_snapshot_job(*, now_ts: Optional[int] = None) -> Dict[str, Any]:
    """Capture the due phase or durably mark every elapsed unrecorded slot."""
    now = int(now_ts or _now())
    current = phase_for_ts(now)
    if current is not None:
        return capture_snapshot(now_ts=now)

    results = []
    for phase in SCHEDULED_PHASES:
        scheduled = _scheduled_for_ts(phase)
        if scheduled is None or now <= scheduled:
            continue
        results.append(capture_snapshot(
            phase=phase,
            now_ts=now,
            force=True,
            observation_status="missed_no_observation",
            fetch_evidence=False,
        ))
    return {
        "ok": True,
        "inserted": any(item.get("inserted") for item in results),
        "phase": None,
        "missed": results,
        "note": "outside snapshot windows",
    }


def current_scenario_report(
    symbol: str = "YMM",
    *,
    operator_evidence: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Current report enriched with preserved premarket/operator evidence."""
    if str(symbol or "").strip().upper() != SCENARIO["symbol"]:
        from core.bull_run_checklist import UnsupportedScenarioError

        raise UnsupportedScenarioError(f"No registered bull-run scenario for {symbol}")
    auto, sources = fetch_auto_evidence(SCENARIO["symbol"])
    preserved = load_preserved_evidence()
    effective = merge_evidence_sets(preserved, auto, operator_evidence or {})
    sources = _effective_source_status(sources, effective, preserved_keys=list(preserved))
    return build_ymm_12_checklist(evidence=effective, source_status=sources)


def recent_snapshots(*, limit: int = 50) -> Dict[str, Any]:
    """Read the immutable scenario timeline, newest first."""
    lim = max(1, min(200, int(limit)))
    try:
        from core.db import db_conn

        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, scenario_id, scoring_version, symbol, phase, slot_key,
                       scheduled_for_ts, captured_at, observed_price, observed_price_ts,
                       observation_status, data_status, decision, confirmed_boxes,
                       known_boxes, unknown_boxes, pending_boxes, red_boxes,
                       independent_confirmations,
                       observed_evidence_json, effective_evidence_json, report_json,
                       source_status_json
                FROM ghost_bull_run_scenario_snapshots
                WHERE scenario_id = %s AND scoring_version = %s
                ORDER BY captured_at DESC, id DESC LIMIT %s
                """,
                (SCENARIO["scenario_id"], SCENARIO["scoring_version"], lim),
            )
            rows = cur.fetchall() or []
            cur.execute(
                """
                SELECT price_1d, return_1d_pct, price_horizon, return_horizon_pct,
                       hit_target, max_favorable_pct, max_adverse_pct,
                       resolved_at, evidence_available_ts, reason
                FROM ghost_bull_run_scenario_resolutions
                WHERE scenario_id = %s AND scoring_version = %s
                """,
                (SCENARIO["scenario_id"], SCENARIO["scoring_version"]),
            )
            resolution_row = cur.fetchone()
            cur.execute(
                """
                SELECT claim_id, signal_key, source, source_timestamp,
                       observation_timestamp, status, claim_json, captured_at
                FROM ghost_bull_run_evidence_claims
                WHERE scenario_id = %s AND scoring_version = %s
                ORDER BY captured_at ASC, id ASC
                """,
                (SCENARIO["scenario_id"], SCENARIO["scoring_version"]),
            )
            claim_rows = cur.fetchall() or []
            cur.execute(
                """
                SELECT conflict_id, signal_key, status, resolution_status,
                       conflict_json, captured_at
                FROM ghost_bull_run_evidence_conflicts
                WHERE scenario_id = %s AND scoring_version = %s
                ORDER BY captured_at ASC, id ASC
                """,
                (SCENARIO["scenario_id"], SCENARIO["scoring_version"]),
            )
            conflict_rows = cur.fetchall() or []
        keys = (
            "id", "scenario_id", "scoring_version", "symbol", "phase", "slot_key",
            "scheduled_for_ts", "captured_at", "observed_price", "observed_price_ts",
            "observation_status", "data_status", "decision", "confirmed", "known",
            "unknown", "pending", "red",
            "independent_confirmations", "observed_evidence", "effective_evidence", "report",
            "source_status",
        )
        payload = []
        for row in rows:
            item = dict(zip(keys, row))
            for key in ("observed_evidence", "effective_evidence", "report", "source_status"):
                item[key] = _coerce_json(item.get(key))
            payload.append(item)
        resolution = None
        if resolution_row:
            resolution = dict(zip(
                (
                    "price_1d", "return_1d_pct", "price_horizon", "return_horizon_pct",
                    "hit_target", "max_favorable_pct", "max_adverse_pct", "resolved_at",
                    "evidence_available_ts", "reason",
                ),
                resolution_row,
            ))
        claims = []
        for row in claim_rows:
            item = dict(zip(
                ("claim_id", "signal_key", "source", "source_timestamp", "observation_timestamp", "status", "claim", "captured_at"),
                row,
            ))
            item["claim"] = _coerce_json(item["claim"])
            claims.append(item)
        conflicts = []
        for row in conflict_rows:
            item = dict(zip(
                ("conflict_id", "signal_key", "status", "resolution_status", "conflict", "captured_at"),
                row,
            ))
            item["conflict"] = _coerce_json(item["conflict"])
            conflicts.append(item)
        return {
            "ok": True,
            "scenario": SCENARIO,
            "rows": payload,
            "resolution": resolution,
            "evidence_claims": claims,
            "data_conflicts": conflicts,
        }
    except Exception as exc:
        LOGGER.exception("recent bull-run snapshots")
        raise BullRunDatabaseError("database_unavailable") from exc


def _bar_date(bar: Dict[str, Any]) -> Optional[str]:
    raw = bar.get("ts")
    if raw is None:
        return None
    text = str(raw).strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    try:
        return datetime.fromtimestamp(int(float(raw)), CT).date().isoformat()
    except Exception:
        return None


def _resolve_from_series(series: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Pure outcome calculation over the five exact preregistered sessions."""
    by_date: Dict[str, Dict[str, Any]] = {}
    for bar in series:
        date_key = _bar_date(bar)
        if date_key in _RESOLUTION_DATES:
            by_date[date_key] = bar
    if any(date_key not in by_date for date_key in _RESOLUTION_DATES):
        return None
    window = [by_date[date_key] for date_key in _RESOLUTION_DATES]
    ref = float(SCENARIO["reference_price"])
    target = float(SCENARIO["target_price"])
    first_close = float(window[0]["close"])
    horizon_close = float(window[-1]["close"])
    highs = [float(bar["high"]) for bar in window if bar.get("high") is not None]
    lows = [float(bar["low"]) for bar in window if bar.get("low") is not None]
    return {
        "price_1d": first_close,
        "return_1d_pct": round((first_close / ref - 1.0) * 100.0, 3),
        "price_horizon": horizon_close,
        "return_horizon_pct": round((horizon_close / ref - 1.0) * 100.0, 3),
        "hit_target": bool(highs and max(highs) >= target),
        "max_favorable_pct": round((max(highs) / ref - 1.0) * 100.0, 3) if highs else None,
        "max_adverse_pct": round((min(lows) / ref - 1.0) * 100.0, 3) if lows else None,
    }


def _resolution_exists() -> bool:
    """Avoid fetching the same one-off outcome on every hourly scheduler run."""
    try:
        from core.db import db_conn

        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT 1 FROM ghost_bull_run_scenario_resolutions
                WHERE scenario_id = %s AND scoring_version = %s
                LIMIT 1
                """,
                (SCENARIO["scenario_id"], SCENARIO["scoring_version"]),
            )
            return cur.fetchone() is not None
    except Exception as exc:
        LOGGER.exception("check bull-run resolution")
        raise BullRunDatabaseError("database_unavailable") from exc


def resolve_scenario(
    *,
    series: Optional[List[Dict[str, Any]]] = None,
    now_ts: Optional[int] = None,
    cur=None,
) -> Dict[str, Any]:
    """Resolve the scenario once five complete event-day-forward bars exist."""
    now = int(now_ts or _now())
    if series is None and now < _RESOLUTION_NOT_BEFORE_TS:
        return {
            "ok": True,
            "resolved": False,
            "note": "five-session resolution window is still open",
        }
    if series is None and cur is None and _resolution_exists():
        return {
            "ok": True,
            "resolved": False,
            "duplicate": True,
            "note": "scenario outcome already resolved",
        }
    if series is None:
        try:
            from core.squeeze_hunter_ledger import _ohlc_series

            series = _ohlc_series(SCENARIO["symbol"], period="3mo")
        except Exception:
            series = []
    outcome = _resolve_from_series(series or [])
    if outcome is None:
        return {"ok": True, "resolved": False, "note": "five completed trading sessions not available"}

    def _write(c) -> Dict[str, Any]:
        c.execute(
            """
            INSERT INTO ghost_bull_run_scenario_resolutions (
                scenario_id, scoring_version, symbol, event_date,
                reference_price, target_price, horizon_days,
                price_1d, return_1d_pct, price_horizon, return_horizon_pct,
                hit_target, max_favorable_pct, max_adverse_pct,
                resolved_at, evidence_available_ts, reason, created_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (scenario_id, scoring_version) DO NOTHING
            RETURNING id
            """,
            (
                SCENARIO["scenario_id"], SCENARIO["scoring_version"], SCENARIO["symbol"],
                SCENARIO["event_date"], SCENARIO["reference_price"], SCENARIO["target_price"],
                SCENARIO["target_horizon_trading_days"], outcome["price_1d"],
                outcome["return_1d_pct"], outcome["price_horizon"],
                outcome["return_horizon_pct"], outcome["hit_target"],
                outcome["max_favorable_pct"], outcome["max_adverse_pct"],
                now, now, "five_trading_day_window_complete", _now(),
            ),
        )
        row = c.fetchone()
        return {
            "ok": True,
            "resolved": row is not None,
            "duplicate": row is None,
            "resolution_id": int(row[0]) if row else None,
            "outcome": outcome,
        }

    try:
        if cur is not None:
            return _write(cur)
        from core.db import db_conn

        with db_conn() as conn:
            result = _write(conn.cursor())
            conn.commit()
            return result
    except Exception as exc:
        LOGGER.exception("resolve bull-run scenario")
        raise BullRunDatabaseError("database_unavailable") from exc


__all__ = [
    "BullRunDatabaseError",
    "SCHEDULED_PHASES",
    "capture_snapshot",
    "current_scenario_report",
    "ensure_bull_run_tables",
    "load_preserved_evidence",
    "phase_for_ts",
    "recent_snapshots",
    "resolve_scenario",
    "run_snapshot_job",
]
