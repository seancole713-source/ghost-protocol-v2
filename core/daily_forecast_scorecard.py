"""Daily next-session OHLC forecast vs actual — per-symbol scorecard for the cockpit.

Each row is a forecast issued at the prior session close for the next trading
day's open / high / low, compared to realized OHLC once the session completes.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

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


def _scorecard_min_bars() -> int:
    return max(30, int(os.getenv("V3_SCORECARD_MIN_BARS", "30")))


def _scorecard_ohlcv_periods() -> List[str]:
    """Periods to try, longest first — delisted names need history beyond 3mo."""
    out: List[str] = []
    primary = (os.getenv("V3_SCORECARD_OHLCV_PERIOD", "") or "").strip()
    if primary:
        out.append(primary)
    try:
        from core.signal_engine import _v3_ohlcv_period
        default = _v3_ohlcv_period()
    except Exception:
        default = "2y"
    for p in (default, "2y", "1y", "6m", "3mo"):
        if p and p not in out:
            out.append(p)
    return out or ["2y"]


def _fetch_scorecard_rows(symbol: str, asset_type: str) -> Tuple[Optional[List[dict]], Optional[str]]:
    from core.signal_engine import _fetch_ohlcv

    min_bars = _scorecard_min_bars()
    last_rows = None
    last_period = None
    for period in _scorecard_ohlcv_periods():
        rows = _fetch_ohlcv(symbol, asset_type, period=period)
        last_rows, last_period = rows, period
        if rows and len(rows) >= min_bars:
            return rows, period
    return last_rows, last_period


def live_now_quote(symbol: str, asset_type: str = "stock") -> Dict[str, Any]:
    """Intraday live quote + today's O/H/L for the Daily Prediction panel."""
    import time as _time

    sym = (symbol or "WOLF").strip().upper()
    from core.prices import get_extended_session, get_stock_price

    sess = get_extended_session(sym) or {}
    price = sess.get("session_price") or sess.get("live_price")
    if not price:
        price = get_stock_price(sym, asset_type)

    today_open = today_high = today_low = None
    market_date = None
    try:
        import yfinance as yf

        h = yf.Ticker(sym).history(period="1d", interval="5m")
        if h is not None and not h.empty:
            today_open = round(float(h["Open"].iloc[0]), 4)
            today_high = round(float(h["High"].max()), 4)
            today_low = round(float(h["Low"].min()), 4)
            last_bar = round(float(h["Close"].iloc[-1]), 4)
            if not price:
                price = last_bar
            elif abs(float(price) - last_bar) / max(last_bar, 0.01) > 0.02:
                # Prefer freshest bar close when spot feed is stale.
                price = last_bar
    except Exception as exc:
        LOGGER.debug("live_now intraday %s: %s", sym, str(exc)[:80])

    try:
        from zoneinfo import ZoneInfo

        market_date = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        market_date = datetime.now(timezone.utc).date().isoformat()

    prev = sess.get("previous_close")
    price_f = round(float(price), 4) if price else None
    chg_abs = chg_pct = None
    if price_f and prev and float(prev) > 0:
        chg_abs = round(price_f - float(prev), 4)
        chg_pct = round(chg_abs / float(prev) * 100, 3)

    session = sess.get("session") or "closed"
    session_label = {
        "premarket": "Pre-market",
        "rth": "Market open",
        "afterhours": "After hours",
        "closed": "Closed",
    }.get(session, session)

    return {
        "symbol": sym,
        "as_of_ts": int(_time.time()),
        "session": session,
        "session_label": session_label,
        "market_date": market_date,
        "price": price_f,
        "previous_close": prev,
        "change_abs": chg_abs,
        "change_pct": chg_pct,
        "today_open": today_open,
        "today_high": today_high,
        "today_low": today_low,
        "gap_pct": sess.get("gap_pct"),
    }


def _last_bar_age_days(rows: List[dict]) -> Optional[int]:
    if not rows:
        return None
    last_ts = _parse_bar_date(str(rows[-1].get("ts", "")))
    if not last_ts:
        return None
    try:
        last_d = date.fromisoformat(last_ts)
    except ValueError:
        return None
    return (datetime.now(timezone.utc).date() - last_d).days


def build_daily_scorecard(symbol: str, days: int = 14, asset_type: str = "stock") -> Dict[str, Any]:
    """Build forecast-vs-actual rows for the last `days` completed sessions (+ live next)."""
    from core.signal_engine import load_model, _active_feature_cols

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
            "live_now": live_now_quote(sym, asset_type),
        }

    rows, period_used = _fetch_scorecard_rows(sym, asset_type)
    min_bars = _scorecard_min_bars()
    if not rows or len(rows) < min_bars:
        return {
            "ok": True,
            "symbol": sym,
            "has_model": True,
            "reason": "insufficient_bars",
            "ohlcv_period": period_used,
            "bar_count": len(rows) if rows else 0,
            "days": [],
            "summary": {},
        }

    last_bar_age_days = _last_bar_age_days(rows)
    data_stale = last_bar_age_days is not None and last_bar_age_days > 10

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
        "live_now": live_now_quote(sym, asset_type),
        "ohlcv_period": period_used,
        "bar_count": len(rows),
        "last_bar_date": _parse_bar_date(str(rows[-1].get("ts", ""))) if rows else None,
        "last_bar_age_days": last_bar_age_days,
        "data_stale": data_stale,
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
