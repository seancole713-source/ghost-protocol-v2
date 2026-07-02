"""Precision-targeted firing gate — the 70% contract (Phase 3).

A model may only fire live picks above a probability threshold that
DEMONSTRABLY produced >= target precision (win rate among fired picks) on
out-of-sample data. "70% accuracy" stops being a hope and becomes a per-model
admission requirement:

  * At train time the threshold is CHOSEN on the calibration slice (lowest
    threshold whose picks won >= target with enough support) and VALIDATED on
    the untouched gate slice. Both must clear or the model is marked unproven.
  * At predict time (core.signal_engine._evaluate_lane) an unproven model
    cannot fire live picks at all — it still journals shadow probabilities and
    still serves research picks, which are excluded from accuracy stats.

No proof, no fire. Selectivity is the lever: a symbol whose model can't
demonstrate a >=70%-precision operating point out-of-sample contributes
nothing to live accuracy except risk.

Env knobs (read at call time so ops can retune without deploy):
  V3_PRECISION_GATE              on|off (default on)
  V3_PRECISION_TARGET            default 0.70
  V3_PRECISION_MIN_SUPPORT       min calib-slice picks at threshold (default 10)
  V3_PRECISION_GATE_MIN_SUPPORT  min gate-slice picks at threshold (default 5)
  V3_PRECISION_GATE_SLACK        allowed gate-slice shortfall vs target (default 0.05)
"""
import logging
import math
import os
from typing import Any, Dict, Optional, Sequence

LOGGER = logging.getLogger("ghost.precision_gate")


def precision_gate_enabled() -> bool:
    return (os.getenv("V3_PRECISION_GATE", "on") or "on").strip().lower() not in (
        "0", "off", "false", "no",
    )


def precision_target() -> float:
    try:
        return float(os.getenv("V3_PRECISION_TARGET", "0.70"))
    except Exception:
        return 0.70


def _min_support_calib() -> int:
    return max(1, int(os.getenv("V3_PRECISION_MIN_SUPPORT", "10")))


def _min_support_gate() -> int:
    return max(1, int(os.getenv("V3_PRECISION_GATE_MIN_SUPPORT", "5")))


def _gate_slack() -> float:
    try:
        return max(0.0, float(os.getenv("V3_PRECISION_GATE_SLACK", "0.05")))
    except Exception:
        return 0.05


def wilson_lower_bound(wins: int, n: int, z: float = 1.96) -> float:
    """95% Wilson score lower bound on a win rate — the honest small-sample floor."""
    if n <= 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def threshold_search(
    probs: Sequence[float],
    labels: Sequence[int],
    target: float,
    min_support: int,
) -> Optional[Dict[str, Any]]:
    """Lowest threshold whose picks (prob >= t) won >= target with enough support.

    Lowest valid threshold maximizes coverage; precision is not monotonic in t,
    so every observed probability is tried as a candidate. Returns None when no
    operating point reaches the target.
    """
    n = min(len(probs), len(labels))
    if n == 0:
        return None
    pairs = sorted(zip((float(p) for p in probs), (int(bool(l)) for l in labels)))
    best = None
    # Suffix sums over probs sorted ascending: picks at threshold pairs[i][0]
    # are pairs[i:]. Walk from the lowest candidate up; first valid wins.
    total_wins = sum(l for _, l in pairs)
    remaining = n
    wins = total_wins
    for i, (p, _l) in enumerate(pairs):
        # Ties: "prob >= p" always selects from the FIRST occurrence of p, so
        # later duplicates are not valid evaluation points.
        is_first_occurrence = i == 0 or pairs[i - 1][0] != p
        support = remaining
        if support < min_support:
            break
        if is_first_occurrence:
            precision = wins / support
            if precision >= target:
                best = {
                    "threshold": round(p, 4),
                    "precision": round(precision, 4),
                    "support": support,
                    "wins": wins,
                    "wilson_low": round(wilson_lower_bound(wins, support), 4),
                }
                break
        wins -= pairs[i][1]
        remaining -= 1
    return best


def _slice_stats(probs, labels, threshold: float) -> Dict[str, Any]:
    picked = [(float(p), int(bool(l))) for p, l in zip(probs, labels) if float(p) >= threshold]
    support = len(picked)
    wins = sum(l for _, l in picked)
    return {
        "support": support,
        "wins": wins,
        "precision": round(wins / support, 4) if support else None,
        "wilson_low": round(wilson_lower_bound(wins, support), 4) if support else None,
    }


def select_fire_threshold(
    calib_probs: Sequence[float],
    calib_labels: Sequence[int],
    gate_probs: Sequence[float],
    gate_labels: Sequence[int],
    target: Optional[float] = None,
) -> Dict[str, Any]:
    """Choose the fire threshold on the calib slice, validate on the gate slice.

    Returns a dict stored in model meta as `precision_gate`:
      ok:        True only when a threshold cleared the target on calib AND held
                 (within slack) on the untouched gate slice with enough support.
      threshold: the chosen operating point (present even when ok=False if a
                 calib candidate existed, for observability).
    """
    tgt = precision_target() if target is None else float(target)
    out: Dict[str, Any] = {"ok": False, "target": round(tgt, 4)}
    candidate = threshold_search(calib_probs, calib_labels, tgt, _min_support_calib())
    if candidate is None:
        out["fail_reason"] = "no_calib_operating_point"
        out["calib_n"] = int(min(len(calib_probs), len(calib_labels)))
        return out
    thr = float(candidate["threshold"])
    out["threshold"] = thr
    out["calib"] = candidate
    gate_stats = _slice_stats(gate_probs, gate_labels, thr)
    out["gate"] = gate_stats
    min_gate = _min_support_gate()
    if gate_stats["support"] < min_gate:
        out["fail_reason"] = f"gate_support<{min_gate} ({gate_stats['support']})"
        return out
    floor = tgt - _gate_slack()
    if (gate_stats["precision"] or 0.0) < floor:
        out["fail_reason"] = (
            f"gate_precision<{floor:.2f} ({gate_stats['precision']})"
        )
        return out
    out["ok"] = True
    return out
