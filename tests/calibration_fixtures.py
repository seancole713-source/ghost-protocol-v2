"""Canonical complete metadata for a model that passes the serve guard."""
from __future__ import annotations

import time
from typing import Any, Dict

import core.signal_engine as signal_engine


def serveable_meta(**overrides: Any) -> Dict[str, Any]:
    meta: Dict[str, Any] = {
        "tier": "proven",
        "direction": "UP",
        "model_sha256": "a" * 64,
        "label_type": signal_engine.LABEL_TYPE,
        "label_schema": signal_engine._v3_label_schema(),
        "feature_schema": signal_engine._v3_feature_schema(),
        "validation_schema": signal_engine._v3_validation_schema(),
        "label_hold_bars": signal_engine.V3_LABEL_HOLD_BARS,
        "trained_at": int(time.time()),
        "accuracy": 0.70,
        "edge": 0.10,
        "wf_acc_mean": 0.68,
        "wf_edge_mean": 0.08,
        "wf_fold_count": 5,
        "calibrated": True,
        "calibration_status": "valid",
        "calibration_schema": "chronological_bakeoff_v1",
        "calibration_method": "sigmoid",
        "calibration_winner": "sigmoid",
        "calibration_n": 30,
        "calibration_fit_n": 18,
        "calibration_purge_n": 2,
        "calibration_selection_n": 10,
        "calibration_refit_n": 30,
        "calibration_candidates": [
            {"method": "raw_identity", "valid": True, "brier": 0.20,
             "log_loss": 0.60, "reliability_gap": 0.10},
            {"method": "sigmoid", "valid": True, "brier": 0.18,
             "log_loss": 0.55, "reliability_gap": 0.08},
        ],
        "gate_n": 20,
        "gate_brier": 0.20,
        "conformal_ok": True,
        "conformal_samples": 10,
        "conformal_q_hat": 0.20,
        "conformal_alpha": 0.10,
    }
    meta.update(overrides)
    return meta
