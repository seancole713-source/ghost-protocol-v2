"""
core/macro_regime.py — Macro regime features from FRED + yfinance (Pillar 3).

Fetches 8 free macro indicators once per day, caches for 24h.
Appends to every symbol's feature vector so the model knows the macro tide.

Sources:
  - FRED (Federal Reserve Economic Data) — free, no API key for basic series
  - yfinance — VIX, DXY, SPY (already used elsewhere)
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

import numpy as np

LOGGER = logging.getLogger("ghost.macro")

# Cache TTL: 24 hours (macro data doesn't change intraday)
_MACRO_CACHE_TTL = int(os.getenv("MACRO_CACHE_TTL_S", "86400"))
_macro_cache: Dict[str, Any] = {"ts": 0.0, "features": {}}

MACRO_FEATURE_NAMES = [
    "macro_vix_level",         # VIX close (normalized: /40)
    "macro_yield_spread",      # 10Y-2Y spread (normalized: /2)
    "macro_fed_rate",          # Fed funds rate (normalized: /10)
    "macro_dxy_change",        # DXY 20-day return
    "macro_spy_20d_return",    # SPY 20-day return
    "macro_spy_vs_sma50",      # SPY / SMA_50 - 1
    "macro_smh_vs_spy",        # SMH/SPY relative strength 20d
    "macro_vix_regime",        # 0=calm(<20), 1=elevated(20-30), 2=fear(>30)
]


def _fetch_fred_series(series_id: str) -> Optional[float]:
    """Fetch the latest value for a FRED series. Free, no API key."""
    try:
        import requests
        url = f"https://api.stlouisfed.org/fred/series/observations"
        params = {
            "series_id": series_id,
            "api_key": os.getenv("FRED_API_KEY", "none"),  # works without key for basic access
            "file_type": "json",
            "sort_order": "desc",
            "limit": 1,
        }
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 200:
            obs = r.json().get("observations", [])
            if obs:
                return float(obs[0]["value"])
    except Exception as e:
        LOGGER.debug("FRED %s: %s", series_id, str(e)[:80])
    return None


def _fetch_yfinance_series(ticker: str, period: str = "1mo") -> Optional[float]:
    """Fetch latest close for a yfinance ticker."""
    from core.circuit_breaker import _yfinance_cb
    if not _yfinance_cb.allow():
        return None
    try:
        import yfinance as yf
        tk = yf.Ticker(ticker)
        h = tk.history(period=period)
        if not h.empty:
            _yfinance_cb.record_success()
            return float(h["Close"].iloc[-1])
    except Exception:
        _yfinance_cb.record_failure()
    return None


def _fetch_yfinance_return(ticker: str, days: int = 20) -> Optional[float]:
    """Fetch N-day return for a yfinance ticker."""
    from core.circuit_breaker import _yfinance_cb
    if not _yfinance_cb.allow():
        return None
    try:
        import yfinance as yf
        tk = yf.Ticker(ticker)
        h = tk.history(period=f"{days+5}d")
        if len(h) >= days:
            start = float(h["Close"].iloc[-days-1]) if len(h) > days else float(h["Close"].iloc[0])
            end = float(h["Close"].iloc[-1])
            if start > 0:
                _yfinance_cb.record_success()
                return (end - start) / start
    except Exception:
        _yfinance_cb.record_failure()
    return None


def fetch_macro_features() -> Dict[str, float]:
    """Fetch all 8 macro features. Cached for 24h."""
    now = time.time()
    if _macro_cache["ts"] and (now - _macro_cache["ts"]) < _MACRO_CACHE_TTL:
        return dict(_macro_cache["features"])

    features: Dict[str, float] = {}

    # 1. VIX level
    vix = _fetch_yfinance_series("^VIX", "5d")
    features["macro_vix_level"] = round(vix / 40.0, 4) if vix else 0.5

    # 2. Yield spread (10Y-2Y)
    t10 = _fetch_fred_series("DGS10")
    t2 = _fetch_fred_series("DGS2")
    if t10 is not None and t2 is not None:
        features["macro_yield_spread"] = round((t10 - t2) / 2.0, 4)
    else:
        features["macro_yield_spread"] = 0.0

    # 3. Fed funds rate
    fed = _fetch_fred_series("DFF")
    features["macro_fed_rate"] = round(fed / 10.0, 4) if fed else 0.5

    # 4. DXY 20-day change
    dxy_ret = _fetch_yfinance_return("DX-Y.NYB", 20)
    features["macro_dxy_change"] = round(dxy_ret, 4) if dxy_ret is not None else 0.0

    # 5. SPY 20-day return
    spy_ret = _fetch_yfinance_return("SPY", 20)
    features["macro_spy_20d_return"] = round(spy_ret, 4) if spy_ret is not None else 0.0

    # 6. SPY vs SMA_50
    spy_close = _fetch_yfinance_series("SPY", "3mo")
    spy_sma50 = None
    from core.circuit_breaker import _yfinance_cb
    if _yfinance_cb.allow():
        try:
            import yfinance as yf
            h = yf.Ticker("SPY").history(period="3mo")
            if len(h) >= 50:
                spy_sma50 = float(h["Close"].iloc[-50:].mean())
                _yfinance_cb.record_success()
        except Exception:
            _yfinance_cb.record_failure()
    if spy_close and spy_sma50 and spy_sma50 > 0:
        features["macro_spy_vs_sma50"] = round(spy_close / spy_sma50 - 1.0, 4)
    else:
        features["macro_spy_vs_sma50"] = 0.0

    # 7. SMH vs SPY relative strength
    smh_ret = _fetch_yfinance_return("SMH", 20)
    if smh_ret is not None and spy_ret is not None:
        features["macro_smh_vs_spy"] = round(smh_ret - spy_ret, 4)
    else:
        features["macro_smh_vs_spy"] = 0.0

    # 8. VIX regime (categorical)
    vix_val = vix if vix else 20
    if vix_val < 20:
        features["macro_vix_regime"] = 0.0
    elif vix_val < 30:
        features["macro_vix_regime"] = 1.0
    else:
        features["macro_vix_regime"] = 2.0

    _macro_cache["ts"] = now
    _macro_cache["features"] = dict(features)
    LOGGER.info("Macro features fetched: VIX=%.1f spread=%.3f SPY_20d=%.1f%%",
                vix if vix else 0, features["macro_yield_spread"],
                features["macro_spy_20d_return"] * 100)
    return dict(features)


def get_macro_features() -> Dict[str, float]:
    """Public accessor — returns cached or fresh macro features."""
    return fetch_macro_features()
