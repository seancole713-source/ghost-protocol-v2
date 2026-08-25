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
            "no_skill_accuracy": 0.0,
            "gate_brier": None,
            "reliability_bins": [],
            "gate_n": 0,
        }
    proba = model.predict_proba(X_gate)[:, 1]
    natural_rate = float(np.mean(y_gate))
    # The honest no-skill accuracy is the majority-class rate.  Comparing only
    # with positive prevalence overstates edge whenever losses are the majority.
    no_skill_accuracy = max(natural_rate, 1.0 - natural_rate)
    preds = (proba >= 0.5).astype(int)
    holdout_acc = float(accuracy_score(y_gate, preds))
    edge = holdout_acc - no_skill_accuracy
    gate_brier = None
    if np.unique(y_gate).size >= 2:
        gate_brier = round(float(brier_score_loss(y_gate, proba)), 4)
    return {
        "holdout_acc": holdout_acc,
        "edge": edge,
        "natural_rate": natural_rate,
        "no_skill_accuracy": no_skill_accuracy,
        "gate_brier": gate_brier,
        "reliability_bins": _reliability_bins(y_gate, proba),
        "gate_n": int(len(y_gate)),
    }


def _weighted_reliability_gap(y_true, y_prob, n_bins: int = 5) -> float:
    """Expected calibration error using the same fixed reliability buckets."""
    bins = _reliability_bins(y_true, y_prob, n_bins=n_bins)
    total = sum(int(bucket["n"]) for bucket in bins)
    if total <= 0:
        return float("inf")
    return float(sum(
        int(bucket["n"]) * abs(float(bucket["mean_pred"]) - float(bucket["observed_rate"]))
        for bucket in bins
    ) / total)


def _probability_metrics(y_true, y_prob) -> Dict[str, Any]:
    from sklearn.metrics import brier_score_loss, log_loss

    labels = np.asarray(y_true, dtype=int)
    probs = np.clip(np.asarray(y_prob, dtype=float), 1e-12, 1.0 - 1e-12)
    if len(labels) == 0 or not np.all(np.isfinite(probs)):
        raise ValueError("invalid probability evaluation population")
    return {
        "n": int(len(labels)),
        "brier": float(brier_score_loss(labels, probs)),
        "log_loss": float(log_loss(labels, probs, labels=[0, 1])),
        "reliability_gap": _weighted_reliability_gap(labels, probs),
    }


def _fit_prefit_calibrator(model, X, y, method: str):
    """Fit one post-hoc calibrator without refitting the base estimator."""
    from sklearn.calibration import CalibratedClassifierCV
    try:
        import sklearn
        version = tuple(int(x) for x in sklearn.__version__.split(".")[:2])
    except Exception:
        version = (0, 0)
    if version >= (1, 6):
        from sklearn.frozen import FrozenEstimator
        calibrated = CalibratedClassifierCV(FrozenEstimator(model), method=method)
    else:
        calibrated = CalibratedClassifierCV(model, method=method, cv="prefit")
    calibrated.fit(X, y)
    return calibrated


def _pipeline(raw_model, served_model, method: str):
    from core.signal_engine import _ProbabilityPipeline
    return _ProbabilityPipeline(raw_model, served_model, method)


def _calibrator_bakeoff(model, X_calib, y_calib, *, purge: int = 0):
    """Chronological raw/sigmoid/isotonic selection and full-slice refit.

    The outer caller owns the untouched final gate. Within the calibration
    slice, candidates fit on an earlier block and compete on a later selection
    block separated by a label-horizon purge. The winner is then refit once on
    the full *outer-purged* calibration slice; the final gate is never consumed
    here.
    """
    n = int(len(X_calib))
    purge = max(0, int(purge))
    info: Dict[str, Any] = {
        "calibrated": False,
        "calibration_status": "invalid",
        "calibration_schema": "chronological_bakeoff_v1",
        "method": None,
        "n_calib": n,
        "purge_n": purge,
        "candidates": [],
    }
    if not _v3_calibration_enabled():
        info.update({"calibration_status": "disabled", "skip_reason": "disabled"})
        return _pipeline(model, model, "raw_identity"), info
    labels = np.asarray(y_calib, dtype=int)
    # Leave at least five untouched selection observations after the inner
    # purge, and require ten earlier observations to fit stable candidates.
    fit_end = max(10, int(n * 0.60))
    selection_start = fit_end + purge
    if n < 20 + purge or fit_end >= n or n - selection_start < 10:
        info["skip_reason"] = "insufficient_calibration_support"
        return _pipeline(model, model, "raw_identity"), info
    X_fit, y_fit = X_calib[:fit_end], labels[:fit_end]
    X_select, y_select = X_calib[selection_start:], labels[selection_start:]
    info.update({
        "calibration_fit_n": int(len(X_fit)),
        "calibration_selection_n": int(len(X_select)),
        "inner_fit_end_index": int(fit_end - 1),
        "inner_selection_start_index": int(selection_start),
    })
    if np.unique(y_fit).size < 2 or np.unique(labels).size < 2:
        info["skip_reason"] = "single_class_calibration_fit"
        return _pipeline(model, model, "raw_identity"), info

    raw_select = np.asarray(model.predict_proba(X_select)[:, 1], dtype=float)
    raw_metrics = _probability_metrics(y_select, raw_select)
    climatology = np.full(len(y_select), float(np.mean(y_fit)), dtype=float)
    climatology_metrics = _probability_metrics(y_select, climatology)
    candidates = []
    fitted_for_selection: Dict[str, Any] = {"raw_identity": model}
    for priority, method in enumerate(("raw_identity", "sigmoid", "isotonic")):
        try:
            candidate = model if method == "raw_identity" else _fit_prefit_calibrator(
                model, X_fit, y_fit, method,
            )
            probs = np.asarray(candidate.predict_proba(X_select)[:, 1], dtype=float)
            metrics = _probability_metrics(y_select, probs)
            metrics.update({
                "method": method,
                "valid": True,
                "paired_raw_brier_improvement": float(raw_metrics["brier"] - metrics["brier"]),
                "climatology_brier_improvement": float(climatology_metrics["brier"] - metrics["brier"]),
                "priority": priority,
            })
            candidates.append(metrics)
            fitted_for_selection[method] = candidate
        except Exception as exc:
            candidates.append({
                "method": method, "valid": False, "priority": priority,
                "error": str(exc)[:120],
            })
    valid = [candidate for candidate in candidates if candidate.get("valid")]
    info["candidates"] = candidates
    info["raw_selection_metrics"] = raw_metrics
    info["climatology_selection_metrics"] = climatology_metrics
    if not valid:
        info["skip_reason"] = "no_valid_calibration_candidate"
        return _pipeline(model, model, "raw_identity"), info
    winner = min(valid, key=lambda item: (
        item["brier"], item["log_loss"], item["reliability_gap"], item["priority"],
    ))
    method = str(winner["method"])
    selection_model = fitted_for_selection[method]
    selection_probs = np.asarray(selection_model.predict_proba(X_select)[:, 1], dtype=float)
    try:
        from core.conformal_calibration import calibrate_conformal
        conformal = calibrate_conformal(selection_probs, y_select)
    except Exception as exc:
        conformal = {
            "ok": False, "error": str(exc)[:120], "samples": int(len(y_select)),
        }
    try:
        served = model if method == "raw_identity" else _fit_prefit_calibrator(
            model, X_calib, labels, method,
        )
    except Exception as exc:
        info.update({
            "skip_reason": "winner_refit_failed: " + str(exc)[:120],
            "winner": method,
            "conformal": conformal,
        })
        return _pipeline(model, model, "raw_identity"), info
    info.update({
        "calibrated": True,
        "calibration_status": "valid",
        "method": method,
        "winner": method,
        "selection_metrics": {key: value for key, value in winner.items() if key != "priority"},
        "paired_raw_brier_improvement": winner["paired_raw_brier_improvement"],
        "climatology_brier_improvement": winner["climatology_brier_improvement"],
        "conformal": conformal,
        "refit_n": n,
    })
    return _pipeline(model, served, method), info


def _maybe_calibrate(model, X_calib, y_calib):
    """Compatibility facade for the chronological calibrator bakeoff."""
    from core.engine_config import _v3_wf_purge
    try:
        return _calibrator_bakeoff(
            model, X_calib, y_calib, purge=_v3_wf_purge(),
        )
    except Exception as exc:
        info = {
            "calibrated": False,
            "calibration_status": "invalid",
            "calibration_schema": "chronological_bakeoff_v1",
            "method": None,
            "n_calib": int(len(X_calib)),
            "skip_reason": "exception: " + str(exc)[:120],
        }
        LOGGER.error("v3 calibration bakeoff failed closed: %s", str(exc)[:200])
        return _pipeline(model, model, "raw_identity"), info


def _build_ensemble(xgb_model, X_fit, y_fit, sample_weight, X_calib, y_calib):
    """Build a raw soft-voting ensemble, then calibrate it as one pipeline."""
    try:
        from sklearn.ensemble import RandomForestClassifier
        rf = RandomForestClassifier(
            n_estimators=300, max_depth=6, min_samples_leaf=3,
            class_weight="balanced", random_state=42,
        )
        rf.fit(X_fit, y_fit, sample_weight=sample_weight)
        raw_ensemble = _proba_ensemble_cls()([xgb_model, rf])
        final_model, info = _maybe_calibrate(raw_ensemble, X_calib, y_calib)
        info.update({
            "ensemble": True,
            "members": ["xgboost", "random_forest"],
        })
        return final_model, info
    except Exception as exc:
        final_model, info = _maybe_calibrate(xgb_model, X_calib, y_calib)
        info["ensemble"] = False
        info["ensemble_skip_reason"] = "exception: " + str(exc)[:120]
        return final_model, info
