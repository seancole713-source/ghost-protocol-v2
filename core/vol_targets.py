"""Shared volatility targets for UP trades — must match live TP/SL in prediction.py.

WOLF-only mode: VOL_MAP retains a single WOLF entry. Anything else uses default.
"""
from __future__ import annotations

VOL_MAP = {
    "WOLF": 0.025,
}


def base_vol_pct(symbol: str, asset_type: str) -> float:
    """Default target move fraction (e.g. 0.025 = +2.5%). Same defaults as predict_symbol."""
    default = 0.020 if (asset_type or "").lower() == "stock" else 0.025
    return float(VOL_MAP.get((symbol or "").upper(), default))


def stop_pct_from_vol(vol_pct: float) -> float:
    """Stop distance as fraction of entry; live uses vol * 0.65."""
    return float(vol_pct) * 0.65
