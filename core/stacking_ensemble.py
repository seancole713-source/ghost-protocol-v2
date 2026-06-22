"""
core/stacking_ensemble.py — Meta-stacking ensemble (Pillar 1).

3 base models (XGBoost, HistGradientBoosting, RandomForest), each independently
probability-calibrated on the holdout slice. A LogisticRegression meta-model
trained on out-of-fold base predictions learns which model to trust when.

All dependencies are in scikit-learn + xgboost — no C++ compilation needed.
Activation: V3_ENSEMBLE=stacking (default "off" preserves single XGBoost).
"""
from __future__ import annotations

import logging
import os
import pickle
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

LOGGER = logging.getLogger("ghost.ensemble")

# ── Ensemble mode ──────────────────────────────────────────────────────
def _ensemble_mode() -> str:
    return (os.getenv("V3_ENSEMBLE", "off") or "off").strip().lower()


def is_stacking_enabled() -> bool:
    return _ensemble_mode() == "stacking"


def is_soft_voting_enabled() -> bool:
    return _ensemble_mode() in ("on", "soft_voting")


# ── Base model builders ────────────────────────────────────────────────

def _build_xgb(params: dict) -> Any:
    from xgboost import XGBClassifier
    return XGBClassifier(
        n_estimators=params.get("n_estimators", 200),
        max_depth=params.get("max_depth", 4),
        learning_rate=params.get("learning_rate", 0.03),
        subsample=params.get("subsample", 0.8),
        colsample_bytree=params.get("colsample_bytree", 0.7),
        min_child_weight=params.get("min_child_weight", 3),
        scale_pos_weight=params.get("scale_pos_weight", 1.0),
        eval_metric="logloss",
        random_state=42,
        verbosity=0,
    )


def _build_hgb(params: dict) -> Any:
    """HistGradientBoostingClassifier — scikit-learn, no C++ compilation needed."""
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(
        max_iter=params.get("n_estimators", 200),
        max_depth=params.get("max_depth", 5),
        learning_rate=params.get("learning_rate", 0.03),
        max_leaf_nodes=params.get("max_leaf_nodes", 31),
        min_samples_leaf=params.get("min_samples_leaf", 20),
        class_weight=params.get("class_weight"),
        random_state=43,
        early_stopping=False,
    )


def _build_rf(params: dict) -> Any:
    from sklearn.ensemble import RandomForestClassifier
    return RandomForestClassifier(
        n_estimators=params.get("n_estimators", 200),
        max_depth=params.get("max_depth", 5),
        min_samples_leaf=params.get("min_samples_leaf", 5),
        class_weight=params.get("class_weight"),
        random_state=45,
        n_jobs=-1,
    )


_BASE_BUILDERS = {
    "xgb": _build_xgb,
    "hgb": _build_hgb,
    "rf": _build_rf,
}


# ── Calibration wrapper ────────────────────────────────────────────────

def _calibrate_model(model, X_calib, y_calib, method: str = "sigmoid") -> Any:
    """Wrap a fitted classifier with probability calibration."""
    from sklearn.calibration import CalibratedClassifierCV
    cal = CalibratedClassifierCV(
        estimator=model,
        method=method,
        cv="prefit",
    )
    cal.fit(X_calib, y_calib)
    return cal


# ── Stacking ensemble ──────────────────────────────────────────────────

class StackingEnsemble:
    """3-model stack with LogisticRegression meta-model.

    Pickleable — persists exactly like a bare model. Exposes the sklearn
    classifier surface (classes_, predict_proba, predict).
    """

    def __init__(self, base_models: list, meta_model: Any, feature_cols: list):
        self.base_models = base_models
        self.meta_model = meta_model
        self.feature_cols = list(feature_cols)
        self.classes_ = np.array([0, 1])

    def predict_proba(self, X):
        # Each base model emits P(WIN)
        base_probas = []
        for m in self.base_models:
            p = m.predict_proba(X)
            if p.shape[1] >= 2:
                base_probas.append(p[:, 1])
            else:
                base_probas.append(p[:, 0])
        stacked = np.column_stack(base_probas)
        return self.meta_model.predict_proba(stacked)

    def predict(self, X):
        return self.classes_[np.argmax(self.predict_proba(X), axis=1)]


def build_stacking_ensemble(
    X_train, y_train,
    X_calib, y_calib,
    sample_weight=None,
    feature_cols=None,
) -> Tuple[Any, Dict[str, Any]]:
    """Build and calibrate 3 base models, then train LR meta-model.

    Returns (ensemble, meta_dict) where ensemble is a StackingEnsemble
    ready for pickle persistence.
    """
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold

    n = len(X_train)
    if n < 40:
        LOGGER.warning("Stacking: only %s samples, need ≥40 — falling back to single XGB", n)
        return None, {"ensemble": False, "skip_reason": f"insufficient_samples({n})"}

    pos_ct = int(np.sum(y_train))
    neg_ct = n - pos_ct
    scale = min(25.0, max(1.0, float(neg_ct / max(pos_ct, 1))))

    params = {
        "n_estimators": 200,
        "max_depth": 4,
        "learning_rate": 0.03,
        "subsample": 0.8,
        "colsample_bytree": 0.7,
        "min_child_weight": 3,
        "min_samples_leaf": 20,
        "max_leaf_nodes": 31,
        "scale_pos_weight": scale,
    }

    # Build and calibrate each base model
    base_models = []
    base_names = []
    for name, builder in _BASE_BUILDERS.items():
        try:
            model = builder(params)
            # HistGradientBoostingClassifier doesn't support sample_weight in fit
            if sample_weight is not None and name == "xgb":
                model.fit(X_train, y_train, sample_weight=sample_weight)
            else:
                model.fit(X_train, y_train)
            # Calibrate on holdout slice
            if len(X_calib) >= 10:
                cal = _calibrate_model(model, X_calib, y_calib)
                base_models.append(cal)
            else:
                base_models.append(model)
            base_names.append(name)
            LOGGER.info("Stacking: %s base model fitted + calibrated", name)
        except Exception as e:
            LOGGER.warning("Stacking: %s base model failed: %s", name, str(e)[:80])

    if len(base_models) < 2:
        return None, {"ensemble": False, "skip_reason": f"only_{len(base_models)}_base_models"}

    # Generate out-of-fold meta-features via 3-fold CV on training set
    kf = StratifiedKFold(n_splits=min(3, max(2, n // 20)), shuffle=True, random_state=42)
    meta_X = np.zeros((n, len(base_models)))
    for train_idx, val_idx in kf.split(X_train, y_train):
        X_tr, y_tr = X_train[train_idx], y_train[train_idx]
        X_val = X_train[val_idx]
        for j, name in enumerate(base_names):
            builder = _BASE_BUILDERS[name]
            fold_model = builder(params)
            if sample_weight is not None and name == "xgb":
                fold_model.fit(X_tr, y_tr, sample_weight=sample_weight[train_idx])
            else:
                fold_model.fit(X_tr, y_tr)
            p = fold_model.predict_proba(X_val)
            meta_X[val_idx, j] = p[:, 1] if p.shape[1] >= 2 else p[:, 0]

    # Train meta-model
    meta = LogisticRegression(
        C=1.0,
        class_weight="balanced",
        max_iter=1000,
        random_state=46,
    )
    meta.fit(meta_X, y_train)

    ensemble = StackingEnsemble(base_models, meta, feature_cols or [])
    info = {
        "ensemble": True,
        "ensemble_mode": "stacking",
        "members": base_names,
        "meta_model": "LogisticRegression",
        "base_count": len(base_models),
    }
    LOGGER.info("Stacking ensemble built: %s base models → LR meta", len(base_models))
    return ensemble, info
