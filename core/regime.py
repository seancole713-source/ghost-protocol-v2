"""Rule-based market-regime tag (audit §3).

Deliberately simple and fully transparent — no model, no fitting. It classifies
WOLF's current posture from price relative to its 5-day SMA (with a volume
qualifier), and exposes a Ghost Score modifier so the composite score is
downgraded in bearish regimes and modestly boosted in confirmed uptrends.

A BUY-style signal means less in a downtrend than in an uptrend; the modifier
encodes that judgement explicitly rather than burying it in the model.
"""
from typing import Any, Dict, Optional

# Multiplier applied to the raw Ghost Score per regime. Neutral = 1.0.
# Values synced with core/ghost_score_spec.py REGIME_MODIFIER (PR #125 audit).
_REGIME_MODIFIER = {
    "Strong Uptrend": 1.10,
    "Uptrend": 1.05,
    "Choppy": 1.00,
    "Downtrend": 0.95,
    "Strong Downtrend": 0.90,
    "Unknown": 1.00,
}

# Thresholds on (price - sma_5d) / sma_5d. Env-free; product-owner choices.
_STRONG = 0.03   # +/-3%
_TREND = 0.01    # +/-1%
_HIGH_VOLUME = 1.5


def classify_regime(current_price: Optional[float],
                    sma_5d: Optional[float],
                    volume_ratio: Optional[float] = None) -> Dict[str, Any]:
    """Return {label, delta_pct, modifier, basis} from price vs 5-day SMA.

    Within +/-1% of the SMA is Choppy; beyond +/-3% (with above-average volume on
    the upside) is a Strong trend. Missing inputs => Unknown (modifier 1.0)."""
    if current_price is None or sma_5d is None or sma_5d <= 0:
        return {"label": "Unknown", "delta_pct": None,
                "modifier": _REGIME_MODIFIER["Unknown"], "basis": "no price/SMA"}

    delta = (current_price - sma_5d) / sma_5d
    high_vol = volume_ratio is not None and volume_ratio >= _HIGH_VOLUME

    if delta >= _STRONG:
        label = "Strong Uptrend" if high_vol else "Uptrend"
    elif delta >= _TREND:
        label = "Uptrend"
    elif delta > -_TREND:
        label = "Choppy"
    elif delta > -_STRONG:
        label = "Downtrend"
    else:
        label = "Strong Downtrend"

    return {
        "label": label,
        "delta_pct": round(delta * 100, 2),
        "modifier": _REGIME_MODIFIER[label],
        "basis": "price vs 5d SMA" + (" + volume" if high_vol else ""),
    }
