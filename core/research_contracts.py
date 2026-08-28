"""core/research_contracts.py — immutable prediction-task contracts (Phase 1).

Every research prediction task is governed by an exact, versioned, frozen
contract. A contract defines the output domain, outcome domain, horizon,
feature/evidence schemas, resolver identity, proof policy, allowed data
sources, and whether the task may ever become live-eligible.

Contracts are canonicalised by SHA-256 and registered once. Duplicate
names/versions with different payloads are rejected at registration time.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

LOGGER = logging.getLogger("ghost.research_contracts")
CURRENT_LIVE_CONTRACT_VERSION = "v3"

# ── frozen spec types ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class SourceSpec:
    """One allowed data source for a contract."""
    source_id: str          # e.g. "daily_ohlcv", "sec_fundamentals", "news_events"
    required: bool = True   # dataset must include this source
    max_staleness_s: int = 86400  # max age of source data at prediction time


@dataclass(frozen=True)
class OutcomeSpec:
    """What a resolved outcome looks like for this contract."""
    terminal_outcomes: FrozenSet[str]  # e.g. {"WIN", "LOSS", "EXPIRED"}
    invalid_outcome: str = "DATA_INVALID"  # explicit data-quality failure
    # Whether EXPIRED counts as non-WIN in precision denominators
    expired_is_non_win: bool = True


@dataclass(frozen=True)
class ProofSpec:
    """Proof requirements for this contract."""
    target_wilson_low: float = 0.70
    min_support: int = 20
    min_forward_support: int = 10
    z_score: float = 1.96  # 95% confidence
    # Whether this contract supports precision claims at all
    precision_applicable: bool = True


@dataclass(frozen=True)
class PredictionContract:
    """Immutable specification for one prediction task.

    Once registered, a contract's identity (SHA-256 of its canonical payload)
    must never change. Lifecycle state transitions (ACTIVE → RETIRED) are
    recorded in the research artifact registry, not by mutating this object.
    """
    name: str                    # e.g. "tp_sl_swing"
    version: str                 # e.g. "v1"
    description: str
    output_domain: FrozenSet[str]  # allowed model outputs, e.g. {"UP", "DOWN"}
    outcome_domain: OutcomeSpec
    horizon_bars: int            # forward bars for resolution
    feature_schema: str          # feature set identity
    evidence_schema: str         # label/evidence identity
    validation_schema: str       # train/calib/gate split identity
    resolver_id: str             # e.g. "tp_sl_bar_path/v1"
    resolver_version: str
    proof: ProofSpec
    allowed_sources: Tuple[SourceSpec, ...]
    live_eligible: bool = False  # only tp_sl_swing may be True
    lifecycle: str = "ACTIVE"    # ACTIVE | DRAFT | RETIRED

    def contract_id(self) -> str:
        """SHA-256 of the canonical JSON payload — immutable identity."""
        return _contract_sha(self)

    def __post_init__(self):
        if not self.name or not self.version:
            raise ValueError("Contract name and version are required")
        if self.live_eligible and self.name != "tp_sl_swing":
            raise ValueError(
                f"Only tp_sl_swing may be live_eligible, got {self.name}"
            )
        if self.horizon_bars < 1:
            raise ValueError(f"horizon_bars must be >= 1, got {self.horizon_bars}")


# ── canonicalisation ───────────────────────────────────────────────────────

def _contract_canonical(contract: PredictionContract) -> Dict[str, Any]:
    """Deterministic JSON-serialisable representation for hashing."""
    return {
        "name": contract.name,
        "version": contract.version,
        "description": contract.description,
        "output_domain": sorted(contract.output_domain),
        "outcome": {
            "terminal_outcomes": sorted(contract.outcome_domain.terminal_outcomes),
            "invalid_outcome": contract.outcome_domain.invalid_outcome,
            "expired_is_non_win": contract.outcome_domain.expired_is_non_win,
        },
        "horizon_bars": contract.horizon_bars,
        "feature_schema": contract.feature_schema,
        "evidence_schema": contract.evidence_schema,
        "validation_schema": contract.validation_schema,
        "resolver_id": contract.resolver_id,
        "resolver_version": contract.resolver_version,
        "proof": {
            "target_wilson_low": contract.proof.target_wilson_low,
            "min_support": contract.proof.min_support,
            "min_forward_support": contract.proof.min_forward_support,
            "z_score": contract.proof.z_score,
            "precision_applicable": contract.proof.precision_applicable,
        },
        "allowed_sources": sorted(
            (
                {
                    "source_id": s.source_id,
                    "required": s.required,
                    "max_staleness_s": s.max_staleness_s,
                }
                for s in contract.allowed_sources
            ),
            key=lambda d: d["source_id"],
        ),
        "live_eligible": contract.live_eligible,
    }


def _contract_sha(contract: PredictionContract) -> str:
    payload = json.dumps(_contract_canonical(contract), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


# ── registry ───────────────────────────────────────────────────────────────

_REGISTRY: Dict[str, PredictionContract] = {}


def register_contract(contract: PredictionContract) -> str:
    """Register a contract. Returns the contract ID (SHA-256).

    Raises ValueError if a contract with the same name+version but different
    payload already exists.
    """
    cid = contract.contract_id()
    key = f"{contract.name}/{contract.version}"
    existing = _REGISTRY.get(key)
    if existing is not None:
        if existing.contract_id() != cid:
            raise ValueError(
                f"Contract {key} already registered with different payload "
                f"(existing={existing.contract_id()}, new={cid})"
            )
        return cid
    _REGISTRY[key] = contract
    LOGGER.info("Registered contract %s → %s", key, cid[:12])
    return cid


def get_contract(name: str, version: str = "v1") -> Optional[PredictionContract]:
    """Look up a registered contract by name and version."""
    return _REGISTRY.get(f"{name}/{version}")


def get_contract_by_id(contract_id: str) -> Optional[PredictionContract]:
    """Look up a registered contract by its SHA-256 ID."""
    for contract in _REGISTRY.values():
        if contract.contract_id() == contract_id:
            return contract
    return None


def list_contracts() -> List[PredictionContract]:
    """All registered contracts."""
    return sorted(_REGISTRY.values(), key=lambda c: (c.name, c.version))


# ── live-compatibility boundary ─────────────────────────────────────────────

def is_live_compatible(contract: PredictionContract) -> bool:
    """True only for exact tp_sl_swing contracts that match current live schemas.

    This is the single gate that prevents non-TP/SL research artifacts from
    ever entering the live prediction path. It checks:
      - contract is tp_sl_swing
      - live_eligible flag is True
      - output domain is exactly {"UP", "DOWN"}
      - schemas match current live configuration
    """
    if contract.lifecycle != "ACTIVE":
        return False
    if not contract.live_eligible:
        return False
    if contract.name != "tp_sl_swing":
        return False
    if contract.version != CURRENT_LIVE_CONTRACT_VERSION:
        return False
    if contract.output_domain != frozenset({"UP", "DOWN"}):
        return False
    # Schema compatibility with current live config
    try:
        from core.signal_engine import (
            _v3_feature_schema,
            _v3_label_schema,
            _v3_validation_schema,
            V3_LABEL_HOLD_BARS,
        )
    except ImportError:
        return False
    if contract.feature_schema != _v3_feature_schema():
        return False
    if contract.evidence_schema != _v3_label_schema():
        return False
    if contract.validation_schema != _v3_validation_schema():
        return False
    if contract.horizon_bars != V3_LABEL_HOLD_BARS:
        return False
    return True


def live_compatible_contract() -> Optional[PredictionContract]:
    """Return the currently registered live-compatible contract, if any."""
    for contract in _REGISTRY.values():
        if is_live_compatible(contract):
            return contract
    return None


# ── v1 contract definitions ────────────────────────────────────────────────

def _register_contracts() -> None:
    """Register frozen historical contracts and the current live contract."""
    from core.signal_engine import (
        _v3_feature_schema,
        _v3_label_schema,
        _v3_validation_schema,
        V3_LABEL_HOLD_BARS,
    )

    # ── tp_sl_swing/v1 (live-eligible) ──────────────────────────────────
    register_contract(PredictionContract(
        name="tp_sl_swing",
        version="v1",
        description=(
            "Directional TP/SL swing prediction. UP or DOWN with volatility-"
            "derived target/stop geometry. 3 completed daily bars. First "
            "chronological touch wins; same-bar target+stop collision is LOSS; "
            "no touch after complete horizon is EXPIRED. Precision denominator "
            "is WIN vs non-WIN (LOSS + EXPIRED)."
        ),
        output_domain=frozenset({"UP", "DOWN"}),
        outcome_domain=OutcomeSpec(
            terminal_outcomes=frozenset({"WIN", "LOSS", "EXPIRED"}),
            expired_is_non_win=True,
        ),
        horizon_bars=V3_LABEL_HOLD_BARS,
        feature_schema=_v3_feature_schema(),
        evidence_schema=_v3_label_schema(),
        validation_schema=_v3_validation_schema(),
        resolver_id="tp_sl_bar_path/v1",
        resolver_version="1.0.0",
        proof=ProofSpec(target_wilson_low=0.70, min_support=20, min_forward_support=10),
        allowed_sources=(
            SourceSpec("daily_ohlcv", required=True, max_staleness_s=86400),
        ),
        live_eligible=True,
        lifecycle="RETIRED",
    ))

    # Preserve the immediately previous contract for historical artifact lookup.
    legacy_feature_schema = _v3_feature_schema().removeprefix("tech3+")
    legacy_feature_schema = legacy_feature_schema.replace("+cs0", "").replace("+cs1", "")
    legacy_feature_schema = legacy_feature_schema.replace("+macro0", "").replace("+macro1", "")
    legacy_validation_schema = _v3_validation_schema().replace(
        "honest_oos_v4", "honest_oos_v3", 1,
    )
    register_contract(PredictionContract(
        name="tp_sl_swing",
        version="v2",
        description=(
            "Historical directional TP/SL swing contract before effective-"
            "session proof and train/serve-safe feature schema enforcement."
        ),
        output_domain=frozenset({"UP", "DOWN"}),
        outcome_domain=OutcomeSpec(
            terminal_outcomes=frozenset({"WIN", "LOSS", "EXPIRED"}),
            expired_is_non_win=True,
        ),
        horizon_bars=V3_LABEL_HOLD_BARS,
        feature_schema=legacy_feature_schema,
        evidence_schema=_v3_label_schema(),
        validation_schema=legacy_validation_schema,
        resolver_id="tp_sl_bar_path/v1",
        resolver_version="1.0.0",
        proof=ProofSpec(target_wilson_low=0.70, min_support=20, min_forward_support=10),
        allowed_sources=(
            SourceSpec("daily_ohlcv", required=True, max_staleness_s=86400),
        ),
        live_eligible=True,
        lifecycle="RETIRED",
    ))

    # v3 binds the effective-session proof and train/serve-safe feature schema.
    # The resolver algorithm is unchanged, so its independently versioned ID
    # remains v1.
    register_contract(PredictionContract(
        name="tp_sl_swing",
        version=CURRENT_LIVE_CONTRACT_VERSION,
        description=(
            "Directional TP/SL swing prediction. UP or DOWN with volatility-"
            f"derived target/stop geometry. {V3_LABEL_HOLD_BARS} completed daily "
            "bars. First chronological touch wins; same-bar target+stop "
            "collision is LOSS; no touch after the complete horizon is EXPIRED. "
            "Precision denominator is WIN vs non-WIN (LOSS + EXPIRED)."
        ),
        output_domain=frozenset({"UP", "DOWN"}),
        outcome_domain=OutcomeSpec(
            terminal_outcomes=frozenset({"WIN", "LOSS", "EXPIRED"}),
            expired_is_non_win=True,
        ),
        horizon_bars=V3_LABEL_HOLD_BARS,
        feature_schema=_v3_feature_schema(),
        evidence_schema=_v3_label_schema(),
        validation_schema=_v3_validation_schema(),
        resolver_id="tp_sl_bar_path/v1",
        resolver_version="1.0.0",
        proof=ProofSpec(target_wilson_low=0.70, min_support=20, min_forward_support=10),
        allowed_sources=(
            SourceSpec("daily_ohlcv", required=True, max_staleness_s=86400),
        ),
        live_eligible=True,
    ))

    # ── intraday_continuation/v1 (research-only) ────────────────────────
    register_contract(PredictionContract(
        name="intraday_continuation",
        version="v1",
        description=(
            "Intraday directional continuation. UP or DOWN. Entry at first "
            "eligible 1-hour bar after issuance. Fixed 60-minute horizon. "
            "Correct directional close-to-close net return after a frozen "
            "cost/neutral band is WIN; otherwise LOSS. Missing/incomplete "
            "session evidence is DATA_INVALID."
        ),
        output_domain=frozenset({"UP", "DOWN"}),
        outcome_domain=OutcomeSpec(
            terminal_outcomes=frozenset({"WIN", "LOSS"}),
        ),
        horizon_bars=1,  # 1 hour
        feature_schema="intraday_1h_v1",
        evidence_schema="intraday_continuation_v1",
        validation_schema="honest_oos_v1:train=0.70:calib=0.15:purge=1",
        resolver_id="intraday_continuation/v1",
        resolver_version="1.0.0",
        proof=ProofSpec(target_wilson_low=0.70, min_support=30, min_forward_support=15),
        allowed_sources=(
            SourceSpec("hourly_ohlcv", required=True, max_staleness_s=3600),
        ),
        live_eligible=False,
    ))

    # ── volatility_expansion/v1 (research-only) ─────────────────────────
    register_contract(PredictionContract(
        name="volatility_expansion",
        version="v1",
        description=(
            "Volatility regime prediction. EXPAND or CONTRACT. Compare "
            "annualized realized volatility over the next 5 completed daily "
            "returns with the prior 10 completed daily returns using a frozen "
            "expansion ratio. Correct class is WIN; incorrect/tie is LOSS. "
            "Never emits UP/DOWN or prices."
        ),
        output_domain=frozenset({"EXPAND", "CONTRACT"}),
        outcome_domain=OutcomeSpec(
            terminal_outcomes=frozenset({"WIN", "LOSS"}),
        ),
        horizon_bars=5,
        feature_schema="volatility_v1",
        evidence_schema="volatility_expansion_v1",
        validation_schema="honest_oos_v1:train=0.70:calib=0.15:purge=5",
        resolver_id="volatility_expansion/v1",
        resolver_version="1.0.0",
        proof=ProofSpec(target_wilson_low=0.70, min_support=20, min_forward_support=10),
        allowed_sources=(
            SourceSpec("daily_ohlcv", required=True, max_staleness_s=86400),
        ),
        live_eligible=False,
    ))

    # ── cross_sectional_ranking/v1 (research-only) ──────────────────────
    register_contract(PredictionContract(
        name="cross_sectional_ranking",
        version="v1",
        description=(
            "Cross-sectional relative-strength ranking. TOP_QUARTILE or "
            "BOTTOM_QUARTILE. Preregistered universe/date cohorts only. "
            "5-day total return compared against synchronized peer quartiles. "
            "Middle-half actuals are LOSS for an actionable extreme call. "
            "Cohorts with inadequate synchronized coverage are DATA_INVALID."
        ),
        output_domain=frozenset({"TOP_QUARTILE", "BOTTOM_QUARTILE"}),
        outcome_domain=OutcomeSpec(
            terminal_outcomes=frozenset({"WIN", "LOSS"}),
        ),
        horizon_bars=5,
        feature_schema="cross_sectional_v1",
        evidence_schema="cross_sectional_ranking_v1",
        validation_schema="honest_oos_v1:train=0.70:calib=0.15:purge=5",
        resolver_id="cross_sectional_ranking/v1",
        resolver_version="1.0.0",
        proof=ProofSpec(target_wilson_low=0.70, min_support=30, min_forward_support=15),
        allowed_sources=(
            SourceSpec("daily_ohlcv", required=True, max_staleness_s=86400),
        ),
        live_eligible=False,
    ))

    # ── event_reaction/v1 (research-only) ───────────────────────────────
    register_contract(PredictionContract(
        name="event_reaction",
        version="v1",
        description=(
            "Event-driven reaction prediction. POSITIVE or NEGATIVE. "
            "Deterministic direct-event IDs from ghost_news_events; peer-derived "
            "events are advisory-only and excluded. Entry is first executable "
            "executable bar after publication. 3-session symbol return minus "
            "SPY return after a frozen cost/neutral band determines outcome. "
            "Wrong/neutral is LOSS. Unavailable event/feed/bar evidence is "
            "DATA_INVALID."
        ),
        output_domain=frozenset({"POSITIVE", "NEGATIVE"}),
        outcome_domain=OutcomeSpec(
            terminal_outcomes=frozenset({"WIN", "LOSS"}),
        ),
        horizon_bars=3,
        feature_schema="event_reaction_v1",
        evidence_schema="event_reaction_v1",
        validation_schema="honest_oos_v1:train=0.70:calib=0.15:purge=3",
        resolver_id="event_reaction/v1",
        resolver_version="1.0.0",
        proof=ProofSpec(target_wilson_low=0.70, min_support=20, min_forward_support=10),
        allowed_sources=(
            SourceSpec("daily_ohlcv", required=True, max_staleness_s=86400),
            SourceSpec("news_events", required=True, max_staleness_s=86400),
        ),
        live_eligible=False,
    ))

    LOGGER.info("research contracts registered (%d total)", len(_REGISTRY))


# Auto-register on import
_register_contracts()
