"""Phase 3 — gate-slice feature audit, sign correction, regime peer weighting."""
from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

import numpy as np

REGIME_KEYS = ("atr_pct", "mom_4h", "pct_b")


def _v3_feature_audit_enabled() -> bool:
    return (os.getenv("V3_FEATURE_AUDIT", "on") or "on").strip().lower() not in (
        "0", "off", "false", "no",
    )


def _v3_feature_invert_min_corr() -> float:
    try:
        return float(os.getenv("V3_FEATURE_INVERT_MIN_CORR", "-0.08"))
    except Exception:
        return -0.08


def _v3_regime_peer_weight_enabled() -> bool:
    return (os.getenv("V3_REGIME_PEER_WEIGHT", "on") or "on").strip().lower() not in (
        "0", "off", "false", "no",
    )


def point_biserial_corr(x: np.ndarray, y: np.ndarray) -> Optional[float]:
    """Pearson correlation between continuous x and binary y (0/1)."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if len(x) < 5 or np.unique(y).size < 2:
        return None
    if np.std(x) < 1e-12:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def audit_gate_features(
    X_gate: np.ndarray,
    y_gate: np.ndarray,
    feature_cols: Sequence[str],
    *,
    min_n: int = 10,
) -> List[Dict[str, Any]]:
    """Per-feature gate-slice correlation with WIN label; flags likely inversions."""
    y_gate = np.asarray(y_gate)
    if len(y_gate) < min_n:
        return []
    threshold = _v3_feature_invert_min_corr()
    out: List[Dict[str, Any]] = []
    for i, name in enumerate(feature_cols):
        corr = point_biserial_corr(X_gate[:, i], y_gate)
        if corr is None:
            continue
        out.append({
            "feature": name,
            "gate_corr": round(corr, 4),
            "invert": bool(corr <= threshold),
        })
    out.sort(key=lambda r: r["gate_corr"])
    return out


def select_inverted_features(
    audit: Sequence[Dict[str, Any]],
    *,
    min_corr: Optional[float] = None,
) -> Set[str]:
    """Features to negate so higher values align with WIN on the gate slice."""
    cutoff = _v3_feature_invert_min_corr() if min_corr is None else float(min_corr)
    return {
        str(row["feature"])
        for row in audit
        if row.get("invert") or float(row.get("gate_corr", 0)) <= cutoff
    }


def apply_inversions_to_features(features: Dict[str, Any], invert_cols: Iterable[str]) -> Dict[str, Any]:
    """Negate selected numeric feature columns in place."""
    for col in invert_cols:
        val = features.get(col)
        if isinstance(val, (int, float)):
            features[col] = -float(val)
    return features


def apply_inversions_to_matrix(
    X: np.ndarray,
    feature_cols: Sequence[str],
    invert_cols: Iterable[str],
) -> np.ndarray:
    """Negate selected columns in a feature matrix (copy)."""
    invert = set(invert_cols)
    if not invert:
        return X
    out = np.array(X, copy=True)
    for i, col in enumerate(feature_cols):
        if col in invert:
            out[:, i] = -out[:, i]
    return out


def regime_profile(rows: Sequence[Dict[str, Any]], keys: Sequence[str] = REGIME_KEYS) -> Dict[str, float]:
    """Median regime features from labeled training rows ({features: ...})."""
    profile: Dict[str, List[float]] = {k: [] for k in keys}
    for row in rows:
        feats = row.get("features") if isinstance(row, dict) else None
        if not isinstance(feats, dict):
            continue
        for k in keys:
            val = feats.get(k)
            if isinstance(val, (int, float)):
                profile[k].append(float(val))
    return {
        k: float(np.median(v)) if v else 0.0
        for k, v in profile.items()
    }


def peer_regime_weight(
    target_profile: Dict[str, float],
    peer_features: Dict[str, Any],
    *,
    atr_scale: float = 0.015,
    mom_scale: float = 0.03,
    pct_scale: float = 0.25,
) -> float:
    """Down-weight peer samples whose vol/momentum/%B regime diverges from target."""
    if not _v3_regime_peer_weight_enabled():
        return 1.0
    weight = 1.0
    scales = {
        "atr_pct": atr_scale,
        "mom_4h": mom_scale,
        "pct_b": pct_scale,
    }
    for key, scale in scales.items():
        t_val = float(target_profile.get(key, 0.0))
        p_val = peer_features.get(key)
        if not isinstance(p_val, (int, float)):
            continue
        dist = abs(float(p_val) - t_val) / max(scale, 1e-9)
        weight *= float(np.exp(-dist))
    return max(0.1, min(1.0, weight))


def reliability_bins_monotonic(
    bins: Sequence[Dict[str, Any]],
    *,
    min_bin_n: int = 2,
    tolerance: float = 0.05,
) -> Dict[str, Any]:
    """True when higher predicted-prob buckets have >= observed win rates."""
    usable = [
        b for b in bins
        if int(b.get("n") or 0) >= min_bin_n
        and b.get("mean_pred") is not None
        and b.get("observed_rate") is not None
    ]
    if len(usable) < 2:
        return {"monotonic": None, "reason": "insufficient_bins", "violations": []}
    usable.sort(key=lambda b: float(b["mean_pred"]))
    violations = []
    for prev, nxt in zip(usable, usable[1:]):
        prev_rate = float(prev["observed_rate"])
        next_rate = float(nxt["observed_rate"])
        if next_rate + tolerance < prev_rate:
            violations.append({
                "from_bin": [prev.get("bin_lo"), prev.get("bin_hi")],
                "to_bin": [nxt.get("bin_lo"), nxt.get("bin_hi")],
                "from_rate": prev_rate,
                "to_rate": next_rate,
            })
    return {
        "monotonic": len(violations) == 0,
        "violations": violations,
        "bins_used": len(usable),
    }
