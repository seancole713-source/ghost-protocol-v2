"""
core/squeeze_monitor.py — Watchlist squeeze radar (all 44 symbols)
==================================================================
Telegram alerts for intraday short-squeeze *moments* — the thing the v3 pick
engine is NOT built for (3-day TP/SL holds + regime gates).

Unlike wolf_monitor (WOLF-only, daily-bar volume), this module:
  • Scans the full STOCK_SYMBOLS watchlist every N seconds during RTH
  • Uses time-adjusted relative volume (RVOL) so morning spikes fire early
  • Uses session HIGH vs prior close (catches the move even if price fades)
  • Tags high short-float names when yfinance short data is available

Enable: SQUEEZE_MONITOR_ENABLED=1 (default on)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

LOGGER = logging.getLogger("ghost.squeeze")

CHECK_INTERVAL_SEC = int(os.getenv("SQUEEZE_MONITOR_INTERVAL", "60"))
SQUEEZE_PRICE_PCT = float(os.getenv("SQUEEZE_PRICE_PCT", "5.0"))
SQUEEZE_VOL_MULT = float(os.getenv("SQUEEZE_VOL_MULT", "2.5"))
FORMING_PRICE_PCT = float(os.getenv("SQUEEZE_FORMING_PRICE_PCT", "3.0"))
FORMING_VOL_MULT = float(os.getenv("SQUEEZE_FORMING_VOL_MULT", "2.0"))
TP_PCT_ACTIVE = float(os.getenv("SQUEEZE_TP_PCT_ACTIVE", "4.0"))
TP_PCT_FORMING = float(os.getenv("SQUEEZE_TP_PCT_FORMING", "2.5"))

from core.market_hours import (
    PREMARKET_START_MIN,
    RTH_CLOSE_MIN,
    RTH_MINUTES,
    RTH_OPEN_MIN,
    SESSION_TZ,
    session_hm,
)
_TIMEOUT = float(os.getenv("PRICE_PROVIDER_TIMEOUT_S", "8.0"))

COOLDOWN_SEC = int(os.getenv("SQUEEZE_ALERT_COOLDOWN", "7200"))
_last_alert: Dict[str, float] = {}
_short_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_SHORT_CACHE_TTL = 86400
_last_scan_report: Dict[str, Any] = {
    "ok": False,
    "message": "No scan completed yet",
}
_scan_cache_path = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "data",
    "squeeze_last_scan.json",
)
_alert_history: List[Dict[str, Any]] = []
_ALERT_HISTORY_MAX = 30
_alert_session_date: Optional[Any] = None


def rth_elapsed_fraction(now: Optional[datetime] = None) -> float:
    """Fraction of regular session elapsed (0..1), minimum 1/390 for RVOL."""
    now_ct, hm = session_hm(now)
    if now_ct.weekday() >= 5:
        return 1.0
    if hm < RTH_OPEN_MIN:
        return max(1.0 / RTH_MINUTES, (hm - PREMARKET_START_MIN) / RTH_MINUTES)
    if hm >= RTH_CLOSE_MIN:
        return 1.0
    elapsed = hm - RTH_OPEN_MIN
    return max(elapsed / RTH_MINUTES, 1.0 / RTH_MINUTES)


def compute_rvol(session_volume: float, avg_daily_volume: float, elapsed_frac: float) -> float:
    """Time-adjusted relative volume: vol so far / expected vol by this point in session."""
    if avg_daily_volume <= 0 or session_volume <= 0:
        return 0.0
    expected = avg_daily_volume * max(elapsed_frac, 1.0 / RTH_MINUTES)
    return session_volume / expected if expected > 0 else 0.0


def evaluate_squeeze_signal(
    peak_move_pct: float,
    current_move_pct: float,
    rvol: float,
    *,
    short_risk: Optional[str] = None,
) -> Optional[str]:
    """
    Return alert kind or None.
      squeeze_active — peak +5% and RVOL ≥ threshold (classic squeeze)
      squeeze_forming — +3% / 2× RVOL, or high-short names at slightly lower bar
    """
    move = max(peak_move_pct, current_move_pct)
    high_short = short_risk in ("high", "extreme")

    if move >= SQUEEZE_PRICE_PCT and rvol >= SQUEEZE_VOL_MULT:
        return "squeeze_active"
    forming_move = FORMING_PRICE_PCT if not high_short else max(FORMING_PRICE_PCT - 0.5, 2.5)
    forming_vol = FORMING_VOL_MULT if not high_short else max(FORMING_VOL_MULT - 0.3, 1.8)
    if move >= forming_move and rvol >= forming_vol:
        return "squeeze_forming"
    return None


def prefilter_candidate(peak_move_pct: float, current_move_pct: float, rvol: float) -> bool:
    """Cheap gate before short-interest fetch (avoids 44× yfinance per cycle)."""
    move = max(peak_move_pct, current_move_pct)
    if move < 2.0 or rvol < 1.5:
        return False
    return True


def get_squeeze_status() -> Dict[str, Any]:
    """Last completed watchlist scan snapshot (for /api/squeeze/status)."""
    _ensure_scan_cache_loaded()
    return dict(_last_scan_report)


def _ensure_scan_cache_loaded() -> None:
    """Load persisted last scan when in-memory state is empty (e.g. after deploy overnight)."""
    global _last_scan_report
    if _last_scan_report.get("status") == "complete" and _last_scan_report.get("ts"):
        return
    if not os.path.isfile(_scan_cache_path):
        return
    try:
        import json

        with open(_scan_cache_path, encoding="utf-8") as fh:
            cached = json.load(fh)
        if isinstance(cached, dict) and cached.get("ts"):
            _last_scan_report = cached
    except Exception as exc:
        LOGGER.debug("[SqueezeMonitor] load scan cache: %s", exc)


def _persist_scan_report(report: Dict[str, Any]) -> None:
    if report.get("status") != "complete":
        return
    try:
        import json

        os.makedirs(os.path.dirname(_scan_cache_path), exist_ok=True)
        payload = {
            k: report.get(k)
            for k in (
                "ok", "ts", "session", "symbols", "fetch_ok", "fetch_fail",
                "fetch_failed_symbols",
                "picks", "candidates", "leaders", "duration_ms", "status", "elapsed_frac",
            )
        }
        with open(_scan_cache_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except Exception as exc:
        LOGGER.debug("[SqueezeMonitor] persist scan cache: %s", exc)


def squeeze_confidence(
    peak_move_pct: float,
    rvol: float,
    *,
    short_risk: Optional[str] = None,
    kind: str = "squeeze_forming",
) -> int:
    """0–95 squeeze confidence from move, RVOL, and short-float context.

    Capped at 95 — 100% confidence is never credible in any prediction system.
    Extreme short risk adds squeeze potential but also signals fragility, so the
    short-risk bonus is halved for "extreme" to avoid overconfidence on the
    riskiest names.
    """
    move = max(0.0, peak_move_pct)
    move_pts = min(40.0, move * 4.0)
    rvol_pts = min(30.0, max(0.0, (rvol - 1.0) * 10.0))
    short_pts = {"extreme": 10.0, "high": 12.0, "medium": 10.0, "low": 5.0}.get(
        short_risk or "", 0.0,
    )
    kind_pts = 10.0 if kind == "squeeze_active" else 0.0
    return int(round(min(95.0, max(0.0, move_pts + rvol_pts + short_pts + kind_pts))))


def squeeze_trade_levels(
    buy_price: float,
    session_high: float,
    kind: str,
) -> Tuple[float, float]:
    """Return (buy, sell) — sell targets session high when still above TP."""
    buy = round(buy_price, 2)
    tp_pct = TP_PCT_ACTIVE if kind == "squeeze_active" else TP_PCT_FORMING
    tp_sell = round(buy * (1.0 + tp_pct / 100.0), 2)
    high_sell = round(session_high, 2) if session_high > buy else tp_sell
    sell = max(tp_sell, high_sell) if high_sell > buy else tp_sell
    return buy, round(sell, 2)


def format_squeeze_alert(
    symbol: str,
    kind: str,
    metrics: Dict[str, Any],
    rvol: float,
    short_ctx: Dict[str, Any],
) -> str:
    """Simple Telegram body: symbol, buy, sell, confidence %."""
    buy, sell = squeeze_trade_levels(metrics["price"], metrics["session_high"], kind)
    conf = squeeze_confidence(
        metrics["peak_move_pct"],
        rvol,
        short_risk=short_ctx.get("squeeze_risk"),
        kind=kind,
    )
    return (
        f"🚨 SQUEEZE — {symbol.upper()}\n"
        f"Buy: ${buy:.2f}\n"
        f"Sell: ${sell:.2f}\n"
        f"Confidence: {conf}%"
    )


def candidate_to_pick(
    symbol: str,
    kind: str,
    metrics: Dict[str, Any],
    rvol: float,
    short_ctx: Dict[str, Any],
) -> Dict[str, Any]:
    """Telegram-aligned pick row for cockpit + API (includes scorecard + probability targets)."""
    from core.squeeze_scorecard import build_scorecard_row

    row = build_scorecard_row(symbol, metrics, rvol, short_ctx, kind=kind)
    conf = squeeze_confidence(
        metrics["peak_move_pct"],
        rvol,
        short_risk=short_ctx.get("squeeze_risk"),
        kind=kind,
    )
    row["confidence_pct"] = conf
    row["short_risk"] = short_ctx.get("squeeze_risk")
    row["message"] = format_squeeze_alert(symbol, kind, metrics, rvol, short_ctx)
    return row


def get_squeeze_picks() -> Dict[str, Any]:
    """Active squeeze picks from the latest scan + recent Telegram alerts."""
    from core.market_hours import is_us_extended_hours, next_radar_resume_label
    from core.squeeze_scorecard import scorecard_legend
    from core.squeeze_live_drift import build_live_drift_board, enrich_pick_rows, first_alert_buy_map, live_price_map, attach_live_drift

    _ensure_scan_cache_loaded()
    st = dict(_last_scan_report)
    picks = list(st.get("picks") or st.get("candidates") or [])
    leaders = list(st.get("leaders") or [])
    alerts = list(_alert_history)
    picks = enrich_pick_rows(picks, alerts, leaders)
    alert_map = first_alert_buy_map(alerts)
    live_map = live_price_map(picks, leaders)
    enriched_alerts: List[Dict[str, Any]] = []
    for a in alerts:
        item = dict(a)
        sym = (item.get("symbol") or "").upper()
        live = live_map.get(sym) or item.get("price") or item.get("buy")
        if live is not None:
            try:
                item["live_price"] = round(float(live), 4)
            except (TypeError, ValueError):
                pass
        alert_buy = alert_map.get(sym)
        if alert_buy is not None and item.get("live_price") is not None:
            attach_live_drift(item, alert_buy=float(alert_buy), live_price=item["live_price"])
        enriched_alerts.append(item)
    radar_active = is_us_extended_hours()
    last_ts = st.get("ts")
    return {
        "scan_ok": bool(st.get("ok") and st.get("status") == "complete"),
        "picks": picks,
        "pick_count": len(picks),
        "alert_history": enriched_alerts,
        "live_drift": build_live_drift_board(alerts, picks, leaders),
        "last_scan_ts": last_ts,
        "last_scan_status": st.get("status"),
        "last_scan_session": st.get("session"),
        "fetch_ok": st.get("fetch_ok"),
        "fetch_fail": st.get("fetch_fail"),
        "fetch_failed_symbols": list(st.get("fetch_failed_symbols") or []),
        "symbols": st.get("symbols"),
        "duration_ms": st.get("duration_ms"),
        "leaders": leaders,
        "scorecard": scorecard_legend(),
        "radar_active": radar_active,
        "radar_resume_ct": next_radar_resume_label(),
        "snapshot_stale": bool(not radar_active and last_ts),
    }


async def prewarm_short_cache() -> None:
    """Background: warm short-interest cache before RTH (Finviz/yfinance, one symbol at a time)."""
    from config.symbols import get_edge_set
    from core.market_hours import is_us_extended_hours

    delay = float(os.getenv("SQUEEZE_SHORT_PREWARM_DELAY_S", "2.5"))
    symbols = sorted(get_edge_set())
    LOGGER.info("[SqueezeMonitor] Short-cache prewarm — %s symbols", len(symbols))
    for sym in symbols:
        if not is_us_extended_hours():
            return
        try:
            _short_context(sym)
        except Exception as exc:
            LOGGER.debug("[SqueezeMonitor] prewarm %s: %s", sym, exc)
        await asyncio.sleep(delay)


async def start_squeeze_monitor() -> None:
    enabled = os.getenv("SQUEEZE_MONITOR_ENABLED", "1") == "1"
    if not enabled:
        LOGGER.info("[SqueezeMonitor] Disabled by SQUEEZE_MONITOR_ENABLED=0")
        return

    LOGGER.info(
        "[SqueezeMonitor] Starting — watchlist scan every %ss "
        "(active: +%.1f%% & %.1fx RVOL)",
        CHECK_INTERVAL_SEC,
        SQUEEZE_PRICE_PCT,
        SQUEEZE_VOL_MULT,
    )
    if os.getenv("SQUEEZE_SHORT_PREWARM", "1").strip().lower() in ("1", "true", "yes", "on"):
        asyncio.create_task(prewarm_short_cache())
    _ensure_scan_cache_loaded()
    while True:
        try:
            await _run_watchlist_scan()
        except Exception as exc:
            LOGGER.error("[SqueezeMonitor] scan failed: %s", exc, exc_info=False)
            global _last_scan_report
            _last_scan_report = {
                "ok": False,
                "ts": int(time.time()),
                "status": "error",
                "error": str(exc)[:200],
            }
        # P3 (audit): degraded mode — double scan interval when APIs are down
        _interval = CHECK_INTERVAL_SEC
        try:
            from core.degraded_mode import degraded_squeeze_interval_mult
            _mult = degraded_squeeze_interval_mult()
            if _mult > 1.0:
                _interval = int(CHECK_INTERVAL_SEC * _mult)
        except Exception:
            pass
        await asyncio.sleep(_interval)


def _reset_alert_history_if_new_session() -> None:
    """Clear in-memory Telegram alert list at start of each CT calendar day."""
    global _alert_history, _alert_session_date
    from core.market_hours import session_hm

    today = session_hm()[0].date()
    if _alert_session_date != today:
        _alert_history = []
        _alert_session_date = today


async def _run_watchlist_scan() -> None:
    from concurrent.futures import ThreadPoolExecutor

    from core.market_hours import is_us_extended_hours, is_us_premarket, is_us_rth

    if not is_us_extended_hours():
        return

    _reset_alert_history_if_new_session()

    from config.symbols import get_edge_set

    symbols = sorted(get_edge_set())
    loop = asyncio.get_running_loop()
    elapsed = rth_elapsed_fraction()
    t0 = time.time()
    report: Dict[str, Any] = {
        "ok": True,
        "ts": int(time.time()),
        "session": "rth" if is_us_rth() else ("premarket" if is_us_premarket() else "extended"),
        "symbols": len(symbols),
        "fetch_ok": 0,
        "fetch_fail": 0,
        "fetch_failed_symbols": [],
        "candidates": [],
        "alerts_sent": 0,
        "duration_ms": 0,
        "elapsed_frac": round(elapsed, 4),
        "status": "running",
    }
    global _last_scan_report
    _last_scan_report = dict(report)

    fetch_timeout = float(os.getenv("SQUEEZE_FETCH_TIMEOUT_S", "18"))
    # Default to 1 worker to avoid hammering APIs with parallel requests.
    # 4 workers × 44 symbols = 176 concurrent API calls that trigger 429 storms.
    workers = int(os.getenv("SQUEEZE_FETCH_WORKERS", "1"))
    # Inter-symbol delay for sequential fetches
    fetch_delay = float(os.getenv("SQUEEZE_FETCH_DELAY_S", "0.3"))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        tasks = {
            sym: loop.run_in_executor(pool, _sync_fetch_metrics, sym) for sym in symbols
        }
        metrics_map: Dict[str, Optional[Dict[str, Any]]] = {}
        for sym, task in tasks.items():
            try:
                metrics_map[sym] = await asyncio.wait_for(task, timeout=fetch_timeout)
            except asyncio.TimeoutError:
                LOGGER.warning("[SqueezeMonitor] fetch timeout %s (%.0fs)", sym, fetch_timeout)
                metrics_map[sym] = None
            except Exception as exc:
                LOGGER.debug("[SqueezeMonitor] fetch %s: %s", sym, exc)
                metrics_map[sym] = None
            # Inter-symbol delay to prevent API rate-limit storms
            if fetch_delay > 0:
                await asyncio.sleep(fetch_delay)

    short_ctx_map: Dict[str, Dict[str, Any]] = {}
    for symbol in symbols:
        metrics = metrics_map.get(symbol)
        if not metrics:
            report["fetch_fail"] += 1
            report["fetch_failed_symbols"].append(symbol)
            continue
        report["fetch_ok"] += 1
        rvol = compute_rvol(metrics["session_volume"], metrics["avg_daily_volume"], elapsed)
        peak_pct = metrics["peak_move_pct"]
        current_pct = metrics["current_move_pct"]

        report.setdefault("leaders", []).append({
            "symbol": symbol,
            "peak_move_pct": round(peak_pct, 2),
            "current_move_pct": round(current_pct, 2),
            "rvol": round(rvol, 2),
            "price": round(float(metrics["price"]), 2),
        })

        kind = evaluate_squeeze_signal(peak_pct, current_pct, rvol, short_risk=None)
        short_ctx: Dict[str, Any] = {}
        if not kind and prefilter_candidate(peak_pct, current_pct, rvol):
            short_ctx = _short_context(symbol)
            short_ctx_map[symbol] = short_ctx
            kind = evaluate_squeeze_signal(
                peak_pct, current_pct, rvol, short_risk=short_ctx.get("squeeze_risk"),
            )
        elif kind:
            short_ctx = _short_context(symbol)
            short_ctx_map[symbol] = short_ctx

        if kind:
            pick = candidate_to_pick(symbol, kind, metrics, rvol, short_ctx)
            report["candidates"].append(pick)
            try:
                from core.squeeze_outcomes import record_squeeze_prediction

                record_squeeze_prediction(pick, source="candidate")
            except Exception:
                pass
            if _maybe_alert(symbol, kind, metrics, rvol, short_ctx):
                report["alerts_sent"] += 1
                alerted_at = int(time.time())
                _alert_history.insert(0, {**pick, "alerted_at": alerted_at})
                del _alert_history[_ALERT_HISTORY_MAX:]
                try:
                    from core.squeeze_outcomes import record_squeeze_prediction

                    record_squeeze_prediction(
                        {**pick, "alerted_at": alerted_at},
                        source="telegram",
                        alerted_at=alerted_at,
                    )
                except Exception:
                    pass

    report["picks"] = list(report["candidates"])
    from core.squeeze_scorecard import build_scorecard_row

    leaders = report.get("leaders") or []
    leaders.sort(key=lambda x: (x.get("peak_move_pct") or 0, x.get("rvol") or 0), reverse=True)
    enriched: List[Dict[str, Any]] = []
    for leader in leaders[:8]:
        sym = leader["symbol"]
        metrics = metrics_map.get(sym)
        if not metrics:
            enriched.append(leader)
            continue
        short_ctx = short_ctx_map.get(sym) or _short_context(sym)
        enriched.append(
            build_scorecard_row(
                sym,
                metrics,
                float(leader["rvol"]),
                short_ctx,
                kind="squeeze_forming",
            )
        )
    report["leaders"] = enriched

    report["duration_ms"] = int((time.time() - t0) * 1000)
    report["status"] = "complete"
    _last_scan_report = report
    _persist_scan_report(report)
    LOGGER.info(
        "[SqueezeMonitor] scan ok=%s fail=%s candidates=%s ms=%s",
        report["fetch_ok"],
        report["fetch_fail"],
        len(report["candidates"]),
        report["duration_ms"],
    )


def _short_context_from_finviz(symbol: str) -> Dict[str, Any]:
    """Finviz scrape fallback when Yahoo short data is unavailable (429 / Alpaca-only mode)."""
    out: Dict[str, Any] = {
        "short_float_pct": None,
        "days_to_cover": None,
        "squeeze_risk": None,
    }
    try:
        from core.wolf_context import _fetch_finviz

        fv = _fetch_finviz(symbol.upper())
        sf = fv.get("short_float")
        dtc = fv.get("days_to_cover")
        if sf is not None:
            out["short_float_pct"] = round(float(sf), 2)
        if dtc is not None:
            out["days_to_cover"] = round(float(dtc), 2)
    except Exception as exc:
        LOGGER.debug("[SqueezeMonitor] finviz short %s: %s", symbol, exc)
    return out


def _short_context(symbol: str) -> Dict[str, Any]:
    sym = symbol.upper()
    cached = _short_cache.get(sym)
    if cached and (time.time() - cached[0]) < _SHORT_CACHE_TTL:
        return cached[1]
    out: Dict[str, Any] = {
        "short_float_pct": None,
        "days_to_cover": None,
        "squeeze_risk": None,
    }
    if _yf_short_enabled():
        from core.circuit_breaker import _yfinance_cb
        if _yfinance_cb.allow():
            try:
                import yfinance as yf

                info = yf.Ticker(sym).info or {}
                sf = info.get("shortPercentOfFloat")
                dtc = info.get("shortRatio")
                if sf is not None:
                    out["short_float_pct"] = round(float(sf) * 100, 2)
                if dtc is not None:
                    out["days_to_cover"] = round(float(dtc), 2)
                _yfinance_cb.record_success()
            except Exception as exc:
                _yfinance_cb.record_failure()
                LOGGER.debug("[SqueezeMonitor] yfinance short %s: %s", sym, exc)
    if out["short_float_pct"] is None and out["days_to_cover"] is None:
        out = _short_context_from_finviz(sym)
    from api.wolf_endpoints import _squeeze_risk_tag

    out["squeeze_risk"] = _squeeze_risk_tag(out["short_float_pct"], out["days_to_cover"])
    _short_cache[sym] = (time.time(), out)
    return out


def _yf_short_enabled() -> bool:
    """Separate from price fallback — short-interest can use Yahoo when not rate-limited."""
    if os.getenv("SQUEEZE_YF_SHORT", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    return True


def _yf_fallback_enabled() -> bool:
    """Avoid hammering Yahoo during 44-symbol parallel scans (429 kills SPCE/WOLF)."""
    if os.getenv("ALPACA_KEY_ID", "") and os.getenv("SQUEEZE_YF_FALLBACK", "0").strip().lower() not in (
        "1", "true", "yes", "on",
    ):
        return False
    return os.getenv("SQUEEZE_YF_FALLBACK", "1").strip().lower() in ("1", "true", "yes", "on")


def _alpaca_headers() -> Optional[dict]:
    key = os.getenv("ALPACA_KEY_ID", "")
    secret = os.getenv("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        return None
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def _alpaca_prev_close(symbol: str) -> Optional[float]:
    """Prior session close from Alpaca 1Day bars (no Yahoo)."""
    headers = _alpaca_headers()
    if not headers:
        return None
    sym = symbol.upper()
    try:
        import requests

        end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        start = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        from core.prices import _alpaca_bar_feeds, _note_alpaca_feed_status
        for feed in _alpaca_bar_feeds():
            url = (
                f"https://data.alpaca.markets/v2/stocks/{sym}/bars"
                f"?timeframe=1Day&start={start}&end={end}&limit=5&feed={feed}"
            )
            r = requests.get(url, headers=headers, timeout=_TIMEOUT)
            if r.status_code != 200:
                _note_alpaca_feed_status(feed, r.status_code)
                continue
            dbars = r.json().get("bars") or []
            if len(dbars) >= 2:
                return round(float(dbars[-2].get("c", 0)), 4)
            if len(dbars) == 1:
                return round(float(dbars[0].get("o", 0)), 4)
    except Exception as exc:
        LOGGER.debug("[SqueezeMonitor] alpaca prev_close %s: %s", sym, exc)
    return None


def _sync_fetch_metrics(symbol: str) -> Optional[Dict[str, Any]]:
    """Price/OHLCV via core.prices session helper + Alpaca/yfinance volume."""
    sym = (symbol or "").upper().strip()
    if not sym:
        return None

    try:
        from core.prices import get_intraday_session

        sess = get_intraday_session(sym)
        prev_close = sess.get("previous_close")
        last_px = sess.get("price")
        session_high = sess.get("today_high") or sess.get("rth_high") or last_px
        if not last_px or float(last_px) <= 0:
            return _yf_fetch_metrics(sym) if _yf_fallback_enabled() else None
        if not prev_close or float(prev_close) <= 0:
            prev_close = _alpaca_prev_close(sym)
        if not prev_close or float(prev_close) <= 0:
            return _yf_fetch_metrics(sym) if _yf_fallback_enabled() else None
        prev_close = float(prev_close)
        last_px = float(last_px)
        session_high = float(session_high or last_px)

        avg_vol, session_vol, vwap = _fetch_volumes(sym)
        if not avg_vol or avg_vol <= 0:
            return _yf_fetch_metrics(sym) if _yf_fallback_enabled() else None
        if not session_vol or session_vol <= 0:
            session_vol = avg_vol * 0.5

        return {
            "price": last_px,
            "prior_close": prev_close,
            "session_high": session_high,
            "session_volume": float(session_vol),
            "avg_daily_volume": float(avg_vol),
            "vwap": vwap,
            "peak_move_pct": (session_high - prev_close) / prev_close * 100,
            "current_move_pct": (last_px - prev_close) / prev_close * 100,
        }
    except Exception as exc:
        LOGGER.debug("[SqueezeMonitor] metrics %s: %s", sym, exc)
        return _yf_fetch_metrics(sym) if _yf_fallback_enabled() else None


def _vwap_from_bars(bars: List[Dict[str, Any]]) -> Optional[float]:
    num = den = 0.0
    for b in bars:
        v = float(b.get("v", 0) or 0)
        if v <= 0:
            continue
        h = float(b.get("h", 0) or 0)
        l = float(b.get("l", 0) or 0)
        c = float(b.get("c", 0) or 0)
        if h <= 0 and l <= 0 and c <= 0:
            continue
        tp = (h + l + c) / 3.0
        num += tp * v
        den += v
    return round(num / den, 4) if den > 0 else None


def _fetch_volumes(symbol: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Return (avg_daily_volume, session_volume_so_far, session_vwap)."""
    sym = symbol.upper()
    headers = _alpaca_headers()
    if headers:
        try:
            import requests

            now_utc = datetime.now(timezone.utc)
            try:
                from zoneinfo import ZoneInfo

                ct = ZoneInfo(SESSION_TZ)
            except Exception:
                ct = None
            if ct:
                day_start = datetime.now(ct).replace(
                    hour=PREMARKET_START_MIN // 60,
                    minute=PREMARKET_START_MIN % 60,
                    second=0,
                    microsecond=0,
                )
                day_start = day_start.astimezone(timezone.utc)
            else:
                day_start = now_utc.replace(hour=9, minute=0, second=0, microsecond=0)
            start_str = day_start.strftime("%Y-%m-%dT%H:%M:%SZ")
            end_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
            avg_vol = session_vol = vwap = None
            from core.prices import _alpaca_bar_feeds, _note_alpaca_feed_status
            for feed in _alpaca_bar_feeds():
                url = (
                    f"https://data.alpaca.markets/v2/stocks/{sym}/bars"
                    f"?timeframe=1Day&start={(now_utc - timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%SZ')}"
                    f"&end={end_str}&limit=25&feed={feed}"
                )
                r = requests.get(url, headers=headers, timeout=_TIMEOUT)
                if r.status_code != 200:
                    _note_alpaca_feed_status(feed, r.status_code)
                    continue
                dbars = r.json().get("bars") or []
                vols = [float(b.get("v", 0)) for b in dbars[-20:] if b.get("v")]
                if vols:
                    avg_vol = sum(vols) / len(vols)
                    break
            for feed in _alpaca_bar_feeds():
                url = (
                    f"https://data.alpaca.markets/v2/stocks/{sym}/bars"
                    f"?timeframe=5Min&start={start_str}&end={end_str}&limit=10000&feed={feed}"
                )
                r = requests.get(url, headers=headers, timeout=_TIMEOUT)
                if r.status_code != 200:
                    _note_alpaca_feed_status(feed, r.status_code)
                    continue
                bars = r.json().get("bars") or []
                if bars:
                    session_vol = sum(float(b.get("v", 0)) for b in bars if b.get("v"))
                    vwap = _vwap_from_bars(bars)
                    break
            if avg_vol and session_vol:
                return avg_vol, session_vol, vwap
            if avg_vol:
                return avg_vol, avg_vol * 0.4, vwap
        except Exception as exc:
            LOGGER.debug("[SqueezeMonitor] alpaca vol %s: %s", sym, exc)

    if not _yf_fallback_enabled():
        return None, None, None

    from core.circuit_breaker import _yfinance_cb
    if not _yfinance_cb.allow():
        return None, None, None

    try:
        import yfinance as yf

        t = yf.Ticker(sym)
        hist = t.history(period="30d", interval="1d")
        intraday = t.history(period="1d", interval="5m")
        if hist is None or hist.empty:
            return None, None, None
        avg_vol = float(hist["Volume"].iloc[-20:].mean())
        vwap = None
        if intraday is not None and not intraday.empty:
            session_vol = float(intraday["Volume"].sum())
            tp = (intraday["High"] + intraday["Low"] + intraday["Close"]) / 3.0
            vols = intraday["Volume"].astype(float)
            den = float(vols.sum())
            vwap = round(float((tp * vols).sum() / den), 4) if den > 0 else None
        else:
            session_vol = float(hist["Volume"].iloc[-1])
        _yfinance_cb.record_success()
        return avg_vol, session_vol, vwap
    except Exception:
        _yfinance_cb.record_failure()
        return None, None, None


def _yf_fetch_metrics(symbol: str) -> Optional[Dict[str, Any]]:
    from core.circuit_breaker import _yfinance_cb
    if not _yfinance_cb.allow():
        return None
    try:
        import yfinance as yf

        t = yf.Ticker(symbol)
        hist = t.history(period="30d", interval="1d")
        intraday = t.history(period="1d", interval="5m")
        if hist is None or hist.empty or len(hist) < 2:
            return None
        prev_close = float(hist["Close"].iloc[-2])
        avg_vol = float(hist["Volume"].iloc[-20:].mean())
        if intraday is not None and not intraday.empty:
            session_vol = float(intraday["Volume"].sum())
            session_high = float(intraday["High"].max())
            last_px = float(intraday["Close"].iloc[-1])
        else:
            session_vol = float(hist["Volume"].iloc[-1])
            session_high = float(hist["High"].iloc[-1])
            last_px = float(hist["Close"].iloc[-1])
        if prev_close <= 0:
            return None
        vwap = None
        if intraday is not None and not intraday.empty:
            tp = (intraday["High"] + intraday["Low"] + intraday["Close"]) / 3.0
            vols = intraday["Volume"].astype(float)
            den = float(vols.sum())
            vwap = round(float((tp * vols).sum() / den), 4) if den > 0 else None
        _yfinance_cb.record_success()
        return {
            "price": last_px,
            "prior_close": prev_close,
            "session_high": session_high,
            "session_volume": session_vol,
            "avg_daily_volume": avg_vol,
            "vwap": vwap,
            "peak_move_pct": (session_high - prev_close) / prev_close * 100,
            "current_move_pct": (last_px - prev_close) / prev_close * 100,
        }
    except Exception:
        _yfinance_cb.record_failure()
        return None


def _maybe_alert(
    symbol: str,
    kind: str,
    metrics: Dict[str, Any],
    rvol: float,
    short_ctx: Dict[str, Any],
) -> bool:
    key = f"{symbol}:{kind}"
    now = time.time()
    if now - _last_alert.get(key, 0) < COOLDOWN_SEC:
        return False
    _last_alert[key] = now
    msg = format_squeeze_alert(symbol, kind, metrics, rvol, short_ctx)
    _send_telegram(key, msg)
    return True


def _send_telegram(key: str, message: str) -> None:
    try:
        from core.telegram_hunter import send_telegram_message

        ok = send_telegram_message(message)
        LOGGER.info("[SqueezeMonitor] Alert [%s]: %s", key, "OK" if ok else "FAILED")
    except Exception as exc:
        LOGGER.error("[SqueezeMonitor] Telegram failed [%s]: %s", key, exc)
