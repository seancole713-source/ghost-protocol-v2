"""Fail-closed evidence integrity primitives for Ghost scenario monitors.

Narrative text, AI output, and operator submissions are claims, never evidence.
Only records whose complete machine-verifiable evidence chain is marked
CONFIRMED may affect a green/red signal. Conflicting claims are preserved and
reported; they are never averaged or silently replaced.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, Iterable, List, Optional


CONFIRMED = "CONFIRMED"
VERIFIED_CONFLICT = "VERIFIED_CONFLICT"
UNVERIFIED = "UNVERIFIED"
PENDING = "PENDING"
MISSING = "MISSING"
SCENARIO = "SCENARIO"
ALLOWED_STATUSES = {CONFIRMED, VERIFIED_CONFLICT, UNVERIFIED, PENDING, MISSING, SCENARIO}

_REQUIRED_CHAIN = (
    "source",
    "source_timestamp",
    "observation_timestamp",
    "reporting_period",
    "currency",
    "unit",
    "basis",
    "expected_value",
    "actual_value",
    "calculation_methodology",
    "confidence_status",
)


def _value(record: Dict[str, Any]) -> Any:
    return record.get("actual_value", record.get("value"))


def _same_value(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    try:
        a, b = float(left), float(right)
        return math.isfinite(a) and math.isfinite(b) and math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)
    except (TypeError, ValueError):
        return str(left) == str(right)


def chain_gaps(record: Optional[Dict[str, Any]], *, growth: bool = False) -> List[str]:
    """Return missing evidence-chain fields for a quantitative claim."""
    if not isinstance(record, dict):
        return list(_REQUIRED_CHAIN)
    missing = [name for name in _REQUIRED_CHAIN if record.get(name) is None]
    if growth and record.get("comparable_prior_period_value") is None:
        missing.append("comparable_prior_period_value")
    return missing


def integrity_status(record: Optional[Dict[str, Any]], *, growth: bool = False) -> str:
    if not isinstance(record, dict):
        return MISSING
    raw = str(record.get("status") or record.get("confidence_status") or UNVERIFIED).upper()
    status = raw if raw in ALLOWED_STATUSES else UNVERIFIED
    if status == VERIFIED_CONFLICT or record.get("data_conflict"):
        return VERIFIED_CONFLICT
    if status == CONFIRMED and chain_gaps(record, growth=growth):
        return UNVERIFIED
    return status


def is_confirmed(record: Optional[Dict[str, Any]], *, growth: bool = False) -> bool:
    return integrity_status(record, growth=growth) == CONFIRMED


def _same_basis(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    fields = (
        "reporting_period",
        "currency",
        "unit",
        "basis",
        "expected_value",
        "comparable_prior_period_value",
        "calculation_methodology",
    )
    return all(left.get(field) == right.get(field) for field in fields)


def _claim_key(record: Dict[str, Any]) -> str:
    payload = {
        "source": record.get("source"),
        "source_timestamp": record.get("source_timestamp", record.get("as_of_ts")),
        "observation_timestamp": record.get("observation_timestamp"),
        "reporting_period": record.get("reporting_period"),
        "currency": record.get("currency"),
        "actual_value": _value(record),
        "expected_value": record.get("expected_value"),
        "comparable_prior_period_value": record.get("comparable_prior_period_value"),
        "unit": record.get("unit"),
        "basis": record.get("basis"),
        "calculation_methodology": record.get("calculation_methodology"),
        "status": record.get("status"),
        "confidence_status": record.get("confidence_status"),
        "provenance": record.get("provenance"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def claims(record: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(record, dict):
        return []
    nested = record.get("claims")
    if isinstance(nested, list):
        return [dict(item) for item in nested if isinstance(item, dict)]
    return [dict(record)]


def _canonical_claims(left: Dict[str, Any], right: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    ordered = sorted((left, right), key=_claim_key)
    return ordered[0], ordered[1]


def data_conflict(signal_key: str, left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    left, right = _canonical_claims(left, right)
    a, b = _value(left), _value(right)
    difference: Any = None
    try:
        difference = round(float(a) - float(b), 9)
    except (TypeError, ValueError):
        difference = f"{a!s} != {b!s}"
    payload = {
        "record_type": "DATA_CONFLICT",
        "signal_key": signal_key,
        "status": VERIFIED_CONFLICT,
        "source_a": left.get("source"),
        "source_b": right.get("source"),
        "source_a_timestamp": left.get("source_timestamp", left.get("as_of_ts")),
        "source_b_timestamp": right.get("source_timestamp", right.get("as_of_ts")),
        "observation_a_timestamp": left.get("observation_timestamp"),
        "observation_b_timestamp": right.get("observation_timestamp"),
        "value_a": a,
        "value_b": b,
        "currency_a": left.get("currency"),
        "currency_b": right.get("currency"),
        "unit_a": left.get("unit"),
        "unit_b": right.get("unit"),
        "basis_a": left.get("basis"),
        "basis_b": right.get("basis"),
        "reporting_period_a": left.get("reporting_period"),
        "reporting_period_b": right.get("reporting_period"),
        "difference": difference if _same_basis(left, right) else None,
        "possible_explanation": "Different observation times, quote feeds, currency/basis, or reporting methodology.",
        "resolution_status": "UNRESOLVED",
    }
    payload["conflict_id"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:32]
    return payload


def reconcile_signal(signal_key: str, records: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Reconcile claims without averaging, favorable selection, or last-write-wins."""
    unique: Dict[str, Dict[str, Any]] = {}
    for record in records:
        for claim in claims(record):
            unique.setdefault(_claim_key(claim), claim)
    rows = [unique[key] for key in sorted(unique)]
    if not rows:
        return None

    conflicts: List[Dict[str, Any]] = []
    for index, left in enumerate(rows):
        for right in rows[index + 1:]:
            if not _same_value(_value(left), _value(right)) or not _same_basis(left, right):
                conflicts.append(data_conflict(signal_key, left, right))
    if conflicts:
        return {
            "value": None,
            "actual_value": None,
            "status": VERIFIED_CONFLICT,
            "confidence_status": VERIFIED_CONFLICT,
            "claims": rows,
            "data_conflict": conflicts,
            "source": None,
            "source_timestamp": None,
            "observation_timestamp": None,
        }

    confirmed = sorted(
        (row for row in rows if integrity_status(row) == CONFIRMED),
        key=_claim_key,
    )
    selected = dict(confirmed[0] if confirmed else rows[0])
    selected["claims"] = rows
    selected["corroborating_claim_count"] = len(rows)
    selected["status"] = integrity_status(selected)
    selected["confidence_status"] = selected["status"]
    selected["value"] = _value(selected)
    return selected


def merge_evidence_sets(*sets: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Merge evidence maps while preserving every claim and surfacing conflicts."""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for evidence in sets:
        for key, record in (evidence or {}).items():
            if isinstance(record, dict):
                grouped.setdefault(key, []).append(record)
    return {
        key: reconciled
        for key, records in grouped.items()
        if (reconciled := reconcile_signal(key, records)) is not None
    }


def conflict_records(evidence: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for record in (evidence or {}).values():
        raw = record.get("data_conflict") if isinstance(record, dict) else None
        for item in raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else []):
            conflict_id = item.get("conflict_id")
            if conflict_id and conflict_id not in seen:
                seen.add(conflict_id)
                out.append(item)
    return out
