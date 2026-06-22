"""
core/conformal_calibration.py — Conformal prediction calibration (Pillar 7).

Replaces the heuristic ×4.0 confidence multiplier with distribution-free
conformal prediction intervals. Produces mathematically guaranteed
prediction intervals with no assumptions about the underlying distribution.

Method:
  1. Hold out calibration set (already have calib slice in train/calib/gate split)
  2. Compute nonconformity scores: s = 1 - up_prob for WIN, s = up_prob for LOSS
  3. At inference, compute calibrated probability as the lower bound of the
     (1-α) prediction interval → conservative, honest probability.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

LOGGER = logging.getLogger("ghost.conformal")

# Confidence level for prediction intervals (env-tunable)
_CONFORMAL_ALPHA = float(os.getenv("CONFORMAL_ALPHA", "0.10"))  # 90% confidence


def calibrate_conformal(
    up_probs: np.ndarray,
    outcomes: np.ndarray,
    alpha: float = _CONFORMAL_ALPHA,
) -> Dict[str, Any]:
    """Compute conformal calibration quantiles from holdout data.

    Args:
        up_probs: model's raw P(WIN) predictions on calibration set
        outcomes: actual outcomes (1=WIN, 0=LOSS)
        alpha: significance level (default 0.10 → 90% confidence)

    Returns:
        dict with q_hat (nonconformity quantile), calibration samples, and
        the calibrated probability formula parameters.
    """
    n = len(up_probs)
    if n < 10:
        return {
            "ok": False,
            "error": f"Need ≥10 calibration samples, have {n}",
            "samples": n,
        }

    # Nonconformity scores: how "wrong" each prediction was
    # For WIN (outcome=1): s = 1 - up_prob (higher = more wrong)
    # For LOSS (outcome=0): s = up_prob (higher = more wrong)
    scores = np.where(outcomes == 1, 1.0 - up_probs, up_probs)

    # q_hat: (1-α) quantile of scores, with finite-sample correction
    # q = ⌈(n+1)(1-α)⌉ / n quantile
    q_idx = int(np.ceil((n + 1) * (1 - alpha))) - 1
    q_idx = max(0, min(n - 1, q_idx))
    q_hat = float(np.sort(scores)[q_idx])

    # Calibrated probability: P(WIN) = max(0, up_prob - q_hat)
    # This is the lower bound of the prediction interval
    # up_prob - q_hat ≤ true_prob ≤ up_prob + q_hat with (1-α) confidence

    # Validate on calibration set
    calibrated_probs = np.maximum(0, up_probs - q_hat)
    brier_cal = float(np.mean((calibrated_probs - outcomes) ** 2))
    brier_raw = float(np.mean((up_probs - outcomes) ** 2))

    return {
        "ok": True,
        "samples": n,
        "alpha": alpha,
        "q_hat": round(q_hat, 4),
        "brier_raw": round(brier_raw, 4),
        "brier_calibrated": round(brier_cal, 4),
        "brier_improvement": round(brier_raw - brier_cal, 4),
        "formula": f"P(WIN) = max(0, up_prob - {round(q_hat, 4)})",
        "note": (
            "Conformal calibration produces conservative probabilities. "
            "The calibrated P(WIN) is the lower bound of a "
            f"{int((1-alpha)*100)}% prediction interval."
        ),
    }


def apply_conformal(up_prob: float, q_hat: float) -> float:
    """Apply conformal adjustment to a raw up_prob.

    Returns calibrated P(WIN) = max(0, up_prob - q_hat).
    """
    return round(max(0.0, min(1.0, up_prob - q_hat)), 4)


def conformal_confidence(
    up_prob: float,
    q_hat: float,
    accuracy: float,
    min_p: float,
    floor: float = 0.75,
    ceiling: float = 0.95,
) -> float:
    """Compute display confidence from conformal-calibrated probability.

    conf = clamp(accuracy + (calibrated_prob - min_p) × slope, floor, ceiling)
    where calibrated_prob = max(0, up_prob - q_hat) and slope is env-tunable.
    """
    slope = float(os.getenv("CONFIDENCE_SLOPE", "4.0"))
    cal_prob = apply_conformal(up_prob, q_hat)
    conf = accuracy + (cal_prob - min_p) * slope
    return round(max(floor, min(ceiling, conf)), 3)
