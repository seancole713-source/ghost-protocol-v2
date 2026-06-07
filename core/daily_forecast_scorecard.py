"""Daily next-session OHLC forecast vs actual — per-symbol scorecard for the cockpit.

Each row is a forecast issued at the prior session close for the next trading
day's open / high / low, compared to realized OHLC once the session completes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.vol_targets import base_vol_pct, stop_pct_from_vol

LOGGER = logging.getLogger("ghost.daily_forecast")

_OVERNIGHT_GAP = 0.001


def _parse_bar_date(ts: str) -> str:
    """Normalize bar timestamp to YYYY-MM-DD."""
    if not ts:
        return ""
    s = str(ts).strip()
    if "T" in s:
        return s.split("T")[0]
    return s[:10]


def _pct_accuracy(predicted: float, actual: float) -> Optional[float]:
    if actual is None or predicted is None or actual == 0:
        return None
    err_pct = abs(float(predicted) - float(actual)) / abs(float(actual)) * 100.0
    return round(max(0.0, 100.0 - err_pct), 2)


def forecast_ohlc_from_prob(prior_close: float, up_prob: float, symbol: str, asset_type: str = "stock") -> Dict[str, Any]:
    """Derive next-day open/high/low band from prior close and model up_prob."""
    vol = base_vol_pct(symbol, asset_type)
    stop = stop_pct_from_vol(vol)
    bullish = float(up_prob) >= 0.5
    pc = float(prior_close)
    if bullish:
        pred_open = pc * (1.0 + _OVERNIGHT_GAP)
        pred_high = pc * (1.0 + vol)
        pred_low = pc * (1.0 - stop)
        bias = "UP"
    else:
        pred_open = pc * (1.0 - _OVERNIGHT_GAP)
        pred_high = pc * (1.0 + stop)
        pred_low = pc * (1.0 - vol)
        bias = "DOWN"
    return {
        "open": round(pred_open, 4),
        "high": round(pred_high, 4),
        "low": round(pred_low, 4),
        "bias": bias,
        "up_prob": round(float(up_prob), 4),
    }


def score_forecast_vs_actual(predicted: Dict[str, float], actual: Dict[str, float]) -> Dict[str, Any]:
    open_pct = _pct_accuracy(predicted.get("open"), actual.get("open"))
    high_pct = _pct_accuracy(predicted.get("high"), actual.get("high"))
    low_pct = _pct_accuracy(predicted.get("low"), actual.get("low"))
    parts = [p for p in (open_pct, high_pct, low_pct) if p is not None]
    overall = round(sum(parts) / len(parts), 2) if parts else None
    pred_up = float(predicted.get("high", 0)) >= float(predicted.get("open", 0))
    act_up = float(actual.get("high", 0)) >= float(actual.get("open", 0))
    return {
        "open_pct": open_pct,
        "high_pct": high_pct,
        "low_pct": low_pct,
        "overall_pct": overall,
        "direction_ok": pred_up == act_up,
    }


def _up_prob_at_bar(rows: List[dict], bar_idx: int, model, feature_cols: List[str]) -> Optional[float]:
    from core.signal_engine import _calculate_features, _backtest_window
    import numpy as np

    window = _backtest_window()
    hist = rows[max(0, bar_idx - window): bar_idx + 1]
    if len(hist) < 30:
        return None
    features = _calculate_features(hist)
    X = np.array([[features.get(c, 0.0) for c in feature_cols]])
    proba = model.predict_proba(X)[0]
    return float(proba[1])


def build_daily_scorecard(symbol: str, days: int = 14, asset_type: str = "stock") -> Dict[str, Any]:
    """Build forecast-vs-actual rows for the last `days` completed sessions (+ live next)."""
    from core.signal_engine import _fetch_ohlcv, load_model, _active_feature_cols

    sym = (symbol or "WOLF").strip().upper()
    days = max(3, min(int(days or 14), 60))
    model, feature_cols, meta = load_model(sym)
    if model is None or not feature_cols:
        return {
            "ok": True,
            "symbol": sym,
            "has_model": False,
            "reason": "no_v3_model",
            "days": [],
            "summary": {},
        }

    rows = _fetch_ohlcv(sym, asset_type, period="3mo")
    if not rows or len(rows) < 35:
        return {
            "ok": True,
            "symbol": sym,
            "has_model": True,
            "reason": "insufficient_bars",
            "days": [],
            "summary": {},
        }

    # Completed sessions only for scoring; last bar may be in-progress.
    completed_end = len(rows) - 1
    start_idx = max(1, completed_end - days)
    out_days: List[Dict[str, Any]] = []

    for i in range(start_idx, completed_end + 1):
        prior = rows[i - 1]
        target = rows[i]
        prior_close = float(prior.get("close") or 0)
        if prior_close <= 0:
            continue
        up_prob = _up_prob_at_bar(rows, i - 1, model, feature_cols)
        if up_prob is None:
            continue
        predicted = forecast_ohlc_from_prob(prior_close, up_prob, sym, asset_type)
        actual = {
            "open": round(float(target.get("open") or 0), 4),
            "high": round(float(target.get("high") or 0), 4),
            "low": round(float(target.get("low") or 0), 4),
            "close": round(float(target.get("close") or 0), 4),
        }
        if actual["open"] <= 0:
            continue
        score = score_forecast_vs_actual(predicted, actual)
        forecast_date = _parse_bar_date(str(prior.get("ts", "")))
        target_date = _parse_bar_date(str(target.get("ts", "")))
        out_days.append({
            "forecast_date": forecast_date,
            "target_date": target_date,
            "predicted": predicted,
            "actual": actual,
            "score": score,
            "resolved": True,
        })

    # Live next-session forecast (no actual yet).
    last = rows[-1]
    last_close = float(last.get("close") or 0)
    if last_close > 0:
        live_prob = _up_prob_at_bar(rows, len(rows) - 1, model, feature_cols)
        if live_prob is not None:
            live_pred = forecast_ohlc_from_prob(last_close, live_prob, sym, asset_type)
            out_days.append({
                "forecast_date": _parse_bar_date(str(last.get("ts", ""))),
                "target_date": "next session",
                "predicted": live_pred,
                "actual": None,
                "score": None,
                "resolved": False,
            })

    scored = [d for d in out_days if d.get("resolved") and d.get("score")]
    overall_vals = [d["score"]["overall_pct"] for d in scored if d["score"].get("overall_pct") is not None]
    dir_hits = sum(1 for d in scored if d["score"].get("direction_ok"))
    summary = {
        "scored_days": len(scored),
        "avg_overall_pct": round(sum(overall_vals) / len(overall_vals), 2) if overall_vals else None,
        "direction_hit_rate_pct": round(dir_hits / len(scored) * 100, 1) if scored else None,
        "model_accuracy_holdout_pct": round(float(meta.get("accuracy", 0)) * 100, 1) if meta else None,
    }

    return {
        "ok": True,
        "symbol": sym,
        "has_model": True,
        "days": out_days,
        "summary": summary,
        "generated_at": int(datetime.now(timezone.utc).timestamp()),
    }


def build_watchlist_scorecards(days: int = 14) -> Dict[str, Any]:
    """Scorecards for every watchlist symbol that has a loadable model."""
    from config.symbols import watchlist_symbol_pairs
    from core.signal_engine import get_model_status

    st = get_model_status() or {}
    loaded = set((st.get("symbols") or {}).keys())
    pairs = watchlist_symbol_pairs(include_portfolio=True)
    symbols = [sym for sym, _ in pairs if sym in loaded]
    if not symbols:
        symbols = ["WOLF"]

    cards = []
    for sym in symbols:
        card = build_daily_scorecard(sym, days=days)
        cards.append({
            "symbol": sym,
            "has_model": card.get("has_model"),
            "summary": card.get("summary") or {},
            "latest": (card.get("days") or [])[-1] if card.get("days") else None,
        })

    return {
        "ok": True,
        "symbols": symbols,
        "cards": cards,
        "days_requested": days,
    }
