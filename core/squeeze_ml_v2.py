"""Squeeze ML v2 — logistic blend over scorecard features (Phase 1).

Trained coefficients are a calibrated baseline until labeled squeeze outcomes
accrue in production. Blends with heuristic v1 probabilities (60% ML / 40% heuristic).
"""
from __future__ import annotations

import json
import math
import os
from typing import Any, Dict, List, Optional

_MODEL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "data",
    "squeeze_ml_v2.json",
)

# Baseline weights (sigmoid) — proxy-trained from squeeze heuristics + domain priors
_DEFAULT_WEIGHTS = {
    "bias": -1.05,
    "setup_norm": 1.35,
    "trigger_norm": 1.10,
    "confirm_norm": 1.45,
    "rvol_norm": 1.20,
    "move_norm": 0.85,
    "above_vwap": 0.55,
    "short_risk_high": 0.35,
}
_BLEND_ML = 0.60


def _enabled() -> bool:
    return os.getenv("SQUEEZE_ML_V2", "1").strip().lower() in ("1", "true", "yes", "on")


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _load_weights() -> Dict[str, float]:
    try:
        if os.path.isfile(_MODEL_PATH):
            with open(_MODEL_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data.get("weights"), dict):
                return {**_DEFAULT_WEIGHTS, **data["weights"]}
    except Exception:
        pass
    return dict(_DEFAULT_WEIGHTS)


def feature_vector(
    *,
    setup_score: float,
    trigger_score: float,
    confirm_score: float,
    rvol: float,
    peak_move_pct: float,
    above_vwap: Optional[bool],
    short_risk: Optional[str] = None,
) -> List[float]:
    short_high = 1.0 if short_risk in ("high", "extreme") else 0.0
    vwap_f = 1.0 if above_vwap is True else (0.0 if above_vwap is False else 0.5)
    return [
        setup_score / 100.0,
        trigger_score / 100.0,
        confirm_score / 100.0,
        min(1.0, max(0.0, (float(rvol) - 0.5) / 3.0)),
        min(1.0, max(0.0, float(peak_move_pct) / 15.0)),
        vwap_f,
        short_high,
    ]


def predict_continue_3pct(
    *,
    setup_score: float,
    trigger_score: float,
    confirm_score: float,
    rvol: float,
    peak_move_pct: float,
    above_vwap: Optional[bool],
    short_risk: Optional[str] = None,
) -> float:
    """P(+3% continuation) in 0..100."""
    if not _enabled():
        return 0.0
    w = _load_weights()
    f = feature_vector(
        setup_score=setup_score,
        trigger_score=trigger_score,
        confirm_score=confirm_score,
        rvol=rvol,
        peak_move_pct=peak_move_pct,
        above_vwap=above_vwap,
        short_risk=short_risk,
    )
    names = ["setup_norm", "trigger_norm", "confirm_norm", "rvol_norm", "move_norm", "above_vwap", "short_risk_high"]
    z = w["bias"]
    for name, val in zip(names, f):
        z += w.get(name, 0.0) * val
    return round(_sigmoid(z) * 100.0, 1)


def blend_probabilities(
    heuristic: Dict[str, float],
    *,
    setup_score: float,
    trigger_score: float,
    confirm_score: float,
    rvol: float,
    peak_move_pct: float,
    above_vwap: Optional[bool],
    short_risk: Optional[str] = None,
) -> Dict[str, float]:
    """Merge heuristic v1 with ML v2 primary target; scale secondary targets proportionally."""
    if not _enabled():
        return dict(heuristic)
    p_ml = predict_continue_3pct(
        setup_score=setup_score,
        trigger_score=trigger_score,
        confirm_score=confirm_score,
        rvol=rvol,
        peak_move_pct=peak_move_pct,
        above_vwap=above_vwap,
        short_risk=short_risk,
    )
    p_heu = float(heuristic.get("p_continue_3pct_60m") or 0.0)
    p_cont = round(_BLEND_ML * p_ml + (1.0 - _BLEND_ML) * p_heu, 1)
    out = dict(heuristic)
    out["p_continue_3pct_60m"] = p_cont
    out["p_continue_3pct_60m_ml"] = p_ml
    out["p_continue_3pct_60m_heuristic"] = p_heu
    ratio = (p_cont / p_heu) if p_heu > 1 else 1.0
    for key in ("p_vwap_hold", "p_close_above_prior_high", "p_exhaustion_soon"):
        if key in out and out[key]:
            if key == "p_exhaustion_soon":
                out[key] = round(max(5.0, min(92.0, float(out[key]) / max(ratio, 0.5))), 1)
            else:
                out[key] = round(max(5.0, min(92.0, float(out[key]) * ratio)), 1)
    return out


def model_info() -> Dict[str, Any]:
    return {
        "model": "squeeze_ml_v2",
        "blend_ml_weight": _BLEND_ML,
        "enabled": _enabled(),
        "weights_path": _MODEL_PATH,
        "note": "Baseline logistic weights until labeled squeeze outcomes accrue.",
    }
