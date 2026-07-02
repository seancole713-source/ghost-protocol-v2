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
        fred_key = os.getenv("FRED_API_KEY", "").strip()
        if not fred_key:
            LOGGER.debug("FRED_API_KEY not set — yield spread / fed rate will be unavailable")
            return None
        url = f"https://api.stlouisfed.org/fred/series/observations"
        params = {
            "series_id": series_id,
            "api_key": fred_key,
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


# ── Phase 1: historical macro features for point-in-time training ──

_HISTORICAL_MACRO_CACHE: Dict[str, Any] = {"ts": 0.0, "series": {}}


def _fetch_yfinance_daily_history(ticker: str, period: str = "2y") -> Optional[Dict[str, float]]:
    """Fetch daily close history for a yfinance ticker. Returns {date_str: close}."""
    from core.circuit_breaker import _yfinance_cb
    if not _yfinance_cb.allow():
        return None
    try:
        import yfinance as yf
        h = yf.Ticker(ticker).history(period=period)
        if h.empty:
            return None
        _yfinance_cb.record_success()
        out: Dict[str, float] = {}
        for idx, row in h.iterrows():
            date_str = str(idx.date())
            out[date_str] = float(row["Close"])
        return out
    except Exception:
        _yfinance_cb.record_failure()
        return None


def _fetch_fred_time_series(series_id: str) -> Optional[Dict[str, float]]:
    """Fetch full FRED time series. Returns {date_str: value}."""
    try:
        import requests
        fred_key = os.getenv("FRED_API_KEY", "").strip()
        if not fred_key:
            return None
        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {
            "series_id": series_id,
            "api_key": fred_key,
            "file_type": "json",
            "sort_order": "asc",
            "limit": 5000,
        }
        r = requests.get(url, params=params, timeout=30)
        if r.status_code != 200:
            return None
        obs = r.json().get("observations", [])
        out: Dict[str, float] = {}
        for o in obs:
            val = o.get("value")
            if val and val != ".":
                out[o["date"]] = float(val)
        return out
    except Exception as e:
        LOGGER.debug("FRED time series %s: %s", series_id, str(e)[:80])
        return None


def _build_historical_macro_series() -> Dict[str, Dict[str, float]]:
    """Build per-date macro feature dicts for the last 2 years.

    Returns {date_str: {macro_feature_name: value}}.
    Cached for 24h — macro history doesn't change intraday.
    """
    now = time.time()
    if _HISTORICAL_MACRO_CACHE["ts"] and (now - _HISTORICAL_MACRO_CACHE["ts"]) < _MACRO_CACHE_TTL:
        return dict(_HISTORICAL_MACRO_CACHE["series"])

    series: Dict[str, Dict[str, float]] = {}

    # Fetch daily histories
    vix_hist = _fetch_yfinance_daily_history("^VIX", "2y") or {}
    spy_hist = _fetch_yfinance_daily_history("SPY", "2y") or {}
    dxy_hist = _fetch_yfinance_daily_history("DX-Y.NYB", "2y") or {}
    smh_hist = _fetch_yfinance_daily_history("SMH", "2y") or {}
    t10_hist = _fetch_fred_time_series("DGS10") or {}
    t2_hist = _fetch_fred_time_series("DGS2") or {}
    fed_hist = _fetch_fred_time_series("DFF") or {}

    # Collect all dates
    all_dates = sorted(set(
        list(vix_hist.keys()) + list(spy_hist.keys()) +
        list(dxy_hist.keys()) + list(smh_hist.keys())
    ))

    # Build per-date features
    for i, date_str in enumerate(all_dates):
        feats: Dict[str, float] = {}

        # VIX
        vix = vix_hist.get(date_str)
        feats["macro_vix_level"] = round(vix / 40.0, 4) if vix else 0.5

        # Yield spread
        t10 = t10_hist.get(date_str)
        t2 = t2_hist.get(date_str)
        if t10 is not None and t2 is not None:
            feats["macro_yield_spread"] = round((t10 - t2) / 2.0, 4)
        else:
            feats["macro_yield_spread"] = 0.0

        # Fed rate
        fed = fed_hist.get(date_str)
        feats["macro_fed_rate"] = round(fed / 10.0, 4) if fed else 0.5

        # DXY 20-day return
        if i >= 20:
            dxy_start = dxy_hist.get(all_dates[i - 20])
            dxy_end = dxy_hist.get(date_str)
            if dxy_start and dxy_end and dxy_start > 0:
                feats["macro_dxy_change"] = round((dxy_end - dxy_start) / dxy_start, 4)
            else:
                feats["macro_dxy_change"] = 0.0
        else:
            feats["macro_dxy_change"] = 0.0

        # SPY 20-day return
        if i >= 20:
            spy_start = spy_hist.get(all_dates[i - 20])
            spy_end = spy_hist.get(date_str)
            if spy_start and spy_end and spy_start > 0:
                feats["macro_spy_20d_return"] = round((spy_end - spy_start) / spy_start, 4)
            else:
                feats["macro_spy_20d_return"] = 0.0
        else:
            feats["macro_spy_20d_return"] = 0.0

        # SPY vs SMA50
        if i >= 50:
            spy_window = [spy_hist.get(all_dates[j]) for j in range(i - 49, i + 1)]
            spy_window = [v for v in spy_window if v is not None]
            if len(spy_window) >= 40:
                sma50 = sum(spy_window) / len(spy_window)
                spy_close = spy_hist.get(date_str)
                if spy_close and sma50 > 0:
                    feats["macro_spy_vs_sma50"] = round(spy_close / sma50 - 1.0, 4)
                else:
                    feats["macro_spy_vs_sma50"] = 0.0
            else:
                feats["macro_spy_vs_sma50"] = 0.0
        else:
            feats["macro_spy_vs_sma50"] = 0.0

        # SMH vs SPY
        if i >= 20:
            smh_start = smh_hist.get(all_dates[i - 20])
            smh_end = smh_hist.get(date_str)
            spy_start2 = spy_hist.get(all_dates[i - 20])
            spy_end2 = spy_hist.get(date_str)
            if all(v is not None and v > 0 for v in [smh_start, smh_end, spy_start2, spy_end2]):
                smh_ret = (smh_end - smh_start) / smh_start
                spy_ret = (spy_end2 - spy_start2) / spy_start2
                feats["macro_smh_vs_spy"] = round(smh_ret - spy_ret, 4)
            else:
                feats["macro_smh_vs_spy"] = 0.0
        else:
            feats["macro_smh_vs_spy"] = 0.0

        # VIX regime
        vix_val = vix if vix else 20
        if vix_val < 20:
            feats["macro_vix_regime"] = 0.0
        elif vix_val < 30:
            feats["macro_vix_regime"] = 1.0
        else:
            feats["macro_vix_regime"] = 2.0

        series[date_str] = feats

    _HISTORICAL_MACRO_CACHE["ts"] = now
    _HISTORICAL_MACRO_CACHE["series"] = dict(series)
    LOGGER.info("Historical macro series built: %d dates", len(series))
    return dict(series)


def get_macro_features_for_date(date_str: str) -> Dict[str, float]:
    """Point-in-time macro features for a historical training bar date.

    Returns a dict of 8 macro feature values, or zeros if unavailable.
    Phase 1 (PR #116): replaces the training-as-zeros behavior so the model
    learns regime-conditional patterns instead of seeing constants.
    """
    series = _build_historical_macro_series()
    return dict(series.get(date_str, {}))
