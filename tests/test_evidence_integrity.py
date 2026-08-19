"""Unit tests for fail-closed evidence integrity reconciliation."""
from __future__ import annotations

from core.evidence_integrity import (
    CONFIRMED,
    UNVERIFIED,
    VERIFIED_CONFLICT,
    chain_gaps,
    integrity_status,
    merge_evidence_sets,
)


def _claim(value, *, source="provider-a", observed=100, status=CONFIRMED):
    return {
        "value": value,
        "actual_value": value,
        "expected_value": 0.0,
        "source": source,
        "source_timestamp": observed,
        "observation_timestamp": observed,
        "reporting_period": "2026-Q2",
        "currency": "USD",
        "unit": "USD per share",
        "basis": "market_quote",
        "calculation_methodology": "latest synchronized provider trade",
        "status": status,
        "confidence_status": status,
    }


def test_complete_confirmed_chain_can_confirm():
    claim = _claim(8.79)
    assert chain_gaps(claim) == []
    assert integrity_status(claim) == CONFIRMED


def test_incomplete_confirmed_chain_downgrades_to_unverified():
    claim = _claim(8.79)
    claim.pop("source_timestamp")
    assert "source_timestamp" in chain_gaps(claim)
    assert integrity_status(claim) == UNVERIFIED


def test_exact_claim_is_deduplicated():
    claim = _claim(8.79)
    merged = merge_evidence_sets({"live_price": claim}, {"live_price": dict(claim)})
    assert merged["live_price"]["status"] == CONFIRMED
    assert merged["live_price"]["corroborating_claim_count"] == 1


def test_same_value_corroboration_is_not_a_conflict():
    merged = merge_evidence_sets(
        {"live_price": _claim(8.79, source="provider-a")},
        {"live_price": _claim(8.79, source="provider-b")},
    )
    assert merged["live_price"]["status"] == CONFIRMED
    assert merged["live_price"]["corroborating_claim_count"] == 2
    assert "data_conflict" not in merged["live_price"]


def test_same_numeric_value_with_incompatible_currency_is_a_conflict():
    usd = _claim(8.79, source="provider-a")
    cny = _claim(8.79, source="provider-b")
    cny["currency"] = "CNY"
    merged = merge_evidence_sets({"live_price": usd}, {"live_price": cny})
    record = merged["live_price"]
    assert record["status"] == VERIFIED_CONFLICT
    assert record["data_conflict"][0]["currency_a"] != record["data_conflict"][0]["currency_b"]
    assert record["data_conflict"][0]["difference"] is None


def test_disagreement_creates_non_scoring_conflict():
    merged = merge_evidence_sets(
        {"live_price": _claim(9.04, source="external-ai", status=UNVERIFIED)},
        {"live_price": _claim(8.79, source="ghost-synchronized")},
    )
    record = merged["live_price"]
    assert record["status"] == VERIFIED_CONFLICT
    assert record["actual_value"] is None
    assert record["data_conflict"][0]["resolution_status"] == "UNRESOLVED"


def test_conflict_reconciliation_is_order_independent():
    external = {"live_price": _claim(9.04, source="external-ai", status=UNVERIFIED)}
    ghost = {"live_price": _claim(8.79, source="ghost-synchronized")}
    forward = merge_evidence_sets(external, ghost)["live_price"]
    reverse = merge_evidence_sets(ghost, external)["live_price"]
    assert forward == reverse
    assert forward["data_conflict"][0]["conflict_id"] == reverse["data_conflict"][0]["conflict_id"]
