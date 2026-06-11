"""Unified regime classifier — rules + signal-engine labels (Phase 2)."""
from __future__ import annotations

from typing import Any, Dict, Optional

from core.regime import classify_regime


def classify_from_indicators(
    *,
    above_ema200: int,
    adx_trending: int,
    ema_trend_bullish: int,
    adx: float = 25.0,
) -> str:
    """Match signal_engine.predict_live_ex coarse labels."""
    if ema_trend_bullish == 1 and adx_trending == 1 and above_ema200 == 1:
        return "Trend-up"
    if ema_trend_bullish == 0 and above_ema200 == 0:
        return "Trend-down"
    if adx_trending == 0:
        return "Chop"
    return "Neutral"


def unified_regime(
    *,
    price: Optional[float] = None,
    sma_5d: Optional[float] = None,
    volume_ratio: Optional[float] = None,
    above_ema200: Optional[int] = None,
    adx_trending: Optional[int] = None,
    ema_trend_bullish: Optional[int] = None,
    adx: Optional[float] = None,
) -> Dict[str, Any]:
    """Combine price/SMA regime (Ghost Score) with engine gate regime."""
    price_regime = classify_regime(price, sma_5d, volume_ratio)
    engine_label = None
    if above_ema200 is not None and adx_trending is not None and ema_trend_bullish is not None:
        engine_label = classify_from_indicators(
            above_ema200=int(above_ema200),
            adx_trending=int(adx_trending),
            ema_trend_bullish=int(ema_trend_bullish),
            adx=float(adx or 25),
        )
    return {
        "price_regime": price_regime,
        "engine_regime": engine_label,
        "primary_label": engine_label or price_regime.get("label") or "Unknown",
    }
