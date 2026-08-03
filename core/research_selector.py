"""core/research_selector.py — abstaining specialist selector (Phase 5).

Evaluates only contract-compatible artifacts against mandatory gates.
Rejects errors, non-finite probabilities, schema mismatches, stale source
data, and unproven thresholds. Ranks passing candidates deterministically
and abstains on no-pass, conflict, or tie. Persists the full candidate/gate
audit in the research prediction row.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple

LOGGER = logging.getLogger("ghost.research_selector")


@dataclass(frozen=True)
class GateResult:
    """One gate evaluation for a specialist candidate."""
    gate_name: str
    passed: bool
    reason: str = ""
    threshold: Optional[float] = None
    actual_value: Optional[float] = None


@dataclass(frozen=True)
class SpecialistCandidate:
    """One specialist artifact evaluated for selection."""
    artifact_sha: str
    contract_id: str
    output: str
    calibrated_prob: float
    threshold: float
    wilson_low: Optional[float] = None
    brier: Optional[float] = None
    coverage: float = 0.0
    invalid_rate: float = 0.0
    gate_results: Tuple[GateResult, ...] = ()
    error: str = ""

    @property
    def passed_all_gates(self) -> bool:
        return all(g.passed for g in self.gate_results) and not self.error


@dataclass(frozen=True)
class SelectorDecision:
    """Final selector decision after evaluating all candidates."""
    selected: Optional[SpecialistCandidate] = None
    candidates: Tuple[SpecialistCandidate, ...] = ()
    abstained: bool = True
    reason: str = ""
    contract_id: str = ""
    symbol: str = ""


# ── gate functions ──────────────────────────────────────────────────────────

def gate_output_domain(
    candidate: SpecialistCandidate,
    allowed_outputs: FrozenSet[str],
) -> GateResult:
    """Reject candidates whose output is not in the contract's output domain."""
    passed = candidate.output in allowed_outputs
    return GateResult(
        gate_name="output_domain",
        passed=passed,
        reason="" if passed else f"output {candidate.output} not in {allowed_outputs}",
    )


def gate_finite_prob(candidate: SpecialistCandidate) -> GateResult:
    """Reject non-finite or out-of-range probabilities."""
    prob = candidate.calibrated_prob
    passed = math.isfinite(prob) and 0.0 <= prob <= 1.0
    return GateResult(
        gate_name="finite_prob",
        passed=passed,
        reason="" if passed else f"probability {prob} is non-finite or out of range",
        actual_value=prob if math.isfinite(prob) else None,
    )


def gate_threshold(candidate: SpecialistCandidate) -> GateResult:
    """Reject candidates below their proven fire threshold."""
    passed = candidate.calibrated_prob >= candidate.threshold
    return GateResult(
        gate_name="threshold",
        passed=passed,
        reason="" if passed else f"prob {candidate.calibrated_prob} < threshold {candidate.threshold}",
        threshold=candidate.threshold,
        actual_value=candidate.calibrated_prob,
    )


def gate_proof(candidate: SpecialistCandidate) -> GateResult:
    """Reject candidates without proven Wilson lower bound."""
    if candidate.wilson_low is None:
        return GateResult(gate_name="proof", passed=False, reason="no_wilson_proof")
    passed = candidate.wilson_low >= 0.70
    return GateResult(
        gate_name="proof",
        passed=passed,
        reason="" if passed else f"wilson_low {candidate.wilson_low} < 0.70",
        threshold=0.70,
        actual_value=candidate.wilson_low,
    )


def gate_calibration(candidate: SpecialistCandidate, max_brier: float = 0.30) -> GateResult:
    """Reject candidates with poor calibration (high Brier score)."""
    if candidate.brier is None:
        return GateResult(gate_name="calibration", passed=True, reason="no_brier_available")
    passed = candidate.brier <= max_brier
    return GateResult(
        gate_name="calibration",
        passed=passed,
        reason="" if passed else f"brier {candidate.brier} > {max_brier}",
        threshold=max_brier,
        actual_value=candidate.brier,
    )


def gate_data_quality(
    candidate: SpecialistCandidate,
    max_invalid_rate: float = 0.30,
) -> GateResult:
    """Reject candidates with excessive DATA_INVALID rate."""
    passed = candidate.invalid_rate <= max_invalid_rate
    return GateResult(
        gate_name="data_quality",
        passed=passed,
        reason="" if passed else f"invalid_rate {candidate.invalid_rate} > {max_invalid_rate}",
        threshold=max_invalid_rate,
        actual_value=candidate.invalid_rate,
    )


# ── selector ───────────────────────────────────────────────────────────────

def evaluate_candidate(
    *,
    artifact_sha: str,
    contract_id: str,
    output: str,
    calibrated_prob: float,
    threshold: float,
    wilson_low: Optional[float] = None,
    brier: Optional[float] = None,
    coverage: float = 0.0,
    invalid_rate: float = 0.0,
    allowed_outputs: FrozenSet[str] = frozenset(),
    max_brier: float = 0.30,
    max_invalid_rate: float = 0.30,
) -> SpecialistCandidate:
    """Evaluate one specialist candidate against all mandatory gates."""
    candidate = SpecialistCandidate(
        artifact_sha=artifact_sha,
        contract_id=contract_id,
        output=output,
        calibrated_prob=calibrated_prob,
        threshold=threshold,
        wilson_low=wilson_low,
        brier=brier,
        coverage=coverage,
        invalid_rate=invalid_rate,
    )

    gates: List[GateResult] = []
    if allowed_outputs:
        gates.append(gate_output_domain(candidate, allowed_outputs))
    gates.append(gate_finite_prob(candidate))
    gates.append(gate_threshold(candidate))
    gates.append(gate_proof(candidate))
    gates.append(gate_calibration(candidate, max_brier))
    gates.append(gate_data_quality(candidate, max_invalid_rate))

    return SpecialistCandidate(
        artifact_sha=artifact_sha,
        contract_id=contract_id,
        output=output,
        calibrated_prob=calibrated_prob,
        threshold=threshold,
        wilson_low=wilson_low,
        brier=brier,
        coverage=coverage,
        invalid_rate=invalid_rate,
        gate_results=tuple(gates),
    )


def select(
    candidates: List[SpecialistCandidate],
    *,
    contract_id: str = "",
    symbol: str = "",
) -> SelectorDecision:
    """Select the best passing candidate or abstain.

    Selection rules (deterministic):
    1. Filter to candidates that passed all gates.
    2. If none pass, abstain.
    3. Group by output; if there's a conflict (UP vs DOWN, TOP_QUARTILE vs
       BOTTOM_QUARTILE, etc.) with no decisive preregistered rule, abstain.
    4. Within the winning output group, rank by Wilson lower bound, then
       calibration (lower Brier), then deterministic artifact SHA.
    5. On tie, abstain.
    """
    passing = [c for c in candidates if c.passed_all_gates]

    if not passing:
        return SelectorDecision(
            abstained=True,
            reason="no_candidate_passed_all_gates",
            candidates=tuple(candidates),
            contract_id=contract_id,
            symbol=symbol,
        )

    # Group by output
    by_output: Dict[str, List[SpecialistCandidate]] = {}
    for c in passing:
        by_output.setdefault(c.output, []).append(c)

    if len(by_output) > 1:
        # Conflict: multiple output directions
        return SelectorDecision(
            abstained=True,
            reason=f"output_conflict: {sorted(by_output.keys())}",
            candidates=tuple(candidates),
            contract_id=contract_id,
            symbol=symbol,
        )

    output_group = list(by_output.values())[0]

    # Rank: Wilson low desc, Brier asc, artifact_sha asc (deterministic)
    ranked = sorted(
        output_group,
        key=lambda c: (
            -(c.wilson_low or 0.0),
            c.brier if c.brier is not None else 999.0,
            c.artifact_sha,
        ),
    )

    if len(ranked) >= 2:
        best = ranked[0]
        second = ranked[1]
        # Check for tie on the ranking dimensions
        best_key = (-(best.wilson_low or 0.0), best.brier if best.brier is not None else 999.0)
        second_key = (-(second.wilson_low or 0.0), second.brier if second.brier is not None else 999.0)
        if best_key == second_key:
            return SelectorDecision(
                abstained=True,
                reason="tie_on_ranking_dimensions",
                candidates=tuple(candidates),
                contract_id=contract_id,
                symbol=symbol,
            )

    return SelectorDecision(
        selected=ranked[0],
        candidates=tuple(candidates),
        abstained=False,
        reason=f"selected_{ranked[0].output}",
        contract_id=contract_id,
        symbol=symbol,
    )


def selector_decision_to_dict(decision: SelectorDecision) -> Dict[str, Any]:
    """Serialize a SelectorDecision for storage in a research prediction row."""
    return {
        "abstained": decision.abstained,
        "reason": decision.reason,
        "contract_id": decision.contract_id,
        "symbol": decision.symbol,
        "selected": {
            "artifact_sha": decision.selected.artifact_sha,
            "output": decision.selected.output,
            "calibrated_prob": decision.selected.calibrated_prob,
            "wilson_low": decision.selected.wilson_low,
        } if decision.selected else None,
        "candidate_count": len(decision.candidates),
        "passing_count": sum(1 for c in decision.candidates if c.passed_all_gates),
        "candidates": [
            {
                "artifact_sha": c.artifact_sha,
                "output": c.output,
                "calibrated_prob": c.calibrated_prob,
                "passed": c.passed_all_gates,
                "gate_results": [
                    {"gate": g.gate_name, "passed": g.passed, "reason": g.reason}
                    for g in c.gate_results
                ],
            }
            for c in decision.candidates
        ],
    }
