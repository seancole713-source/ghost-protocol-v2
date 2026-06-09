"""Daily next-session OHLC forecast vs actual — per-symbol scorecard for the cockpit.

Each row is a forecast issued at the prior session close for the next trading
day's open / high / low, compared to realized OHLC once the session completes.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.vol_targets import base_vol_pct, forecast_band_vol_pct, stop_pct_from_vol

LOGGER = logging.getLogger("ghost.daily_forecast")

_OVERNIGHT_GAP = 0.001


def _parse_bar_date(ts: str) -> str:
    """Normalize bar timestamp to YYYY-MM-DD in US/Eastern (session date)."""
    if not ts:
        return ""
    s = str(ts).strip()
    try:
        from zoneinfo import ZoneInfo

        if "T" in s:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        pass
    if "T" in s:
        return s.split("T")[0]
    return s[:10]


def _previous_trading_day(d: date) -> date:
    cur = d - timedelta(days=1)
    while cur.weekday() >= 5:
        cur -= timedelta(days=1)
    return cur


def _next_trading_day(d: date) -> date:
    cur = d + timedelta(days=1)
    while cur.weekday() >= 5:
        cur += timedelta(days=1)
    return cur


def panel_session_dates(now_et: Optional[datetime] = None) -> Dict[str, Any]:
    """Canonical ET calendar dates for the Daily Prediction panel rows."""
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("America/New_York")
    except Exception:
        tz = timezone.utc
    now = now_et or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)
    today = now.date()
    hm = now.hour * 60 + now.minute
    rth_open = 9 * 60 + 30
    rth_close = 16 * 60

    if today.weekday() >= 5:
        last_completed = _previous_trading_day(today)
        next_session = _next_trading_day(today)
        return {
            "live_date": last_completed.isoformat(),
            "live_label": "last session",
            "predict_date": next_session.isoformat(),
            "market_date": last_completed.isoformat(),
        }
    if hm >= rth_close:
        last_completed = today
        next_session = _next_trading_day(today)
        return {
            "live_date": today.isoformat(),
            "live_label": "today",
            "predict_date": next_session.isoformat(),
            "market_date": last_completed.isoformat(),
        }
    if hm >= rth_open:
        last_completed = _previous_trading_day(today)
        next_session = _next_trading_day(today)
        return {
            "live_date": today.isoformat(),
            "live_label": "today",
            "predict_date": next_session.isoformat(),
            "market_date": last_completed.isoformat(),
        }
    # Pre-market weekday: forecast targets today's open; last full session = prior day.
    last_completed = _previous_trading_day(today)
    return {
        "live_date": today.isoformat(),
        "live_label": "premarket",
        "predict_date": today.isoformat(),
        "market_date": last_completed.isoformat(),
    }


def _bar_for_date(rows: List[dict], target: str) -> Optional[dict]:
    want = str(target)[:10]
    for bar in reversed(rows or []):
        if _parse_bar_date(str(bar.get("ts", ""))) == want:
            return bar
    return None


def _bar_index_for_date(rows: List[dict], target: str) -> Optional[int]:
    want = str(target)[:10]
    for i, bar in enumerate(rows or []):
        if _parse_bar_date(str(bar.get("ts", ""))) == want:
            return i
    return None


def _actual_from_bar(bar: Optional[dict]) -> Optional[Dict[str, float]]:
    if not bar:
        return None
    o = float(bar.get("open") or 0)
    h = float(bar.get("high") or 0)
    l = float(bar.get("low") or 0)
    c = float(bar.get("close") or 0)
    if o <= 0:
        return None
    return {
        "open": round(o, 4),
        "high": round(h, 4),
        "low": round(l, 4),
        "close": round(c, 4),
    }


def _actual_from_live(live: Dict[str, Any], *, use_rth_close: bool = False) -> Optional[Dict[str, float]]:
    o = live.get("today_open")
    h = live.get("today_high")
    l = live.get("today_low")
    if use_rth_close:
        c = live.get("rth_close") or live.get("price")
    else:
        c = live.get("price")
    if o is None or float(o) <= 0:
        return None
    return {
        "open": round(float(o), 4),
        "high": round(float(h or o), 4),
        "low": round(float(l or o), 4),
        "close": round(float(c or o), 4),
    }


def _after_rth_close(now_et: Optional[datetime], live_intraday: Optional[Dict[str, Any]]) -> bool:
    if live_intraday and live_intraday.get("session") == "afterhours":
        return True
    if now_et is None:
        return False
    return now_et.hour * 60 + now_et.minute >= 16 * 60


def build_prediction_panel(
    symbol: str,
    rows: List[dict],
    out_days: List[Dict[str, Any]],
    model,
    feature_cols: List[str],
    invert_cols: List[str],
    asset_type: str,
    live_intraday: Optional[Dict[str, Any]] = None,
    *,
    now_et: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Align Live / Predict / Market rows to the ET trading calendar."""
    sym = (symbol or "WOLF").strip().upper()
    dates = panel_session_dates(now_et)
    predict_date = dates["predict_date"]
    market_date = dates["market_date"]
    live_date = dates["live_date"]
    try:
        from zoneinfo import ZoneInfo
        now = now_et or datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now = now_et
    after_close = _after_rth_close(now, live_intraday)

    market_actual = None
    if after_close and live_intraday and live_date == market_date:
        market_actual = _actual_from_live(live_intraday, use_rth_close=True)
    if market_actual is None:
        market_actual = _actual_from_bar(_bar_for_date(rows, market_date))
    if market_actual is None and live_intraday and live_date == market_date:
        market_actual = _actual_from_live(live_intraday, use_rth_close=after_close)

    prior_date = _previous_trading_day(date.fromisoformat(predict_date[:10]))
    prior_idx = _bar_index_for_date(rows, prior_date.isoformat())
    prior_bar = rows[prior_idx] if prior_idx is not None else None
    if prior_bar is None:
        for bar in reversed(rows or []):
            bd = _parse_bar_date(str(bar.get("ts", "")))
            if bd and bd < predict_date[:10]:
                prior_bar = bar
                prior_idx = _bar_index_for_date(rows, bd)
                break

    predicted = None
    if prior_bar is not None and prior_idx is not None:
        prior_close = float(prior_bar.get("close") or 0)
        if prior_close > 0:
            up_prob = _up_prob_at_bar(rows, prior_idx, model, feature_cols, invert_cols)
            if up_prob is not None:
                band_vol = forecast_band_vol_pct(sym, asset_type, rows, end_idx=prior_idx)
                predicted = forecast_ohlc_from_prob(
                    prior_close, up_prob, sym, asset_type, band_vol=band_vol,
                )

    market_score = None
    market_forecast = None
    scored = next(
        (d for d in reversed(out_days or []) if d.get("resolved") and d.get("target_date") == market_date),
        None,
    )
    if scored and scored.get("predicted"):
        market_forecast = scored["predicted"]
        market_score = scored.get("score")
    else:
        issue_date = _previous_trading_day(date.fromisoformat(market_date[:10]))
        issue_idx = _bar_index_for_date(rows, issue_date.isoformat())
        if issue_idx is not None:
            issue_close = float(rows[issue_idx].get("close") or 0)
            if issue_close > 0:
                up_prob = _up_prob_at_bar(rows, issue_idx, model, feature_cols, invert_cols)
                if up_prob is not None:
                    band_vol = forecast_band_vol_pct(sym, asset_type, rows, end_idx=issue_idx)
                    market_forecast = forecast_ohlc_from_prob(
                        issue_close, up_prob, sym, asset_type, band_vol=band_vol,
                    )
        if market_forecast and market_actual:
            market_score = score_forecast_vs_actual(market_forecast, market_actual)

    live = dict(live_intraday or {})
    live["panel_date"] = live_date
    if after_close and live.get("session") == "afterhours":
        live["panel_label"] = "after hours"
    else:
        live["panel_label"] = dates["live_label"]
    live["market_date"] = live_date

    if dates["live_label"] in ("last session", "premarket") and market_date == live_date:
        if market_actual:
            live["today_open"] = market_actual["open"]
            live["today_high"] = market_actual["high"]
            live["today_low"] = market_actual["low"]
            if live.get("price") is None:
                live["price"] = market_actual["close"]
    elif dates["live_label"] == "last session" and market_actual:
        live["today_open"] = market_actual["open"]
        live["today_high"] = market_actual["high"]
        live["today_low"] = market_actual["low"]
        if live.get("price") is None:
            live["price"] = market_actual["close"]

    return {
        "predict_date": predict_date,
        "market_date": market_date,
        "live_date": live_date,
        "predict": {
            "target_date": predict_date,
            "forecast_date": prior_date.isoformat(),
            "predicted": predicted,
            "resolved": False,
        },
        "market": {
            "target_date": market_date,
            "actual": market_actual,
            "score": market_score,
            "resolved": bool(market_actual),
        },
        "live": live,
    }


def refresh_scorecard_live_panel(payload: Dict[str, Any], asset_type: str = "stock") -> Dict[str, Any]:
    """Recompute live quote + aligned panel rows (never serve stale calendar dates)."""
    if not payload.get("has_model"):
        payload["live_now"] = live_now_quote(payload.get("symbol") or "WOLF", asset_type)
        return payload
    from core.signal_engine import load_model

    sym = (payload.get("symbol") or "WOLF").strip().upper()
    model, feature_cols, meta = load_model(sym)
    invert_cols = (meta or {}).get("feature_inversions") or []
    if model is None or not feature_cols:
        payload["live_now"] = live_now_quote(sym, asset_type)
        return payload
    rows, _ = _fetch_scorecard_rows(sym, asset_type)
    live_quote = live_now_quote(sym, asset_type)
    payload["live_now"] = live_quote
    if rows:
        payload["panel"] = build_prediction_panel(
            sym,
            rows,
            payload.get("days") or [],
            model,
            feature_cols,
            invert_cols,
            asset_type,
            live_quote,
        )
    return payload


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


def forecast_ohlc_from_prob(
    prior_close: float,
    up_prob: float,
    symbol: str,
    asset_type: str = "stock",
    *,
    band_vol: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Derive next-day open/high/low band from prior close and model up_prob.

    Telemetry only — trade direction for picks uses classifier up_prob via
    predict_live_ex, not the sign of these level bands.

    When ``band_vol`` is supplied (from forecast_band_vol_pct), high/low use
    realized-range-aware width for meme/high-ATR names; pick TP/SL unchanged.
    """
    vol_info = band_vol or {"vol_pct": base_vol_pct(symbol, asset_type), "source": "base"}
    vol = float(vol_info.get("vol_pct") or base_vol_pct(symbol, asset_type))
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
        "band_vol_pct": round(vol, 4),
        "band_vol_source": vol_info.get("source"),
        "band_realized_range_pct": vol_info.get("realized_range_pct"),
    }


def score_forecast_vs_actual(predicted: Dict[str, float], actual: Dict[str, float]) -> Dict[str, Any]:
    open_pct = _pct_accuracy(predicted.get("open"), actual.get("open"))
    peak_pct = _pct_accuracy(predicted.get("high"), actual.get("high"))
    close_pct = _pct_accuracy(predicted.get("close"), actual.get("close"))
    low_pct = _pct_accuracy(predicted.get("low"), actual.get("low"))
    parts = [p for p in (open_pct, peak_pct, low_pct, close_pct) if p is not None]
    overall = round(sum(parts) / len(parts), 2) if parts else None
    act_up = float(actual.get("close", actual.get("high", 0))) >= float(actual.get("open", 0))
    up_prob = predicted.get("up_prob")
    if up_prob is not None:
        pred_up = float(up_prob) >= 0.5
        direction_source = "classifier_up_prob"
    else:
        pred_up = float(predicted.get("close", predicted.get("high", 0))) >= float(predicted.get("open", 0))
        direction_source = "ohlc_band_legacy"
    return {
        "open_pct": open_pct,
        "open_rate": open_pct,
        "peak_pct": peak_pct,
        "peak_rate": peak_pct,
        "close_pct": close_pct,
        "close_rate": close_pct,
        "high_pct": peak_pct,
        "low_pct": low_pct,
        "low_rate": low_pct,
        "overall_pct": overall,
        "direction_ok": pred_up == act_up,
        "direction_source": direction_source,
    }


def _up_prob_at_bar(
    rows: List[dict],
    bar_idx: int,
    model,
    feature_cols: List[str],
    invert_cols: Optional[List[str]] = None,
) -> Optional[float]:
    from core.signal_engine import _calculate_features, _backtest_window
    from core.feature_audit import apply_inversions_to_features
    import numpy as np

    window = _backtest_window()
    hist = rows[max(0, bar_idx - window): bar_idx + 1]
    if len(hist) < 30:
        return None
    features = _calculate_features(hist)
    if invert_cols:
        apply_inversions_to_features(features, invert_cols)
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
    from core.prices import get_intraday_session

    sym = (symbol or "WOLF").strip().upper()
    out = get_intraday_session(sym)
    if out:
        return out
    return {"symbol": sym, "as_of_ts": int(time.time()), "session": "closed", "session_label": "Closed"}


def _et_trading_date():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:
        return datetime.now(timezone.utc).date()


def _daily_bar_in_progress(rows: List[dict]) -> bool:
    """True when the latest daily bar is today's ET session still trading."""
    if not rows:
        return False
    last_d = _parse_bar_date(str(rows[-1].get("ts", "")))
    if not last_d:
        return False
    try:
        bar_d = date.fromisoformat(last_d)
    except ValueError:
        return False
    today = _et_trading_date()
    if bar_d != today:
        return False
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return False
    if now_et.weekday() >= 5:
        return False
    hm = now_et.hour * 60 + now_et.minute
    return hm < 16 * 60  # after 4:00 PM ET cash close → score today's session


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
    invert_cols = (meta or {}).get("feature_inversions") or []
    if model is None or not feature_cols:
        reject = None
        try:
            from core.db import db_conn
            with db_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT value FROM ghost_v3_model WHERE key=%s", (f"meta_{sym}",))
                row = cur.fetchone()
            if row and row[0]:
                import json as _json
                from core.signal_engine import model_serve_guard
                reject = model_serve_guard(_json.loads(row[0]))
        except Exception:
            reject = None
        reason = "serve_reject" if reject else "no_v3_model"
        last_fail = None
        if reject:
            try:
                from core.signal_engine import get_last_train_fail_for_symbol
                last_fail = get_last_train_fail_for_symbol(sym)
            except Exception:
                last_fail = None
        return {
            "ok": True,
            "symbol": sym,
            "has_model": False,
            "reason": reason,
            "serve_reject": reject,
            "last_train_fail": last_fail,
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

    # Completed sessions only for scoring; exclude today's in-progress daily bar.
    completed_end = len(rows) - 1
    today_in_progress = _daily_bar_in_progress(rows)
    if today_in_progress and completed_end > 0:
        completed_end = len(rows) - 2
    start_idx = max(1, completed_end - days)
    out_days: List[Dict[str, Any]] = []

    for i in range(start_idx, completed_end + 1):
        prior = rows[i - 1]
        target = rows[i]
        prior_close = float(prior.get("close") or 0)
        if prior_close <= 0:
            continue
        up_prob = _up_prob_at_bar(rows, i - 1, model, feature_cols, invert_cols)
        if up_prob is None:
            continue
        band_vol = forecast_band_vol_pct(sym, asset_type, rows, end_idx=i - 1)
        predicted = forecast_ohlc_from_prob(prior_close, up_prob, sym, asset_type, band_vol=band_vol)
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
        live_prob = _up_prob_at_bar(rows, len(rows) - 1, model, feature_cols, invert_cols)
        if live_prob is not None:
            band_vol = forecast_band_vol_pct(sym, asset_type, rows)
            live_pred = forecast_ohlc_from_prob(last_close, live_prob, sym, asset_type, band_vol=band_vol)
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
    dir_pct = round(dir_hits / len(scored) * 100, 1) if scored else None
    level_avg = round(sum(overall_vals) / len(overall_vals), 2) if overall_vals else None
    live_band = forecast_band_vol_pct(sym, asset_type, rows) if rows else None
    summary = {
        "scored_days": len(scored),
        "avg_overall_pct": level_avg,
        "level_closeness_avg_pct": level_avg,
        "direction_hit_rate_pct": dir_pct,
        "trade_direction_hit_rate_pct": dir_pct,
        "direction_metric": "classifier_up_prob",
        "model_accuracy_holdout_pct": round(float(meta.get("accuracy", 0)) * 100, 1) if meta else None,
        "calibrated": bool(meta.get("calibrated")) if meta else None,
        "calibration_brier": meta.get("gate_brier") if meta else None,
        "forecast_band_vol_pct": live_band.get("vol_pct") if live_band else None,
        "forecast_band_vol_source": live_band.get("source") if live_band else None,
        "forecast_realized_range_pct": live_band.get("realized_range_pct") if live_band else None,
    }

    live_row = next((d for d in reversed(out_days) if not d.get("resolved")), None)
    last_completed = next((d for d in reversed(out_days) if d.get("resolved")), None)
    live_quote = live_now_quote(sym, asset_type)
    panel = build_prediction_panel(
        sym, rows, out_days, model, feature_cols, invert_cols, asset_type, live_quote,
    )

    return {
        "ok": True,
        "symbol": sym,
        "has_model": True,
        "days": out_days,
        "summary": summary,
        "generated_at": int(datetime.now(timezone.utc).timestamp()),
        "next_session_forecast": live_row,
        "last_completed_session": last_completed,
        "panel": panel,
        "today_in_progress": today_in_progress,
        "live_now": live_quote,
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
    stored = set((st.get("stored_symbols") or st.get("symbols") or {}).keys())
    try:
        from core.signal_engine import get_last_train_fail_for_symbol
    except Exception:
        get_last_train_fail_for_symbol = None  # type: ignore
    pairs = watchlist_symbol_pairs(include_portfolio=True)
    all_syms = [sym for sym, _ in pairs] or ["WOLF"]
    entries = []
    stale_count = 0
    for sym in all_syms:
        entry = {"symbol": sym, "has_model": sym in loaded, "serveable": sym in loaded}
        if sym in stored and sym not in loaded:
            reject = ((st.get("stored_symbols") or {}).get(sym) or {}).get("serve_reject")
            if reject:
                entry["serve_reject"] = reject
                stale_count += 1
                if get_last_train_fail_for_symbol:
                    fail = get_last_train_fail_for_symbol(sym)
                    if fail:
                        entry["last_train_fail"] = fail
        entries.append(entry)
    trained = [e["symbol"] for e in entries if e["has_model"]]
    missing = [e["symbol"] for e in entries if not e["has_model"]]
    no_model = [sym for sym in missing if sym not in stored]
    return {
        "ok": True,
        "watchlist": entries,
        "symbols": all_syms,
        "trained_symbols": trained,
        "missing_symbols": missing,
        "no_model_symbols": no_model,
        "serveable_count": len(trained),
        "stale_count": stale_count,
        "needs_train_count": len(missing),
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
