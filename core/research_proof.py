"""core/research_proof.py — forward proof engine (Phase 4).

Computes exact integer support/wins, Wilson intervals, calibration statistics,
and forward-registration proof for research artifacts. Every precision claim
requires exact integer evidence and a 70% Wilson lower bound on forward-only
data. Registrations are immutable — one row per contract/artifact experiment,
never overwritten.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

LOGGER = logging.getLogger("ghost.research_proof")


# ── Wilson interval ─────────────────────────────────────────────────────────

def wilson_interval(wins: int, n: int, z: float = 1.96) -> Dict[str, float]:
    """95% Wilson score interval for a binomial proportion."""
    if n <= 0:
        return {"point": 0.0, "low": 0.0, "high": 0.0}
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    low = max(0.0, (centre - margin) / denom)
    high = min(1.0, (centre + margin) / denom)
    return {"point": round(p, 4), "low": round(low, 4), "high": round(high, 4)}


# ── proof computation ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProofResult:
    """Immutable proof result for one artifact evaluation window."""
    artifact_sha: str
    contract_id: str
    window: str             # calibration | gate | forward
    total_predictions: int
    actionable: int         # non-DATA_INVALID predictions
    wins: int
    losses: int
    expired: int = 0
    data_invalid: int = 0
    win_rate: float = 0.0
    wilson: Dict[str, float] = field(default_factory=dict)
    brier: Optional[float] = None
    coverage: float = 0.0   # actionable / total
    invalid_rate: float = 0.0  # data_invalid / total
    proven: bool = False
    target: float = 0.70

    def __post_init__(self):
        if self.actionable < 0:
            raise ValueError("actionable must be >= 0")
        if self.wins < 0 or self.losses < 0:
            raise ValueError("wins and losses must be >= 0")


def compute_proof(
    predictions: List[Dict[str, Any]],
    *,
    target_wilson_low: float = 0.70,
    z_score: float = 1.96,
    min_support: int = 10,
    expired_is_non_win: bool = True,
) -> ProofResult:
    """Compute proof statistics from a list of resolved predictions.

    Each prediction dict must have: outcome, calibrated_prob (optional).
    WIN counts as a win. LOSS and (optionally) EXPIRED count as non-win.
    DATA_INVALID is excluded from the actionable denominator but tracked.
    """
    total = len(predictions)
    wins = 0
    losses = 0
    expired = 0
    data_invalid = 0
    brier_terms: List[float] = []

    for p in predictions:
        outcome = p.get("outcome", "")
        prob = p.get("calibrated_prob")

        if outcome == "WIN":
            wins += 1
            if prob is not None and math.isfinite(prob):
                brier_terms.append((prob - 1.0) ** 2)
        elif outcome == "LOSS":
            losses += 1
            if prob is not None and math.isfinite(prob):
                brier_terms.append((prob - 0.0) ** 2)
        elif outcome == "EXPIRED":
            expired += 1
            if expired_is_non_win and prob is not None and math.isfinite(prob):
                brier_terms.append((prob - 0.0) ** 2)
        elif outcome == "DATA_INVALID":
            data_invalid += 1
            # DATA_INVALID excluded from Brier — no ground truth

    actionable = wins + losses + (expired if expired_is_non_win else 0)
    win_rate = wins / actionable if actionable > 0 else 0.0
    wilson = wilson_interval(wins, actionable, z_score) if actionable > 0 else {"point": 0.0, "low": 0.0, "high": 0.0}
    brier = sum(brier_terms) / len(brier_terms) if brier_terms else None
    coverage = actionable / total if total > 0 else 0.0
    invalid_rate = data_invalid / total if total > 0 else 0.0

    proven = (
        actionable >= min_support
        and wilson["low"] >= target_wilson_low
    )

    return ProofResult(
        artifact_sha=predictions[0].get("artifact_sha", "") if predictions else "",
        contract_id=predictions[0].get("contract_id", "") if predictions else "",
        window="forward",
        total_predictions=total,
        actionable=actionable,
        wins=wins,
        losses=losses,
        expired=expired,
        data_invalid=data_invalid,
        win_rate=round(win_rate, 4),
        wilson=wilson,
        brier=round(brier, 4) if brier is not None else None,
        coverage=round(coverage, 4),
        invalid_rate=round(invalid_rate, 4),
        proven=proven,
        target=target_wilson_low,
    )


# ── forward registration ───────────────────────────────────────────────────

def ensure_research_forward_registrations(cur) -> None:
    """Create forward registration table."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_research_forward_registrations (
            id SERIAL PRIMARY KEY,
            contract_id TEXT NOT NULL,
            artifact_sha TEXT NOT NULL,
            registered_at_ts BIGINT NOT NULL,
            universe_symbols TEXT,
            slices JSONB,
            threshold FLOAT,
            min_support INT NOT NULL,
            target_wilson_low FLOAT NOT NULL,
            family_size INT DEFAULT 1,
            correction TEXT DEFAULT '',
            selection_evidence_sha TEXT,
            created_at BIGINT NOT NULL
        )
    """)
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_research_forward_reg "
        "ON ghost_research_forward_registrations (contract_id, artifact_sha)"
    )


def register_forward_experiment(
    *,
    contract_id: str,
    artifact_sha: str,
    universe_symbols: List[str],
    threshold: float,
    min_support: int = 10,
    target_wilson_low: float = 0.70,
    slices: Optional[List[Dict[str, Any]]] = None,
    family_size: int = 1,
    correction: str = "",
    cur=None,
) -> bool:
    """Register a forward-proof experiment. One row per contract/artifact.

    Immutable — once registered, the timestamp and parameters cannot change.
    """
    if cur is not None:
        return _register_forward_impl(
            cur, contract_id, artifact_sha, universe_symbols, threshold,
            min_support, target_wilson_low, slices, family_size, correction,
        )
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        ensure_research_forward_registrations(c)
        result = _register_forward_impl(
            c, contract_id, artifact_sha, universe_symbols, threshold,
            min_support, target_wilson_low, slices, family_size, correction,
        )
        conn.commit()
        return result


def _register_forward_impl(
    cur, contract_id, artifact_sha, universe_symbols, threshold,
    min_support, target_wilson_low, slices, family_size, correction,
) -> bool:
    now = int(time.time())
    # Build selection evidence hash
    evidence = {
        "contract_id": contract_id,
        "artifact_sha": artifact_sha,
        "universe_symbols": sorted(universe_symbols),
        "threshold": threshold,
        "min_support": min_support,
        "target_wilson_low": target_wilson_low,
        "registered_at_ts": now,
    }
    selection_sha = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    cur.execute(
        """
        INSERT INTO ghost_research_forward_registrations
            (contract_id, artifact_sha, registered_at_ts, universe_symbols,
             slices, threshold, min_support, target_wilson_low, family_size,
             correction, selection_evidence_sha, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (contract_id, artifact_sha) DO NOTHING
        """,
        (
            contract_id, artifact_sha, now,
            json.dumps(sorted(universe_symbols)),
            json.dumps(slices or []),
            threshold, min_support, target_wilson_low,
            family_size, correction, selection_sha, now,
        ),
    )
    return cur.rowcount > 0


def get_forward_registration(
    contract_id: str,
    artifact_sha: str,
    cur=None,
) -> Optional[Dict[str, Any]]:
    """Load a forward registration."""
    if cur is not None:
        return _get_forward_impl(cur, contract_id, artifact_sha)
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        return _get_forward_impl(c, contract_id, artifact_sha)


def _get_forward_impl(cur, contract_id, artifact_sha) -> Optional[Dict[str, Any]]:
    cur.execute(
        """
        SELECT contract_id, artifact_sha, registered_at_ts, universe_symbols,
               slices, threshold, min_support, target_wilson_low, family_size,
               correction, selection_evidence_sha
        FROM ghost_research_forward_registrations
        WHERE contract_id = %s AND artifact_sha = %s
        """,
        (contract_id, artifact_sha),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "contract_id": row[0],
        "artifact_sha": row[1],
        "registered_at_ts": row[2],
        "universe_symbols": json.loads(row[3]) if isinstance(row[3], str) else row[3],
        "slices": json.loads(row[4]) if isinstance(row[4], str) else row[4],
        "threshold": row[5],
        "min_support": row[6],
        "target_wilson_low": row[7],
        "family_size": row[8],
        "correction": row[9],
        "selection_evidence_sha": row[10],
    }


def evaluate_forward_proof(
    predictions: List[Dict[str, Any]],
    registration: Dict[str, Any],
    *,
    z_score: float = 1.96,
    expired_is_non_win: bool = True,
) -> ProofResult:
    """Evaluate forward-only proof against a registration.

    Only predictions issued strictly after registered_at_ts count.
    """
    registered_at = registration["registered_at_ts"]
    threshold = registration.get("threshold", 0.0)
    universe = set(registration.get("universe_symbols") or [])

    forward = [
        p for p in predictions
        if p.get("issued_ts", 0) > registered_at
        and p.get("symbol", "") in universe
        and p.get("calibrated_prob", 0) >= threshold
    ]

    return compute_proof(
        forward,
        target_wilson_low=registration["target_wilson_low"],
        z_score=z_score,
        min_support=registration["min_support"],
        expired_is_non_win=expired_is_non_win,
    )
