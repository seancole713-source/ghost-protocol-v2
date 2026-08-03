"""Tests for core/research_promotion.py — champion/challenger promotion."""
import pytest
from core.research_promotion import (
    review_promotion,
    PromotionReview,
    DEFAULT_REQUIREMENTS,
)


def _candidate_proof(**overrides):
    kwargs = {
        "actionable": 20,
        "wins": 16,
        "losses": 4,
        "win_rate": 0.80,
        "wilson": {"point": 0.80, "low": 0.72, "high": 0.88},
        "brier": 0.15,
        "invalid_rate": 0.05,
    }
    kwargs.update(overrides)
    return kwargs


def _champion_proof(**overrides):
    kwargs = {
        "actionable": 30,
        "wins": 20,
        "losses": 10,
        "win_rate": 0.667,
        "wilson": {"point": 0.667, "low": 0.55, "high": 0.78},
        "brier": 0.20,
        "invalid_rate": 0.03,
    }
    kwargs.update(overrides)
    return kwargs


# ── research-only contracts ────────────────────────────────────────────────

def test_research_only_contract_always_returns_research_only():
    result = review_promotion(
        contract_id="abc",
        candidate_artifact_sha="a" * 64,
        candidate_proof=_candidate_proof(),
        live_eligible=False,
    )
    assert result.decision == "RESEARCH_ONLY"
    assert result.approved is False


# ── insufficient evidence ─────────────────────────────────────────────────

def test_insufficient_forward_support():
    result = review_promotion(
        contract_id="abc",
        candidate_artifact_sha="a" * 64,
        candidate_proof=_candidate_proof(actionable=5),
        live_eligible=True,
    )
    assert result.decision == "INSUFFICIENT_EVIDENCE"


# ── keep shadowing ─────────────────────────────────────────────────────────

def test_keep_shadowing_low_wilson():
    result = review_promotion(
        contract_id="abc",
        candidate_artifact_sha="a" * 64,
        candidate_proof=_candidate_proof(wilson={"point": 0.60, "low": 0.40, "high": 0.80}),
        live_eligible=True,
    )
    assert result.decision == "KEEP_SHADOWING"


def test_keep_shadowing_high_brier():
    result = review_promotion(
        contract_id="abc",
        candidate_artifact_sha="a" * 64,
        candidate_proof=_candidate_proof(brier=0.50),
        live_eligible=True,
    )
    assert result.decision == "KEEP_SHADOWING"


def test_keep_shadowing_high_invalid_rate():
    result = review_promotion(
        contract_id="abc",
        candidate_artifact_sha="a" * 64,
        candidate_proof=_candidate_proof(invalid_rate=0.50),
        live_eligible=True,
    )
    assert result.decision == "KEEP_SHADOWING"


# ── promote candidate ─────────────────────────────────────────────────────

def test_promote_no_champion():
    result = review_promotion(
        contract_id="abc",
        candidate_artifact_sha="a" * 64,
        candidate_proof=_candidate_proof(),
        live_eligible=True,
    )
    assert result.decision == "PROMOTE_CANDIDATE"
    assert result.approved is True


def test_promote_over_champion():
    result = review_promotion(
        contract_id="abc",
        candidate_artifact_sha="a" * 64,
        candidate_proof=_candidate_proof(),
        champion_artifact_sha="b" * 64,
        champion_proof=_champion_proof(),
        live_eligible=True,
    )
    assert result.decision == "PROMOTE_CANDIDATE"
    assert result.approved is True


# ── keep champion ─────────────────────────────────────────────────────────

def test_keep_champion_small_wilson_delta():
    # Both clear absolute Wilson gate, but delta is too small
    result = review_promotion(
        contract_id="abc",
        candidate_artifact_sha="a" * 64,
        candidate_proof=_candidate_proof(wilson={"point": 0.72, "low": 0.71, "high": 0.73}),
        champion_artifact_sha="b" * 64,
        champion_proof=_champion_proof(wilson={"point": 0.72, "low": 0.70, "high": 0.74}),
        live_eligible=True,
    )
    assert result.decision == "KEEP_CHAMPION"


def test_keep_champion_small_win_rate_delta():
    result = review_promotion(
        contract_id="abc",
        candidate_artifact_sha="a" * 64,
        candidate_proof=_candidate_proof(win_rate=0.68),
        champion_artifact_sha="b" * 64,
        champion_proof=_champion_proof(win_rate=0.667),
        live_eligible=True,
    )
    assert result.decision == "KEEP_CHAMPION"


# ── frozen invariants ─────────────────────────────────────────────────────

def test_promotion_review_is_frozen():
    r = PromotionReview(
        contract_id="abc", candidate_artifact_sha="a" * 64,
        champion_artifact_sha=None, decision="PROMOTE_CANDIDATE",
        approved=True, reason="test",
    )
    with pytest.raises(Exception):
        r.decision = "KEEP_CHAMPION"  # type: ignore
