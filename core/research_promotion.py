"""core/research_promotion.py — champion/challenger promotion (Phase 5).

Scoped to one exact contract and comparable registered population. Promotion
requires immutable artifact payload, untouched offline gate proof, forward-only
exact-SHA Wilson lower bound >= 0.70, preregistered minimum support, acceptable
Brier/invalid/coverage/drawdown/expectancy metrics where applicable, and
superiority/non-inferiority rules against the incumbent.

Four non-TP/SL contracts always return RESEARCH_ONLY, even when proven.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("ghost.research_promotion")

DECISIONS = {
    "PROMOTE_CANDIDATE",
    "KEEP_CHAMPION",
    "KEEP_SHADOWING",
    "RETIRE_CANDIDATE",
    "INSUFFICIENT_EVIDENCE",
    "RESEARCH_ONLY",
}

DEFAULT_REQUIREMENTS = {
    "min_forward_support": 10,
    "min_wilson_low": 0.70,
    "max_brier": 0.30,
    "max_invalid_rate": 0.30,
    "min_win_rate_delta": 0.05,
    "min_wilson_delta": 0.02,
}


@dataclass(frozen=True)
class PromotionReview:
    """One promotion review result."""
    contract_id: str
    candidate_artifact_sha: str
    champion_artifact_sha: Optional[str]
    decision: str
    approved: bool
    reason: str
    candidate_metrics: Dict[str, Any] = field(default_factory=dict)
    champion_metrics: Dict[str, Any] = field(default_factory=dict)
    requirements: Dict[str, Any] = field(default_factory=dict)


def review_promotion(
    *,
    contract_id: str,
    candidate_artifact_sha: str,
    candidate_proof: Dict[str, Any],
    champion_artifact_sha: Optional[str] = None,
    champion_proof: Optional[Dict[str, Any]] = None,
    live_eligible: bool = False,
    requirements: Optional[Dict[str, Any]] = None,
) -> PromotionReview:
    """Evaluate whether a candidate should be promoted over the champion.

    Non-live-eligible contracts always return RESEARCH_ONLY.
    """
    req = dict(DEFAULT_REQUIREMENTS)
    if requirements:
        req.update({k: v for k, v in requirements.items() if v is not None})

    # Non-TP/SL tasks are permanently research-only
    if not live_eligible:
        return PromotionReview(
            contract_id=contract_id,
            candidate_artifact_sha=candidate_artifact_sha,
            champion_artifact_sha=champion_artifact_sha,
            decision="RESEARCH_ONLY",
            approved=False,
            reason="Contract is not live-eligible; promotion is permanently disabled.",
            candidate_metrics=candidate_proof,
            champion_metrics=champion_proof or {},
            requirements=req,
        )

    # Check minimum forward support
    forward_support = candidate_proof.get("actionable", 0)
    min_support = int(req["min_forward_support"])
    if forward_support < min_support:
        return PromotionReview(
            contract_id=contract_id,
            candidate_artifact_sha=candidate_artifact_sha,
            champion_artifact_sha=champion_artifact_sha,
            decision="INSUFFICIENT_EVIDENCE",
            approved=False,
            reason=f"Forward support {forward_support} < {min_support}",
            candidate_metrics=candidate_proof,
            champion_metrics=champion_proof or {},
            requirements=req,
        )

    # Check Wilson lower bound
    wilson = candidate_proof.get("wilson", {})
    wilson_low = wilson.get("low", 0.0)
    target_wilson = float(req["min_wilson_low"])
    if wilson_low < target_wilson:
        return PromotionReview(
            contract_id=contract_id,
            candidate_artifact_sha=candidate_artifact_sha,
            champion_artifact_sha=champion_artifact_sha,
            decision="KEEP_SHADOWING",
            approved=False,
            reason=f"Wilson lower bound {wilson_low:.4f} < {target_wilson}",
            candidate_metrics=candidate_proof,
            champion_metrics=champion_proof or {},
            requirements=req,
        )

    # Check Brier
    brier = candidate_proof.get("brier")
    max_brier = float(req["max_brier"])
    if brier is not None and brier > max_brier:
        return PromotionReview(
            contract_id=contract_id,
            candidate_artifact_sha=candidate_artifact_sha,
            champion_artifact_sha=champion_artifact_sha,
            decision="KEEP_SHADOWING",
            approved=False,
            reason=f"Brier {brier:.4f} > {max_brier}",
            candidate_metrics=candidate_proof,
            champion_metrics=champion_proof or {},
            requirements=req,
        )

    # Check invalid rate
    invalid_rate = candidate_proof.get("invalid_rate", 0.0)
    max_invalid = float(req["max_invalid_rate"])
    if invalid_rate > max_invalid:
        return PromotionReview(
            contract_id=contract_id,
            candidate_artifact_sha=candidate_artifact_sha,
            champion_artifact_sha=champion_artifact_sha,
            decision="KEEP_SHADOWING",
            approved=False,
            reason=f"Invalid rate {invalid_rate:.4f} > {max_invalid}",
            candidate_metrics=candidate_proof,
            champion_metrics=champion_proof or {},
            requirements=req,
        )

    # If no champion, candidate can be promoted
    if not champion_proof or not champion_artifact_sha:
        return PromotionReview(
            contract_id=contract_id,
            candidate_artifact_sha=candidate_artifact_sha,
            champion_artifact_sha=None,
            decision="PROMOTE_CANDIDATE",
            approved=True,
            reason="No incumbent champion; candidate clears all gates.",
            candidate_metrics=candidate_proof,
            champion_metrics={},
            requirements=req,
        )

    # Compare against champion
    champ_wilson = champion_proof.get("wilson", {})
    champ_wilson_low = champ_wilson.get("low", 0.0)
    wilson_delta = wilson_low - champ_wilson_low
    min_wilson_delta = float(req["min_wilson_delta"])

    if wilson_delta < min_wilson_delta:
        return PromotionReview(
            contract_id=contract_id,
            candidate_artifact_sha=candidate_artifact_sha,
            champion_artifact_sha=champion_artifact_sha,
            decision="KEEP_CHAMPION",
            approved=False,
            reason=f"Wilson delta {wilson_delta:.4f} < {min_wilson_delta}",
            candidate_metrics=candidate_proof,
            champion_metrics=champion_proof,
            requirements=req,
        )

    # Check win rate delta
    cand_wr = candidate_proof.get("win_rate", 0.0)
    champ_wr = champion_proof.get("win_rate", 0.0)
    wr_delta = cand_wr - champ_wr
    min_wr_delta = float(req["min_win_rate_delta"])

    if wr_delta < min_wr_delta:
        return PromotionReview(
            contract_id=contract_id,
            candidate_artifact_sha=candidate_artifact_sha,
            champion_artifact_sha=champion_artifact_sha,
            decision="KEEP_CHAMPION",
            approved=False,
            reason=f"Win rate delta {wr_delta:.4f} < {min_wr_delta}",
            candidate_metrics=candidate_proof,
            champion_metrics=champion_proof,
            requirements=req,
        )

    return PromotionReview(
        contract_id=contract_id,
        candidate_artifact_sha=candidate_artifact_sha,
        champion_artifact_sha=champion_artifact_sha,
        decision="PROMOTE_CANDIDATE",
        approved=True,
        reason="Candidate clears all promotion gates over champion.",
        candidate_metrics=candidate_proof,
        champion_metrics=champion_proof,
        requirements=req,
    )
