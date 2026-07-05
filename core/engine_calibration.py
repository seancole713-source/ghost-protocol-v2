"""core/engine_calibration.py — probability calibration + ensemble build (split from signal_engine PR #130).

core.signal_engine re-exports these. _ProbaEnsemble itself stays in
core.signal_engine because persisted model pickles reference it by that path.
"""
from typing import Any, Dict, List

import numpy as np

from core.engine_config import _v3_calibration_enabled, _v3_calibration_method

LOGGER = __import__("logging").getLogger("ghost.engine_calibration")


def _proba_ensemble_cls():
    from core.signal_engine import _ProbaEnsemble
    return _ProbaEnsemble

def _reliability_bins(y_true, y_prob, n_bins: int = 5) -> List[Dict[str, Any]]:
    """Reliability diagram bins: predicted prob bucket vs realized win rate."""
    y_true = np.asarray(y_true, dtype=float)
    y_prob = np.asarray(y_prob, dtype=float)
    if len(y_true) == 0:
        return []
    n_bins = max(2, min(int(n_bins), 10))
    bins: List[Dict[str, Any]] = []
    for i in range(n_bins):
        lo = i / n_bins
        hi = (i + 1) / n_bins
        if i == n_bins - 1:
            mask = (y_prob >= lo) & (y_prob <= hi)
        else:
            mask = (y_prob >= lo) & (y_prob < hi)
        cnt = int(np.sum(mask))
        if cnt == 0:
            continue
        bins.append({
            "bin_lo": round(lo, 3),
            "bin_hi": round(hi, 3),
            "n": cnt,
            "mean_pred": round(float(np.mean(y_prob[mask])), 4),
            "observed_rate": round(float(np.mean(y_true[mask])), 4),
        })
    return bins


def _evaluate_calibration_holdout(model, X_gate, y_gate) -> Dict[str, Any]:
    """Evaluate the deployed (calibrated) model on the untouched gate slice."""
    from sklearn.metrics import accuracy_score, brier_score_loss

    y_gate = np.asarray(y_gate)
    if len(y_gate) == 0:
        return {
            "holdout_acc": 0.0,
            "edge": 0.0,
            "natural_rate": 0.0,
            "gate_brier": None,
            "reliability_bins": [],
            "gate_n": 0,
        }
    proba = model.predict_proba(X_gate)[:, 1]
    natural_rate = float(np.mean(y_gate))
    preds = (proba >= 0.5).astype(int)
    holdout_acc = float(accuracy_score(y_gate, preds))
    edge = holdout_acc - natural_rate
    gate_brier = None
    if np.unique(y_gate).size >= 2:
        gate_brier = round(float(brier_score_loss(y_gate, proba)), 4)
    return {
        "holdout_acc": holdout_acc,
        "edge": edge,
        "natural_rate": natural_rate,
        "gate_brier": gate_brier,
        "reliability_bins": _reliability_bins(y_gate, proba),
        "gate_n": int(len(y_gate)),
    }


def _maybe_calibrate(model, X_calib, y_calib):
    """Wrap a fitted base model with prefit probability calibration.

    The base model was fit on the training slice and never saw X_calib, so the
    held-out slice is a valid post-hoc calibration set (strictly time-ordered:
    it is the most recent ~20% of the series). Returns (final_model, info).

    Falls back to the raw model (info["calibrated"]=False) whenever calibration
    isn't viable — disabled, too few points, or a single-class calib slice — so
    training never breaks on this. Calibration quality itself is validated live
    via the confidence-bucket calibration curve, not offline here.
    """
    info = {"calibrated": False, "method": None, "n_calib": int(len(X_calib))}
    if not _v3_calibration_enabled():
        info["skip_reason"] = "disabled"
        return model, info
    if len(X_calib) < 10 or np.unique(y_calib).size < 2:
        info["skip_reason"] = "insufficient_calib_data"
        return model, info
    method = _v3_calibration_method()
    if method == "auto":
        method = "isotonic" if len(X_calib) >= 200 else "sigmoid"
    if method not in ("isotonic", "sigmoid"):
        method = "sigmoid"
    try:
        from sklearn.calibration import CalibratedClassifierCV
        calibrated = CalibratedClassifierCV(model, method=method, cv="prefit")
        calibrated.fit(X_calib, y_calib)
        info.update({"calibrated": True, "method": method})
        return calibrated, info
    except Exception as e:
        info["skip_reason"] = "exception: " + str(e)[:120]
        return model, info


def _build_ensemble(xgb_model, X_fit, y_fit, sample_weight, X_calib, y_calib):
    """Soft-voting blend of the fitted XGB model with a RandomForest (W5).

    Each component is individually probability-calibrated on the WOLF holdout,
    then their probabilities are averaged. Returns (model, calib_info) with the
    same calib_info shape _maybe_calibrate produces, plus ensemble metadata.
    Falls back to the calibrated single XGB model if anything goes wrong, so
    enabling the ensemble can never break a training run.
    """
    try:
        from sklearn.ensemble import RandomForestClassifier
        rf = RandomForestClassifier(
            n_estimators=300, max_depth=6, min_samples_leaf=3,
            class_weight="balanced", random_state=42,
        )
        rf.fit(X_fit, y_fit, sample_weight=sample_weight)
        cal_xgb, info_x = _maybe_calibrate(xgb_model, X_calib, y_calib)
        cal_rf, _info_r = _maybe_calibrate(rf, X_calib, y_calib)
        ens = _proba_ensemble_cls()([cal_xgb, cal_rf])
        info = {
            "calibrated": bool(info_x.get("calibrated", False)),
            "method": info_x.get("method"),
            "n_calib": int(len(X_calib)),
            "ensemble": True,
            "members": ["xgboost", "random_forest"],
        }
        return ens, info
    except Exception as e:
        final_model, info = _maybe_calibrate(xgb_model, X_calib, y_calib)
        info["ensemble"] = False
        info["ensemble_skip_reason"] = "exception: " + str(e)[:120]
        return final_model, info
