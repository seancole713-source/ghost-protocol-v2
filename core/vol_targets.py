"""Shared volatility targets for UP trades — must match live TP/SL in prediction.py."""
from __future__ import annotations

VOL_MAP = {
    "LTC": 0.020,
    "AAVE": 0.040,
    "LINK": 0.031,
    "SOL": 0.029,
    "BTC": 0.022,
    "XRP": 0.015,
    "MATIC": 0.020,
    "SUI": 0.022,
    "NEAR": 0.028,
    "BCH": 0.015,
    "ETH": 0.025,
    "DOT": 0.025,
    "TRX": 0.020,
    "ATOM": 0.025,
    "ARB": 0.030,
    "UNI": 0.030,
}


def base_vol_pct(symbol: str, asset_type: str) -> float:
    """Default target move fraction (e.g. 0.025 = +2.5%). Same defaults as predict_symbol."""
    default = 0.020 if (asset_type or "").lower() == "stock" else 0.025
    return float(VOL_MAP.get((symbol or "").upper(), default))


def stop_pct_from_vol(vol_pct: float) -> float:
    """Stop distance as fraction of entry; live uses vol * 0.65."""
    return float(vol_pct) * 0.65
