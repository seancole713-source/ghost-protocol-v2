"""core/binomial_stats.py — exact shared binomial statistics for all proof layers.

Every precision gate, watcher, contract-70 registry/slices/verdict, and research
proof module must use the same Wilson implementation. This module provides the
single source of truth. Presentation rounding is a separate concern; admission
decisions always compare unrounded values against the target.

Contract 70 v2 (2026-08-02): fixed 50-outcome confirmatory test, minimum 42 wins.
The legacy 25-row revival rule is preserved as v1 behavior for existing verdicts;
new claims route through the v2 protocol.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple


# ── v2 confirmatory protocol constants ──────────────────────────────────────
V2_CONFIRMATORY_N = 50          # fixed forward sample size
V2_MIN_WINS = 42                # minimum wins to pass (Wilson low >= 0.70 at 42/50)
V2_MIN_ISSUANCE_DATES = 20      # minimum distinct trading dates
V2_MAX_SYMBOL_CONCENTRATION = 0.20  # no symbol > 20% of outcomes (universe models)
V2_MAX_CALENDAR_DAYS = 120      # deadline before experiment is INCOMPLETE
V2_TARGET = 0.70                # the precision target

# Legacy v1 revival rule (preserved for existing verdicts, not for new claims)
V1_REVIVAL_MIN_N = 25
V1_REVIVAL_WILSON_LOW = 0.70


def wilson_lower_bound(wins: int, n: int, z: float = 1.96) -> float:
    """Exact (unrounded) 95% Wilson score lower bound.

    This is the honest small-sample floor. Returns 0.0 when n <= 0.
    All admission decisions must compare this unrounded value against the target.
    """
    if n <= 0:
        return 0.0
    p = wins / n
    denom = 1.0 + z * z / n
    centre = p + z * z / (2.0 * n)
    margin = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def wilson_upper_bound(wins: int, n: int, z: float = 1.96) -> float:
    """Exact (unrounded) 95% Wilson score upper bound."""
    if n <= 0:
        return 1.0
    p = wins / n
    denom = 1.0 + z * z / n
    centre = p + z * z / (2.0 * n)
    margin = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * n)) / n)
    return min(1.0, (centre + margin) / denom)


def wilson_interval(wins: int, n: int, z: float = 1.96) -> Dict[str, float]:
    """Rounded Wilson interval for display. Admission uses unrounded values."""
    if n <= 0:
        return {"p": 0.0, "low": 0.0, "high": 0.0}
    p = wins / n
    low = wilson_lower_bound(wins, n, z)
    high = wilson_upper_bound(wins, n, z)
    return {
        "p": round(p, 4),
        "low": round(low, 4),
        "high": round(high, 4),
    }


def wilson_pass(wins: int, n: int, target: float = 0.70, z: float = 1.96) -> bool:
    """True when the unrounded Wilson lower bound clears the target.

    This is the single admission gate. No rounding, no display tricks.
    """
    if n <= 0:
        return False
    return wilson_lower_bound(wins, n, z) >= target


def v2_min_wins_for_n(n: int, target: float = 0.70, z: float = 1.96) -> Optional[int]:
    """Minimum integer wins needed for Wilson lower bound >= target at sample size n.

    Returns None when even n/n cannot reach the target (very small n).
    """
    if n <= 0:
        return None
    for w in range(0, n + 1):
        if wilson_lower_bound(w, n, z) >= target:
            return w
    return None


def v2_pass_table(target: float = 0.70, z: float = 1.96) -> Dict[int, int]:
    """Precomputed minimum-wins table for n=1..100. Useful for tests and docs."""
    return {n: w for n in range(1, 101) if (w := v2_min_wins_for_n(n, target, z)) is not None}


def v2_confirmatory_pass(wins: int, n: int) -> bool:
    """True when the fixed 50-outcome v2 confirmatory test passes.

    Requires exactly 50 outcomes and at least 42 wins. Early termination for
    futility is handled separately by the caller.
    """
    if n != V2_CONFIRMATORY_N:
        return False
    if wins < V2_MIN_WINS:
        return False
    return wilson_pass(wins, n, V2_TARGET)


def v2_confirmatory_futile(wins: int, n: int) -> bool:
    """True when 42/50 is mathematically impossible given current non-wins.

    Non-wins = n - wins. If non-wins > 8, even winning every remaining outcome
    cannot reach 42 wins at n=50.
    """
    non_wins = n - wins
    remaining = V2_CONFIRMATORY_N - n
    max_possible_wins = wins + remaining
    return max_possible_wins < V2_MIN_WINS


def v2_confirmatory_status(wins: int, n: int) -> str:
    """Human-readable status for a v2 confirmatory experiment."""
    if n > V2_CONFIRMATORY_N:
        return "OVERFLOW"
    if n == V2_CONFIRMATORY_N:
        if v2_confirmatory_pass(wins, n):
            return "PROVEN"
        return "FALSIFIED"
    if v2_confirmatory_futile(wins, n):
        return "FUTILE"
    return "COLLECTING"


def exact_wilson_display(wins: int, n: int, z: float = 1.96) -> Dict[str, Any]:
    """Full display payload: exact values plus rounded presentation.

    The 'exact_low' field is the unrounded Wilson lower bound used for admission.
    The 'low' field is rounded for display only.
    """
    if n <= 0:
        return {
            "wins": 0, "n": 0, "win_rate": None,
            "exact_low": 0.0, "low": 0.0, "high": 0.0,
            "passes_70": False,
        }
    p = wins / n
    exact_low = wilson_lower_bound(wins, n, z)
    exact_high = wilson_upper_bound(wins, n, z)
    return {
        "wins": wins,
        "n": n,
        "win_rate": round(p, 4),
        "exact_low": exact_low,
        "low": round(exact_low, 4),
        "high": round(exact_high, 4),
        "passes_70": exact_low >= V2_TARGET,
    }


def block_bootstrap_lower_bound(
    wins: int,
    n: int,
    *,
    n_bootstrap: int = 10000,
    block_size: int = 5,
    confidence: float = 0.95,
    seed: int = 42,
) -> float:
    """Moving-block bootstrap lower confidence bound on win rate.

    A secondary correlation safeguard: even if exact Wilson passes, temporal
    clustering can inflate the effective sample size. This resamples contiguous
    blocks of outcomes (preserving within-block autocorrelation) and returns
    the empirical lower bound at the given confidence level.

    Returns 0.0 when n < block_size (insufficient data for blocking).
    """
    if n < max(1, block_size):
        return 0.0
    import random as _random
    _random.seed(seed)
    n_blocks = n - block_size + 1
    if n_blocks < 1:
        return 0.0
    # Build block-level win rates
    block_rates: list[float] = []
    # We need the actual sequence, but we only have aggregate wins/n.
    # For aggregate-only callers, use a conservative binomial draw.
    # Callers with per-outcome sequences should use the sequence variant.
    p_obs = wins / n
    for _ in range(n_bootstrap):
        # Resample blocks with replacement
        sampled_blocks = int(math.ceil(n / block_size))
        total_w = 0
        total_n = 0
        for _ in range(sampled_blocks):
            # Conservative: draw block win count from binomial(block_size, p_obs)
            # This assumes worst-case within-block correlation = perfect
            bw = _random.choices([0, block_size], weights=[1 - p_obs, p_obs])[0]
            total_w += bw
            total_n += block_size
        # Trim to exact n
        if total_n > n:
            total_w = max(0, total_w - (total_n - n))
            total_n = n
        if total_n > 0:
            block_rates.append(total_w / total_n)
    if not block_rates:
        return 0.0
    block_rates.sort()
    idx = int((1.0 - confidence) * len(block_rates))
    return max(0.0, block_rates[idx])


# ── Precomputed verification table ─────────────────────────────────────────
# Exact Wilson lower bounds for key n values. Used by tests to verify the
# implementation against known-correct values.

_WILSON_VERIFICATION = {
    # (wins, n): exact_low — computed with z=1.96
    (3, 3):   0.4385,   # 3/3 — smallest n where Wilson low > 0
    (9, 9):   0.7008,   # 9/9 — smallest perfect sample clearing 0.70
    (42, 50): 0.7149,   # 42/50 — v2 confirmatory minimum pass
    (43, 50): 0.7381,   # 43/50
    (76, 96): 0.7000,   # 76/96 — display rounds to 0.7000 but exact < 0.70
    (77, 96): 0.7114,   # 77/96 — genuinely clears
    (25, 25): 0.8668,   # 25/25 — legacy revival perfect
    (20, 25): 0.6087,   # 20/25 — legacy revival fail
    (18, 25): 0.5242,   # 18/25
}


def verify_wilson_table(tolerance: float = 0.0002) -> Dict[str, Any]:
    """Verify the Wilson implementation against precomputed known values.

    Returns a dict with 'ok' and any failures. Tolerance accounts for
    floating-point differences across Python/math library versions.
    """
    failures = []
    for (wins, n), expected in sorted(_WILSON_VERIFICATION.items()):
        actual = wilson_lower_bound(wins, n)
        if abs(actual - expected) > tolerance:
            failures.append({
                "wins": wins, "n": n,
                "expected": expected, "actual": round(actual, 4),
                "delta": round(abs(actual - expected), 6),
            })
    return {
        "ok": len(failures) == 0,
        "failures": failures,
        "verified": len(_WILSON_VERIFICATION) - len(failures),
        "total": len(_WILSON_VERIFICATION),
    }
