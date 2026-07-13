"""api/routes_ghost_system.py — endpoint group split out of wolf_app.py (PR #130).

Endpoint bodies late-import shared helpers from wolf_app at request time so
tests that monkeypatch wolf_app attributes (db_conn, _cron_ok, ...) keep
working, and so this module never imports wolf_app at import time (no cycle).
wolf_app re-exports every endpoint name for backward compatibility.
"""
import os, sys, time, json, logging, threading, hmac, math, asyncio, base64  # noqa: F401,E401

from fastapi import APIRouter, Header, HTTPException, Request, Depends  # noqa: F401
from fastapi.responses import JSONResponse, HTMLResponse, Response, PlainTextResponse  # noqa: F401

router = APIRouter()

@router.get("/api/ghost/blueprint")
def ghost_blueprint_endpoint():
    """Phase 1+2 module status for admin/cockpit verification."""
    try:
        from core.feature_drift import compute_drift
        from core.ghost_contract import ghost_contract
        from core.news_sentiment import score_articles
        from core.options_flow import probe_options_flow
        from core.regime_calibration import regime_calibration_enabled, sma5_gate_trend_up_bypass
        from core.squeeze_ml_v2 import model_info

        drift = compute_drift("WOLF", window=14)
        opts = probe_options_flow("WOLF")
        articles: list = []
        try:
            from core.news import get_recent_articles

            articles = get_recent_articles(20, symbol="WOLF") or []
        except Exception:
            pass
        sent = score_articles(articles, symbol="WOLF")
        return {
            "ok": True,
            "contract": ghost_contract(),
            "phase1": {
                "regime_calibration": regime_calibration_enabled(),
                "sma5_trend_up_bypass": sma5_gate_trend_up_bypass(),
                "squeeze_ml_v2": model_info(),
            },
            "phase2": {
                "feature_drift": {"status": drift.get("status"), "alerts": len(drift.get("alerts") or [])},
                "news_sentiment": {"label": sent.get("label"), "count": sent.get("count")},
                "options_flow": {
                    "available": opts.get("available"),
                    "put_call_volume_ratio": opts.get("put_call_volume_ratio"),
                },
            },
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/ghost/contract")
def ghost_contract_endpoint():
    """Post-falsification product contract — honest lane positioning (Phase 1)."""
    try:
        from core.ghost_contract import ghost_contract

        return {"ok": True, **ghost_contract()}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/ghost/doctrine")
def ghost_doctrine_spec_endpoint():
    """Static Ghost Doctrine specification — 6-step thinking layer (PR #129)."""
    try:
        from core.ghost_doctrine import ghost_doctrine_spec

        return {"ok": True, **ghost_doctrine_spec()}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/ghost/doctrine/{symbol}")
def ghost_doctrine_symbol_endpoint(
    symbol: str,
    light: int = 0,
    live: int = 0,
):
    """Per-symbol 6-step doctrine (PR #129).

    light=1: cheap DB-only mode (latest ledger row, no super-ghost build)
    live=1:  additionally runs predict_live_ex + up_prob inversion (heavy)
    """
    sym = (symbol or "").strip().upper()
    mode = "light" if int(light) else "full"
    include_live = bool(int(live))
    cache_key = f"ghost-doctrine:{sym}:{mode}:{int(include_live)}"

    # Check cache (reuse wolf_endpoints cache aliases)
    try:
        from api.wolf_endpoints import _cache_get, _cache_set
        cached = _cache_get(cache_key, 180.0)
        if cached:
            return cached
    except Exception:
        _cache_get = None
        _cache_set = None

    try:
        from core.ghost_doctrine import build_symbol_doctrine

        payload = build_symbol_doctrine(sym, mode=mode, include_live_gate=include_live)
        if _cache_set:
            _cache_set(cache_key, payload)
        return payload
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/ghost/drift")
def ghost_drift_endpoint(symbol: str = "WOLF", window: int = 14):
    """Feature drift alerts vs baseline snapshots (Phase 2)."""
    try:
        from core.feature_drift import compute_drift

        return compute_drift(symbol, window=window)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/ghost/options")
def ghost_options_endpoint(symbol: str = "WOLF"):
    """Options put/call volume probe (Phase 2)."""
    try:
        from core.options_flow import probe_options_flow

        return probe_options_flow(symbol)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/ghost/regime")
def ghost_regime_endpoint(symbol: str = "WOLF"):
    """Unified price + engine regime label (Phase 2)."""
    from wolf_app import db_conn  # late import — shared state + monkeypatch-safe
    try:
        from core.regime_classifier import unified_regime

        sym = (symbol or "WOLF").upper()
        payload: dict = {"price": None, "sma_5d": None, "volume_ratio": None}
        try:
            with db_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT payload FROM ghost_feature_snapshots
                    WHERE symbol = %s AND payload IS NOT NULL
                    ORDER BY feature_asof_ts DESC
                    LIMIT 1
                    """,
                    (sym,),
                )
                row = cur.fetchone()
                if row and row[0] and isinstance(row[0], dict):
                    p = row[0]
                    payload["price"] = p.get("close") or p.get("price")
                    payload["sma_5d"] = p.get("sma_5d")
                    payload["volume_ratio"] = p.get("volume_ratio")
                    payload["above_ema200"] = p.get("above_ema200")
                    payload["adx_trending"] = p.get("adx_trending")
                    payload["ema_trend_bullish"] = p.get("ema_trend_bullish")
                    payload["adx"] = p.get("adx")
        except Exception:
            pass
        regime = unified_regime(**{k: payload[k] for k in payload if payload[k] is not None})
        return {"ok": True, "symbol": sym, **regime}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/ghost/score-spec")
def ghost_score_spec_endpoint():
    """Ghost Score v1.0 specification — weights, thresholds, signal labels,
    modifier tables, and determinism audit. Public, read-only. P3 audit."""
    try:
        from core.ghost_score_spec import (
            GHOST_SCORE_SPEC_VERSION,
            GHOST_WEIGHTS,
            GHOST_SIGNAL_LABELS,
            SQUEEZE_MODIFIER,
            REGIME_MODIFIER,
        )
        return {
            "ok": True,
            "version": GHOST_SCORE_SPEC_VERSION,
            "weights": GHOST_WEIGHTS,
            "signal_labels": {f"{lo}-{hi}": label for (lo, hi), label in GHOST_SIGNAL_LABELS.items()},
            "squeeze_modifier": SQUEEZE_MODIFIER,
            "regime_modifier": REGIME_MODIFIER,
            "deterministic": True,
            "note": "All components are deterministic. Claude Haiku sentiment is NOT a Ghost Score component.",
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/ghost/sentiment")
def ghost_sentiment_endpoint(symbol: str = "WOLF", limit: int = 20):
    """Lexicon sentiment on recent headlines (Phase 2)."""
    try:
        from core.news import get_recent_articles
        from core.news_sentiment import score_articles

        sym = (symbol or "WOLF").upper()
        raw = get_recent_articles(min(limit, 50), symbol=sym) or []
        out = score_articles(raw, symbol=sym)
        out["symbol"] = sym
        return out
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)




@router.get("/api/watcher/summary")
def watcher_summary_endpoint(days: int = 30, limit: int = 5000):
    """Read-only Watcher: calibration, blind spots, and shadow-brain evidence.

    The Watcher is a notebook, not a control loop; it never mutates Ghost.
    """
    try:
        from core.watcher import watcher_summary
        return watcher_summary(days=days, limit=limit)
    except Exception as e:
        return JSONResponse({"ok": False, "read_only": True, "error": str(e)[:200]}, status_code=500)


@router.get("/api/watcher/snapshots")
def watcher_snapshots_endpoint(limit: int = 20):
    """Read Watcher's own append-only notebook rows."""
    try:
        from core.watcher import latest_watcher_snapshots
        return latest_watcher_snapshots(limit=limit)
    except Exception as e:
        return JSONResponse({"ok": False, "read_only": True, "error": str(e)[:200]}, status_code=500)


@router.get("/api/shadow-stats")
def shadow_stats_endpoint(days: int = 30):
    """Per-symbol virtual hit-rate scoreboard (shadow scoring). Read-only.

    Every scanned symbol's daily model evaluation is resolved against real
    prices with the live TP/SL bar-path rules — gates ignored — so the
    operator can see which models have live edge before they ever fire.
    """
    try:
        from core.shadow_outcomes import shadow_stats
        return shadow_stats(days=days)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/squeeze/daily-log")
def squeeze_daily_log_endpoint(
    session_date: str = "",
    days: int = 14,
):
    """Squeeze prediction ledger — Ghost buy/sell/stop vs cash-session OHLC (EOD resolve)."""
    try:
        from core.squeeze_outcomes import squeeze_daily_log

        return squeeze_daily_log(
            session_date=session_date.strip() or None,
            days=max(1, min(90, int(days))),
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200], "rows": []}, status_code=500)


@router.get("/api/squeeze/picks")
def squeeze_picks_endpoint():
    """Live short-squeeze picks — same fields as Telegram alerts (buy/sell/confidence)."""
    try:
        from core.market_hours import (
            is_us_after_hours,
            is_us_extended_hours,
            is_us_premarket,
            is_us_rth,
            market_session_label,
            next_radar_resume_label,
            now_ct_iso,
            now_et_iso,
        )
        from core.squeeze_monitor import get_squeeze_picks

        board = get_squeeze_picks()
        return {
            **board,
            "ok": True,
            "enabled": os.getenv("SQUEEZE_MONITOR_ENABLED", "1") == "1",
            "market_session": market_session_label(),
            "now_ct": now_ct_iso(),
            "now_et": now_et_iso(),
            "is_rth": is_us_rth(),
            "is_premarket": is_us_premarket(),
            "is_after_hours": is_us_after_hours(),
            "is_extended_hours": is_us_extended_hours(),
            "radar_active": board.get("radar_active", is_us_extended_hours()),
            "radar_resume_ct": board.get("radar_resume_ct") or next_radar_resume_label(),
            "scan_interval_sec": int(os.getenv("SQUEEZE_MONITOR_INTERVAL", "60")),
            "panel_refresh_sec": int(os.getenv("SQUEEZE_PANEL_REFRESH_SEC", "180")),
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200], "picks": []}, status_code=500)


@router.get("/api/squeeze/status")
def squeeze_status_endpoint():
    """Last watchlist squeeze-radar scan snapshot (44 symbols, RVOL + candidates)."""
    try:
        from core.market_hours import (
            is_us_after_hours,
            is_us_extended_hours,
            is_us_premarket,
            is_us_rth,
            market_session_label,
            now_ct_iso,
            now_et_iso,
        )
        from core.squeeze_monitor import get_squeeze_status

        st = get_squeeze_status()
        return {
            "ok": True,
            "enabled": os.getenv("SQUEEZE_MONITOR_ENABLED", "1") == "1",
            "market_session": market_session_label(),
            "now_ct": now_ct_iso(),
            "now_et": now_et_iso(),
            "is_rth": is_us_rth(),
            "is_premarket": is_us_premarket(),
            "is_after_hours": is_us_after_hours(),
            "is_extended_hours": is_us_extended_hours(),
            "scan_interval_sec": int(os.getenv("SQUEEZE_MONITOR_INTERVAL", "60")),
            "last_scan": st,
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/system/breakers")
def system_breakers_endpoint():
    """Per-breaker status — name, state, failure count, cooldown, rate-limit info."""
    try:
        from core.circuit_breaker import all_breaker_status
        return {"ok": True, "breakers": all_breaker_status()}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/system/degraded")
def system_degraded_endpoint():
    """Degraded-mode status — open circuit breakers, confidence bump, squeeze interval. P3 audit."""
    try:
        from core.degraded_mode import check_degraded
        return {"ok": True, **check_degraded()}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/system/latency")
def system_latency_endpoint():
    """Request latency SLOs — p50/p95/p99 per route over 5-min window. P3 audit."""
    try:
        from core.latency_slo import all_stats, slowest_routes
        stats = all_stats()
        return {
            "ok": True,
            **stats,
            "slowest_routes": slowest_routes(5),
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/regime", include_in_schema=False)
def api_regime():
    """WOLF-only mode: regime gate is a no-op. Endpoint retained for back-compat."""
    return {"ok": True, "block_crypto_buys": False, "reduce_size": False, "reason": "", "btc_24h_pct": 0.0}


@router.get("/api/objective")
def api_objective():
    """Progress telemetry toward configured prediction win-rate objective."""
    try:
        from core.prediction import get_objective_status
        return {"ok": True, **get_objective_status()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


@router.get("/api/objective/report")
def api_objective_report(days: int = 14):
    """Daily objective trend report for the last N days."""
    try:
        from core.prediction import get_objective_daily_report
        return {"ok": True, **get_objective_daily_report(days=days)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
