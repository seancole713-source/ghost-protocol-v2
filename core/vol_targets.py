"""Shared volatility targets for UP trades — must match live TP/SL in prediction.py.

Training labels (_simulate_up_tp_sl) and live reconcile (core.tp_sl_resolve) both
derive target/stop from base_vol_pct + stop_pct_from_vol and use calendar
forward bars after entry (Phase 5: tp_sl_fwd_v1). Do not override live vol
from DB without also changing the label generator.

Phase 4: forecast_band_vol_pct widens scorecard OHLC bands from recent realized
range — telemetry only; pick TP/SL still use base_vol_pct.
"""
from __future__ import annotations

import os
import statistics
from typing import Any, Dict, List, Optional, Sequence

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


def _forecast_band_lookback() -> int:
    try:
        return max(3, int(os.getenv("V3_FORECAST_BAND_LOOKBACK", "10")))
    except Exception:
        return 10


def _forecast_band_vol_cap(asset_type: str) -> float:
    try:
        default = 0.08 if (asset_type or "").lower() == "stock" else 0.12
        return max(0.03, float(os.getenv("V3_FORECAST_BAND_VOL_CAP", str(default))))
    except Exception:
        return 0.08


def _forecast_band_realized_scale() -> float:
    """Fraction of median daily (H-L)/close used when widening bands."""
    try:
        return max(0.5, float(os.getenv("V3_FORECAST_BAND_REALIZED_SCALE", "0.85")))
    except Exception:
        return 0.85


def median_realized_range_pct(rows: Sequence[Dict[str, Any]], lookback: int) -> Optional[float]:
    """Median daily true range as fraction of close over the last ``lookback`` bars."""
    if not rows or lookback < 1:
        return None
    window = list(rows)[-lookback:]
    pcts: List[float] = []
    for bar in window:
        close = float(bar.get("close") or 0)
        if close <= 0:
            continue
        hi = float(bar.get("high") or close)
        lo = float(bar.get("low") or close)
        pcts.append(max(0.0, (hi - lo) / close))
    if not pcts:
        return None
    return float(statistics.median(pcts))


def forecast_band_vol_pct(
    symbol: str,
    asset_type: str,
    rows: Optional[Sequence[Dict[str, Any]]] = None,
    *,
    end_idx: Optional[int] = None,
) -> Dict[str, Any]:
    """Vol fraction for daily forecast OHLC bands (scorecard telemetry only).

    Uses max(base_vol_pct, scaled median recent range) capped at
    V3_FORECAST_BAND_VOL_CAP. Does not change training labels or live TP/SL.
    """
    base = base_vol_pct(symbol, asset_type)
    lookback = _forecast_band_lookback()
    cap = _forecast_band_vol_cap(asset_type)
    scale = _forecast_band_realized_scale()
    hist = list(rows or [])
    if end_idx is not None:
        hist = hist[: end_idx + 1]
    realized = median_realized_range_pct(hist, lookback) if len(hist) >= 3 else None
    widened = max(base, float(realized) * scale) if realized is not None else base
    vol = min(widened, cap)
    source = "base"
    if realized is not None and vol > base + 1e-9:
        source = "realized_range"
    elif vol >= cap - 1e-9 and realized is not None and realized * scale > cap:
        source = "realized_range_capped"
    return {
        "vol_pct": round(vol, 4),
        "base_vol_pct": round(base, 4),
        "realized_range_pct": round(realized, 4) if realized is not None else None,
        "lookback_bars": lookback,
        "cap_pct": round(cap, 4),
        "source": source,
    }
