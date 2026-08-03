"""Tests for core/research_selector.py — abstaining specialist selector."""
import math
import pytest
from core.research_selector import (
    GateResult,
    SpecialistCandidate,
    SelectorDecision,
    gate_output_domain,
    gate_finite_prob,
    gate_threshold,
    gate_proof,
    gate_calibration,
    gate_data_quality,
    evaluate_candidate,
    select,
    selector_decision_to_dict,
)


# ── gate tests ─────────────────────────────────────────────────────────────

def test_gate_output_domain_passes():
    c = SpecialistCandidate(artifact_sha="a" * 64, contract_id="abc", output="UP",
                            calibrated_prob=0.75, threshold=0.55)
    result = gate_output_domain(c, frozenset({"UP", "DOWN"}))
    assert result.passed is True


def test_gate_output_domain_rejects():
    c = SpecialistCandidate(artifact_sha="a" * 64, contract_id="abc", output="EXPAND",
                            calibrated_prob=0.75, threshold=0.55)
    result = gate_output_domain(c, frozenset({"UP", "DOWN"}))
    assert result.passed is False


def test_gate_finite_prob_rejects_nan():
    c = SpecialistCandidate(artifact_sha="a" * 64, contract_id="abc", output="UP",
                            calibrated_prob=float("nan"), threshold=0.55)
    result = gate_finite_prob(c)
    assert result.passed is False


def test_gate_finite_prob_rejects_inf():
    c = SpecialistCandidate(artifact_sha="a" * 64, contract_id="abc", output="UP",
                            calibrated_prob=float("inf"), threshold=0.55)
    result = gate_finite_prob(c)
    assert result.passed is False


def test_gate_finite_prob_rejects_out_of_range():
    c = SpecialistCandidate(artifact_sha="a" * 64, contract_id="abc", output="UP",
                            calibrated_prob=1.5, threshold=0.55)
    result = gate_finite_prob(c)
    assert result.passed is False


def test_gate_threshold_passes():
    c = SpecialistCandidate(artifact_sha="a" * 64, contract_id="abc", output="UP",
                            calibrated_prob=0.75, threshold=0.55)
    result = gate_threshold(c)
    assert result.passed is True


def test_gate_threshold_rejects():
    c = SpecialistCandidate(artifact_sha="a" * 64, contract_id="abc", output="UP",
                            calibrated_prob=0.50, threshold=0.55)
    result = gate_threshold(c)
    assert result.passed is False


def test_gate_proof_passes():
    c = SpecialistCandidate(artifact_sha="a" * 64, contract_id="abc", output="UP",
                            calibrated_prob=0.75, threshold=0.55, wilson_low=0.72)
    result = gate_proof(c)
    assert result.passed is True


def test_gate_proof_rejects_low_wilson():
    c = SpecialistCandidate(artifact_sha="a" * 64, contract_id="abc", output="UP",
                            calibrated_prob=0.75, threshold=0.55, wilson_low=0.30)
    result = gate_proof(c)
    assert result.passed is False


def test_gate_proof_rejects_missing_wilson():
    c = SpecialistCandidate(artifact_sha="a" * 64, contract_id="abc", output="UP",
                            calibrated_prob=0.75, threshold=0.55, wilson_low=None)
    result = gate_proof(c)
    assert result.passed is False


def test_gate_calibration_passes():
    c = SpecialistCandidate(artifact_sha="a" * 64, contract_id="abc", output="UP",
                            calibrated_prob=0.75, threshold=0.55, brier=0.15)
    result = gate_calibration(c, max_brier=0.30)
    assert result.passed is True


def test_gate_calibration_rejects_high_brier():
    c = SpecialistCandidate(artifact_sha="a" * 64, contract_id="abc", output="UP",
                            calibrated_prob=0.75, threshold=0.55, brier=0.50)
    result = gate_calibration(c, max_brier=0.30)
    assert result.passed is False


def test_gate_data_quality_passes():
    c = SpecialistCandidate(artifact_sha="a" * 64, contract_id="abc", output="UP",
                            calibrated_prob=0.75, threshold=0.55, invalid_rate=0.05)
    result = gate_data_quality(c, max_invalid_rate=0.30)
    assert result.passed is True


def test_gate_data_quality_rejects():
    c = SpecialistCandidate(artifact_sha="a" * 64, contract_id="abc", output="UP",
                            calibrated_prob=0.75, threshold=0.55, invalid_rate=0.50)
    result = gate_data_quality(c, max_invalid_rate=0.30)
    assert result.passed is False


# ── evaluate_candidate ────────────────────────────────────────────────────

def test_evaluate_candidate_all_pass():
    c = evaluate_candidate(
        artifact_sha="a" * 64, contract_id="abc", output="UP",
        calibrated_prob=0.75, threshold=0.55, wilson_low=0.72,
        brier=0.15, allowed_outputs=frozenset({"UP", "DOWN"}),
    )
    assert c.passed_all_gates is True
    assert len(c.gate_results) == 6


def test_evaluate_candidate_fails_on_output():
    c = evaluate_candidate(
        artifact_sha="a" * 64, contract_id="abc", output="EXPAND",
        calibrated_prob=0.75, threshold=0.55, wilson_low=0.72,
        allowed_outputs=frozenset({"UP", "DOWN"}),
    )
    assert c.passed_all_gates is False


# ── select ─────────────────────────────────────────────────────────────────

def _make_candidate(sha="a", output="UP", prob=0.75, threshold=0.55, wilson=0.72, brier=0.15):
    return SpecialistCandidate(
        artifact_sha=sha * 64, contract_id="abc", output=output,
        calibrated_prob=prob, threshold=threshold, wilson_low=wilson,
        brier=brier,
        gate_results=(
            GateResult("output_domain", True),
            GateResult("finite_prob", True),
            GateResult("threshold", True),
            GateResult("proof", True),
            GateResult("calibration", True),
            GateResult("data_quality", True),
        ),
    )


def test_select_single_candidate():
    c = _make_candidate()
    decision = select([c], contract_id="abc", symbol="WOLF")
    assert decision.abstained is False
    assert decision.selected is not None
    assert decision.selected.artifact_sha == c.artifact_sha


def test_select_abstains_when_none_pass():
    c = SpecialistCandidate(
        artifact_sha="a" * 64, contract_id="abc", output="UP",
        calibrated_prob=0.50, threshold=0.55,
        gate_results=(GateResult("threshold", False, "below threshold"),),
    )
    decision = select([c], contract_id="abc", symbol="WOLF")
    assert decision.abstained is True
    assert "no_candidate_passed" in decision.reason


def test_select_abstains_on_output_conflict():
    c1 = _make_candidate(sha="a", output="UP")
    c2 = _make_candidate(sha="b", output="DOWN")
    decision = select([c1, c2], contract_id="abc", symbol="WOLF")
    assert decision.abstained is True
    assert "output_conflict" in decision.reason


def test_select_ranks_by_wilson():
    c1 = _make_candidate(sha="a", wilson=0.72)
    c2 = _make_candidate(sha="b", wilson=0.80)
    decision = select([c1, c2], contract_id="abc", symbol="WOLF")
    assert decision.abstained is False
    assert decision.selected.artifact_sha == c2.artifact_sha


def test_select_abstains_on_tie():
    c1 = _make_candidate(sha="a", wilson=0.72, brier=0.15)
    c2 = _make_candidate(sha="b", wilson=0.72, brier=0.15)
    decision = select([c1, c2], contract_id="abc", symbol="WOLF")
    assert decision.abstained is True
    assert "tie" in decision.reason


def test_selector_decision_to_dict():
    c = _make_candidate()
    decision = select([c], contract_id="abc", symbol="WOLF")
    d = selector_decision_to_dict(decision)
    assert d["abstained"] is False
    assert d["selected"] is not None
    assert d["candidate_count"] == 1
    assert d["passing_count"] == 1


# ── frozen invariants ─────────────────────────────────────────────────────

def test_gate_result_is_frozen():
    g = GateResult("test", True)
    with pytest.raises(Exception):
        g.passed = False  # type: ignore


def test_specialist_candidate_is_frozen():
    c = _make_candidate()
    with pytest.raises(Exception):
        c.calibrated_prob = 0.99  # type: ignore


def test_selector_decision_is_frozen():
    c = _make_candidate()
    d = select([c])
    with pytest.raises(Exception):
        d.abstained = True  # type: ignore
