"""Daily next-session OHLC forecast vs actual — per-symbol scorecard for the cockpit.

Each row is a forecast issued at the prior session close for the next trading
day's open / high / low, compared to realized OHLC once the session completes.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
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


def next_trading_date_after(from_date_str: str) -> str:
    """First US equity session strictly after ``from_date_str`` (YYYY-MM-DD)."""
    try:
        d = date.fromisoformat(str(from_date_str)[:10])
    except ValueError:
        d = datetime.now(timezone.utc).date()
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


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
        pred_close = pc * (1.0 + vol * 0.55)
        bias = "UP"
    else:
        pred_open = pc * (1.0 - _OVERNIGHT_GAP)
        pred_high = pc * (1.0 + stop)
        pred_low = pc * (1.0 - vol)
        pred_close = pc * (1.0 - vol * 0.55)
        bias = "DOWN"
    return {
        "open": round(pred_open, 4),
        "high": round(pred_high, 4),
        "low": round(pred_low, 4),
        "close": round(pred_close, 4),
        "bias": bias,
        "up_prob": round(float(up_prob), 4),
    }


def score_forecast_vs_actual(predicted: Dict[str, float], actual: Dict[str, float]) -> Dict[str, Any]:
    open_pct = _pct_accuracy(predicted.get("open"), actual.get("open"))
    peak_pct = _pct_accuracy(predicted.get("high"), actual.get("high"))
    close_pct = _pct_accuracy(predicted.get("close"), actual.get("close"))
    low_pct = _pct_accuracy(predicted.get("low"), actual.get("low"))
    parts = [p for p in (open_pct, peak_pct, close_pct) if p is not None]
    overall = round(sum(parts) / len(parts), 2) if parts else None
    pred_up = float(predicted.get("close", predicted.get("high", 0))) >= float(predicted.get("open", 0))
    act_up = float(actual.get("close", actual.get("high", 0))) >= float(actual.get("open", 0))
    return {
        "open_pct": open_pct,
        "open_rate": open_pct,
        "peak_pct": peak_pct,
        "peak_rate": peak_pct,
        "close_pct": close_pct,
        "close_rate": close_pct,
        "high_pct": peak_pct,
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
            issued = _parse_bar_date(str(last.get("ts", "")))
            target = next_trading_date_after(issued) if issued else "next session"
            out_days.append({
                "forecast_date": issued,
                "target_date": target,
                "predicted": live_pred,
                "actual": None,
                "score": None,
                "resolved": False,
                "is_next_session": True,
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

    live_row = next((d for d in reversed(out_days) if not d.get("resolved")), None)

    return {
        "ok": True,
        "symbol": sym,
        "has_model": True,
        "days": out_days,
        "summary": summary,
        "generated_at": int(datetime.now(timezone.utc).timestamp()),
        "next_session_forecast": live_row,
    }


def build_watchlist_universe() -> Dict[str, Any]:
    """Full configured watchlist with model coverage flags (all symbols, not just trained)."""
    from config.symbols import watchlist_symbol_pairs
    from core.signal_engine import get_model_status

    st = get_model_status() or {}
    loaded = set((st.get("symbols") or {}).keys())
    pairs = watchlist_symbol_pairs(include_portfolio=True)
    all_syms = [sym for sym, _ in pairs] or ["WOLF"]
    entries = [{"symbol": sym, "has_model": sym in loaded} for sym in all_syms]
    trained = [e["symbol"] for e in entries if e["has_model"]]
    missing = [e["symbol"] for e in entries if not e["has_model"]]
    return {
        "ok": True,
        "watchlist": entries,
        "symbols": all_syms,
        "trained_symbols": trained,
        "missing_symbols": missing,
        "trained_count": len(trained),
        "watchlist_count": len(all_syms),
    }


def build_watchlist_scorecards(days: int = 14) -> Dict[str, Any]:
    """Summary scorecards for the full watchlist; detailed rows only where a model exists."""
    universe = build_watchlist_universe()
    all_syms = universe.get("symbols") or ["WOLF"]
    loaded = set(universe.get("trained_symbols") or [])

    cards = []
    for sym in all_syms:
        if sym not in loaded:
            cards.append({
                "symbol": sym,
                "has_model": False,
                "reason": "no_v3_model",
                "summary": {},
                "latest": None,
            })
            continue
        card = build_daily_scorecard(sym, days=days)
        cards.append({
            "symbol": sym,
            "has_model": card.get("has_model"),
            "reason": card.get("reason"),
            "summary": card.get("summary") or {},
            "latest": (card.get("days") or [])[-1] if card.get("days") else None,
        })

    return {
        "ok": True,
        "symbols": all_syms,
        "trained_symbols": universe.get("trained_symbols"),
        "missing_symbols": universe.get("missing_symbols"),
        "trained_count": universe.get("trained_count"),
        "watchlist_count": universe.get("watchlist_count"),
        "cards": cards,
        "days_requested": days,
    }
