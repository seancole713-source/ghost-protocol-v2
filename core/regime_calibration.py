"""Regime-conditional calibration — adjust live floors by issuance regime (Phase 1)."""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

_DEFAULT_ADJ = {
    "Trend-up": -0.012,
    "Strong Uptrend": -0.015,
    "Uptrend": -0.012,
    "Neutral": -0.004,
    "Chop": 0.0,
    "Choppy": 0.0,
    "Trend-down": 0.010,
    "Strong Downtrend": 0.015,
    "Downtrend": 0.010,
}


def regime_calibration_enabled() -> bool:
    return os.getenv("GHOST_REGIME_CALIBRATION", "1").strip().lower() in ("1", "true", "yes", "on")


def sma5_gate_trend_up_bypass() -> bool:
    return os.getenv("REGIME_GATE_SMA5_TREND_UP_BYPASS", "1").strip().lower() in (
        "1", "true", "yes", "on",
    )


def effective_min_win_proba(
    regime_label: Optional[str],
    *,
    base: float,
) -> float:
    """Raise the prob floor in downtrends; never below a named contract's floor.

    Audit U51: the uptrend discounts (-0.012/-0.015) and the 0.58 cap used to
    take min_win_proba BELOW the accuracy contract (0.55 -> 0.538 in Trend-up;
    0.60 -> 0.58 under contract 80), bypassing resolve_float's no-weakening
    rule. On the named contracts (55/70/80) the regime may only tighten the
    floor. The pre-audit "legacy" escape hatch keeps the old behaviour.
    """
    if not regime_calibration_enabled():
        return base
    adj = _DEFAULT_ADJ.get(regime_label or "", 0.0)
    floor = float(os.getenv("V3_MIN_WIN_PROBA_FLOOR", "0.50"))
    cap = float(os.getenv("V3_MIN_WIN_PROBA_CAP", "0.58"))
    effective = max(floor, min(cap, base + adj))
    try:
        from core.accuracy_contract import _NO_WEAKENING_CONTRACTS, contract_name
        if contract_name() in _NO_WEAKENING_CONTRACTS:
            effective = max(effective, float(base))
    except Exception:
        effective = max(effective, float(base))  # fail closed: never loosen
    return round(effective, 4)


def regime_calibration_meta(regime_label: Optional[str], base: float) -> Dict[str, Any]:
    effective = effective_min_win_proba(regime_label, base=base)
    return {
        "enabled": regime_calibration_enabled(),
        "regime_label": regime_label,
        "base_min_win_proba": base,
        "adjustment": round(effective - base, 4),
        "effective_min_win_proba": effective,
    }
