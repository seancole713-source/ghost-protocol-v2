"""core/momentum.py — trend/breakout detection (PR #151).

Ghost's production engine thinks in short-term mean-reversion terms: "will this
bounce ~2% in ~3 days?" It is blind to the OTHER way to make money — riding a
confirmed multi-week uptrend (the ODD-style +80% climb it never "saw"). This
module is the second way of thinking: detect stocks that are in a real bullish
run — breaking to new highs, above rising moving averages, trending (not
chopping), with volume behind them.

HONESTY: momentum is a real, studied factor, but capturing it profitably in
advance (not hindsight) is hard, and momentum reverses hard. So the consumer
(momentum_shadow brain) is shadow-only and confidence-capped — this MEASURES
whether "buy the confirmed uptrends" works forward before anything trusts it.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from core.quiet import note_suppressed

LOGGER = logging.getLogger("ghost.momentum")

_CACHE: Dict[str, tuple] = {}
_CACHE_TTL_S = 3600  # price-derived; one refresh/hour is plenty
_CACHE_MAX = 2000


def compute_momentum(symbol: str, asset_type: str = "stock") -> Dict[str, Any]:
    """Score a symbol's bullish-run strength from recent price action.

    Returns {"available": False, "reason": ...} on any shortfall — never raises.
    Signals (each contributes to a 0-6 score):
      breakout        price within 1% of / above its 20-day high
      uptrend_struct  SMA20 > SMA50 (rising structure)
      above_sma20     price above the 20-day average
      trending        ADX >= 20 (a real trend, not chop)
      strong_return   20-day return >= +8%
      volume_confirm  recent volume >= 1.2x the 20-day average
    """
    key = symbol.upper()
    now = time.time()
    hit = _CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
    try:
        out = _compute(symbol, asset_type)
    except Exception as exc:
        out = {"available": False, "reason": f"compute failed: {str(exc)[:80]}"}
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = (now + _CACHE_TTL_S, out)
    return out


def _compute(symbol: str, asset_type: str) -> Dict[str, Any]:
    from core.signal_engine import _fetch_ohlcv
    from core.engine_indicators import _adx

    rows = _fetch_ohlcv(symbol, asset_type, period="1y") or []
    if len(rows) < 60:
        return {"available": False, "reason": f"only {len(rows)} bars"}
    closes = [r["close"] for r in rows]
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    vols = [r.get("volume") or 0 for r in rows]
    c = closes[-1]
    if c <= 0:
        return {"available": False, "reason": "no price"}

    sma20 = sum(closes[-20:]) / 20
    sma50 = sum(closes[-50:]) / 50
    hi20 = max(highs[-20:])
    ret20 = (c / closes[-21] - 1) * 100 if len(closes) >= 21 else 0.0
    adx = _adx(highs, lows, closes)
    vol_recent = sum(vols[-5:]) / 5 if any(vols[-5:]) else 0
    vol_base = sum(vols[-20:]) / 20 if any(vols[-20:]) else 0
    vol_ratio = (vol_recent / vol_base) if vol_base else 0.0

    signals = {
        "breakout": c >= hi20 * 0.99,
        "uptrend_struct": sma20 > sma50,
        "above_sma20": c > sma20,
        "trending": adx >= 20.0,
        "strong_return": ret20 >= 8.0,
        "volume_confirm": vol_ratio >= 1.2,
    }
    score = sum(1 for v in signals.values() if v)
    return {
        "available": True,
        "symbol": symbol.upper(),
        "price": round(c, 4),
        "score": score,          # 0-6
        "signals": signals,
        "ret_20d_pct": round(ret20, 1),
        "adx": round(adx, 1),
        "vol_ratio": round(vol_ratio, 2),
        "sma20": round(sma20, 4),
        "sma50": round(sma50, 4),
        "hi_20d": round(hi20, 4),
    }
