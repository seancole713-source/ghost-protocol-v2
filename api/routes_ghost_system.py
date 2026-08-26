"""api/routes_ghost_system.py — endpoint group split out of wolf_app.py (PR #130).

Endpoint bodies late-import shared helpers from wolf_app at request time so
tests that monkeypatch wolf_app attributes (db_conn, _cron_ok, ...) keep
working, and so this module never imports wolf_app at import time (no cycle).
wolf_app re-exports every endpoint name for backward compatibility.
"""
import os, sys, time, json, logging, threading, hmac, math, asyncio, base64  # noqa: F401,E401

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request, Depends  # noqa: F401
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


@router.get("/api/ghost/options/snapshots")
def ghost_options_snapshots_endpoint(symbol: str = "", days: int = 30, limit: int = 500):
    """Read-only daily point-in-time options snapshot history."""
    try:
        from core.options_snapshots import get_snapshots

        return get_snapshots(symbol or None, days=max(1, min(365, int(days))),
                             limit=max(1, min(5000, int(limit))))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/ghost/options/readiness")
def ghost_options_readiness_endpoint(days: int = 60):
    """Accrual health for the options forward-clock — catches silent failure."""
    try:
        from core.options_edge import options_pcr_readiness

        return options_pcr_readiness(days=max(1, min(365, int(days))))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/ghost/options/edge-test")
def ghost_options_edge_test_endpoint(days: int = 60):
    """PCR-edge verdict (read-only): does put/call ratio separate winners?
    Provisional until sufficient_data=true. Changes no gate."""
    try:
        from core.options_edge import options_pcr_edge

        return options_pcr_edge(days=max(1, min(365, int(days))))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.post("/api/ghost/options/snapshot-run")
def ghost_options_snapshot_run_endpoint(request: Request,
                                        x_cron_secret: str = Header(default="")):
    """Manual snapshot trigger (ops/backfill) — admin/cron gated."""
    from wolf_app import _ADMIN_COOKIE, _admin_token_valid, _cron_ok  # late import — shared state
    tok = request.cookies.get(_ADMIN_COOKIE, "")
    if not (_cron_ok(x_cron_secret, strict=True) or _admin_token_valid(tok)):
        raise HTTPException(status_code=403, detail="admin login or cron secret required")
    try:
        from core.options_snapshots import record_snapshots

        return record_snapshots()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/ghost/contract/70-verdict")
def ghost_contract_70_verdict_endpoint(days: int = 90):
    """Pre-registered contract-70 verdict — read-only honesty layer.

    Same precedent as the 80%-claim falsification gate: criteria registered
    before the outcome; changes the claim, never the firing behavior.
    """
    try:
        from core.contract_70_verdict import contract_70_verdict

        return contract_70_verdict(days=max(7, min(365, int(days))))
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


@router.post("/api/watcher/contract-70/register")
def watcher_contract_70_register_endpoint(
    request: Request,
    x_cron_secret: str = Header(default=""),
    min_n: int = 8,
    min_wilson_low: float = 0.70,
    days: int = 30,
    limit: int = 5000,
    mode: str = "slice",
):
    """Pre-register a forward-only 70+ proof universe.

    This endpoint is intentionally conservative:

    * admin/cron gated;
    * writes only the frozen universe row in ``ghost_state``;
    * refuses criteria weaker than the 70+ contract defaults;
    * does not auto-register an empty/cherry-picked universe;
    * can freeze either a qualified slice (default) or legacy symbol universe; and
    * never changes model gates, picks, broker state, or paper-wallet state.
    """
    from wolf_app import _ADMIN_COOKIE, _admin_token_valid, _cron_ok  # late import — shared state + monkeypatch-safe
    tok = request.cookies.get(_ADMIN_COOKIE, "")
    if not (_cron_ok(x_cron_secret, strict=True) or _admin_token_valid(tok)):
        raise HTTPException(status_code=403, detail="admin login or cron secret required")

    try:
        min_n_i = int(min_n)
        min_wilson_f = float(min_wilson_low)
    except Exception:
        raise HTTPException(status_code=422, detail="invalid contract-70 registration criteria")
    if min_n_i < 8:
        raise HTTPException(status_code=422, detail="min_n cannot be weaker than 8 for contract-70 registration")
    if min_wilson_f < 0.70:
        raise HTTPException(status_code=422, detail="min_wilson_low cannot be weaker than 0.70")
    if min_wilson_f > 1.0:
        raise HTTPException(status_code=422, detail="min_wilson_low cannot exceed 1.0")

    days_i = max(1, min(365, int(days or 30)))
    limit_i = max(1, min(50000, int(limit or 5000)))
    mode_i = str(mode or "slice").strip().lower()
    if mode_i not in ("slice", "slices", "symbol", "symbols", "universe"):
        raise HTTPException(status_code=422, detail="mode must be slice or symbol")
    try:
        criteria = {
            "min_n": min_n_i,
            "min_wilson_low": round(min_wilson_f, 4),
            "target": 0.70,
            "prob_floor": 0.70,
            "days": days_i,
            "limit": limit_i,
            "mode": "slice" if mode_i in ("slice", "slices") else "symbol",
        }
        if mode_i in ("slice", "slices"):
            from core.contract_70_registry import register_slices
            from core.contract_70_slices import contract_70_slice_search

            search = contract_70_slice_search(days=days_i, min_n=min_n_i, min_wilson_low=min_wilson_f, limit=limit_i)
            qualified = search.get("qualified") or []
            if not qualified:
                return {
                    "ok": True,
                    "registered": False,
                    "status": "no_qualified_slice",
                    "criteria": criteria,
                    "candidate_count": 0,
                    "best_per_dimension": search.get("best_per_dimension") or [],
                    "note": "No slice currently has enough resolved evidence with Wilson lower bound >= the registration bar; forward proof was not started.",
                }
            # Register the strongest proven slice only. Freezing all overlapping
            # qualified slices would double-count future rows and blur the proof.
            picked = [qualified[0]]
            payload = register_slices(picked, min_n=min_n_i, min_wilson_low=min_wilson_f)
            return {
                "ok": True,
                "registered": True,
                "status": "registered_slice",
                "criteria": criteria,
                "candidate_count": len(picked),
                "slices": [{"dims": s.get("dims") or [], "key": s.get("key") or {}} for s in picked],
                "registry": payload,
                "note": "Forward proof started. Only future resolved rows matching the frozen slice count.",
            }

        from core.contract_70_registry import register_universe, select_candidate_universe
        from core.watcher import watcher_summary

        summary = watcher_summary(days=days_i, limit=limit_i)
        contract = ((summary.get("shadow_calibration") or {}).get("contract_70") or {})
        symbols = contract.get("symbols") or []
        picked = select_candidate_universe(
            symbols,
            min_n=min_n_i,
            min_wilson_low=min_wilson_f,
        )
        if not picked:
            return {
                "ok": True,
                "registered": False,
                "status": "no_qualified_symbols",
                "criteria": criteria,
                "candidate_count": 0,
                "available_symbols": symbols,
                "note": "No symbol currently has enough own 70+ evidence with Wilson lower bound >= the registration bar; forward proof was not started.",
            }

        payload = register_universe(picked, min_n=min_n_i, min_wilson_low=min_wilson_f)
        return {
            "ok": True,
            "registered": True,
            "status": "registered_symbols",
            "criteria": criteria,
            "candidate_count": len(picked),
            "symbols": picked,
            "registry": payload,
            "note": "Forward proof started. Only future resolved 70+ rows for the frozen universe count.",
        }
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse({"ok": False, "registered": False, "error": str(e)[:200]}, status_code=500)

@router.get("/api/watcher/contract-70/slices")
def watcher_contract_70_slices_endpoint(
    days: int = 120,
    min_n: int = 8,
    min_wilson_low: float = 0.70,
    limit: int = 20000,
):
    """Read-only 70+ slice search over resolved contract outcomes.

    Groups the SAME TP/SL win-test rows the contract already scores by symbol,
    market regime, and probability band, and reports which conditional slices
    (if any) clear a Wilson-proven 0.70. A qualified slice is a CANDIDATE to
    pre-register for a forward proof, not a 70+ claim. Never fires, never
    loosens a gate, never mutates state.
    """
    try:
        from core.contract_70_slices import contract_70_slice_search
        return contract_70_slice_search(
            days=days, min_n=min_n, min_wilson_low=min_wilson_low, limit=limit
        )
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


@router.get("/api/squeeze/hunter/board")
@router.get("/api/squeeze/hunter/scan", deprecated=True)
def squeeze_hunter_scan_endpoint(limit: int = 20):
    """Return the last completed Hunter board without calling market vendors."""
    try:
        from core.squeeze_hunter import get_hunter_snapshot

        return get_hunter_snapshot(limit=max(1, min(100, int(limit))))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.post("/api/squeeze/hunter/refresh")
def squeeze_hunter_refresh_endpoint(x_cron_secret: str = Header(default="")):
    """Cron-gated board refresh; public traffic cannot trigger provider work."""
    from wolf_app import _cron_ok

    if not _cron_ok(x_cron_secret, strict=True):
        raise HTTPException(status_code=403)
    try:
        from core.squeeze_hunter import refresh_hunter_snapshot

        return refresh_hunter_snapshot(limit=20)
    except Exception:
        return JSONResponse({"ok": False, "error": "refresh_failed"}, status_code=503)


@router.get("/api/bull-run/checklist/{symbol}")
def bull_run_checklist_endpoint(symbol: str):
    """Live, read-only evidence for a registered bull-run scenario.

    Only event-safe market observations are auto-filled. Earnings values remain
    pending until period-, unit-, source-, and timestamp-validated evidence is
    submitted to the evaluate endpoint.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return JSONResponse({"ok": False, "error": "symbol required"}, status_code=400)
    try:
        from core.bull_run_checklist import UnsupportedScenarioError
        from core.bull_run_ledger import BullRunDatabaseError, current_scenario_report
        return current_scenario_report(sym)
    except UnsupportedScenarioError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)
    except BullRunDatabaseError:
        return JSONResponse({"ok": False, "error": "database_unavailable"}, status_code=503)
    except Exception:
        return JSONResponse({"ok": False, "error": "service_unavailable"}, status_code=503)


@router.post("/api/bull-run/checklist/{symbol}/evaluate")
def bull_run_checklist_evaluate_endpoint(
    symbol: str,
    payload: dict = Body(...),
):
    """Evaluate sourced operator evidence without persisting or trading on it."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return JSONResponse({"ok": False, "error": "symbol required"}, status_code=400)
    try:
        from core.bull_run_checklist import (
            ChecklistInputError,
            UnsupportedScenarioError,
            validate_operator_payload,
        )
        from core.bull_run_ledger import BullRunDatabaseError, current_scenario_report

        evidence = validate_operator_payload(sym, payload)
        return current_scenario_report(sym, operator_evidence=evidence)
    except UnsupportedScenarioError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)
    except ChecklistInputError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except BullRunDatabaseError:
        return JSONResponse({"ok": False, "error": "database_unavailable"}, status_code=503)
    except Exception:
        return JSONResponse({"ok": False, "error": "service_unavailable"}, status_code=503)


@router.post("/api/bull-run/checklist/{symbol}/snapshot")
def bull_run_checklist_snapshot_endpoint(
    symbol: str,
    request: Request,
    payload: dict = Body(...),
    x_cron_secret: str = Header(default=""),
):
    """Persist validated operator evidence as an immutable audit snapshot."""
    from wolf_app import _ADMIN_COOKIE, _admin_token_valid, _cron_ok

    if not (
        _cron_ok(x_cron_secret, strict=True)
        or _admin_token_valid(request.cookies.get(_ADMIN_COOKIE, ""))
    ):
        raise HTTPException(status_code=404)
    sym = (symbol or "").strip().upper()
    try:
        from core.bull_run_checklist import (
            ChecklistInputError,
            UnsupportedScenarioError,
            validate_operator_payload,
        )
        from core.bull_run_ledger import BullRunDatabaseError, capture_snapshot

        evidence = validate_operator_payload(sym, payload)
        return capture_snapshot(operator_evidence=evidence)
    except UnsupportedScenarioError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)
    except ChecklistInputError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except BullRunDatabaseError:
        return JSONResponse({"ok": False, "error": "database_unavailable"}, status_code=503)
    except Exception:
        return JSONResponse({"ok": False, "error": "service_unavailable"}, status_code=503)


@router.get("/api/bull-run/checklist/{symbol}/ledger")
def bull_run_checklist_ledger_endpoint(symbol: str, limit: int = 50):
    """Read the immutable scenario timeline and eventual five-day outcome."""
    if (symbol or "").strip().upper() != "YMM":
        return JSONResponse({"ok": False, "error": "No registered bull-run scenario"}, status_code=404)
    try:
        from core.bull_run_ledger import recent_snapshots

        result = recent_snapshots(limit=max(1, min(200, int(limit))))
        if not result.get("ok", False):
            return JSONResponse({"ok": False, "error": "database_unavailable", "rows": []}, status_code=503)
        return result
    except Exception:
        return JSONResponse({"ok": False, "error": "database_unavailable", "rows": []}, status_code=503)


@router.post("/api/bull-run/checklist/snapshot-run")
def bull_run_checklist_snapshot_run_endpoint(x_cron_secret: str = Header(default="")):
    """Cron-gated manual run of the preregistered phase sampler."""
    from wolf_app import _cron_ok

    if not _cron_ok(x_cron_secret, strict=True):
        raise HTTPException(status_code=403)
    from core.bull_run_ledger import run_snapshot_job

    try:
        result = run_snapshot_job()
        if not result.get("ok", False):
            return JSONResponse({"ok": False, "error": "service_unavailable"}, status_code=503)
        return result
    except Exception:
        return JSONResponse({"ok": False, "error": "database_unavailable"}, status_code=503)


@router.post("/api/bull-run/checklist/resolve")
def bull_run_checklist_resolve_endpoint(x_cron_secret: str = Header(default="")):
    """Cron-gated five-trading-day outcome resolver."""
    from wolf_app import _cron_ok

    if not _cron_ok(x_cron_secret, strict=True):
        raise HTTPException(status_code=403)
    from core.bull_run_ledger import resolve_scenario

    try:
        result = resolve_scenario()
        if not result.get("ok", False):
            return JSONResponse({"ok": False, "error": "service_unavailable"}, status_code=503)
        return result
    except Exception:
        return JSONResponse({"ok": False, "error": "database_unavailable"}, status_code=503)


@router.get("/api/ghost/checklist/spec")
def checklist_spec_endpoint():
    """Static description of every checklist box and veto — for the UI and docs."""
    try:
        from core.catalyst_checklist import checklist_spec
        return checklist_spec()
    except Exception:
        return JSONResponse({"ok": False, "error": "spec_unavailable"}, status_code=503)


@router.get("/api/ghost/checklist/record")
def checklist_global_record_endpoint(limit: int = 30):
    """Every symbol's recently resolved calls, newest first — the Record tab."""
    try:
        from core.checklist_ledger import recent_resolved_across_symbols
        return {"ok": True, "snapshots": recent_resolved_across_symbols(limit=limit)}
    except Exception:
        return JSONResponse({"ok": False, "error": "database_unavailable"}, status_code=503)


@router.post("/api/ghost/checklist/snapshot-run")
def checklist_snapshot_run_endpoint(x_cron_secret: str = Header(default="")):
    """Retired: delayed current-state reconstruction is not issue-time evidence."""
    from wolf_app import _cron_ok

    if not _cron_ok(x_cron_secret, strict=True):
        raise HTTPException(status_code=403)
    return JSONResponse(
        {"ok": False, "error": "retired", "reason": "checklists_are_frozen_at_prediction_issuance"},
        status_code=410,
    )


@router.post("/api/ghost/checklist/resolve")
def checklist_resolve_endpoint(x_cron_secret: str = Header(default="")):
    """Cron-gated manual trigger for the checklist outcome resolver."""
    from wolf_app import _cron_ok

    if not _cron_ok(x_cron_secret, strict=True):
        raise HTTPException(status_code=403)
    try:
        from core.checklist_ledger import resolve_open_snapshots
        return resolve_open_snapshots()
    except Exception:
        return JSONResponse({"ok": False, "error": "database_unavailable"}, status_code=503)


@router.get("/api/ghost/checklist/prediction/{prediction_id}")
def checklist_prediction_endpoint(prediction_id: int):
    """Immutable issue-time checklist for a specific issued prediction."""
    try:
        from core.checklist_calibration import build_calibration, confidence_for
        from core.checklist_evidence import sources_for
        from core.checklist_ledger import resolved_samples_for_calibration, snapshot_for_prediction

        snapshot = snapshot_for_prediction(prediction_id)
        if snapshot is None:
            return JSONResponse({"ok": False, "error": "snapshot_not_found"}, status_code=404)
        frozen_evidence = snapshot.get("evidence") or {}
        frozen_sources = sources_for(frozen_evidence)
        cohort = {
            "checklist_version": snapshot["checklist_version"],
            "hold_bars": snapshot["hold_bars"],
            "outcome_contract": snapshot["outcome_contract"],
            "direction": snapshot["direction"],
            "symbol": None,
            "scope": "global",
        }
        samples = resolved_samples_for_calibration(
            checklist_version=cohort["checklist_version"],
            hold_bars=cohort["hold_bars"],
            outcome_contract=cohort["outcome_contract"],
            direction=cohort["direction"],
            min_issued_before=snapshot["issued_at"],
        )
        calibration = build_calibration(samples, cohort=cohort)
        report = dict(snapshot.get("report") or {})
        for group in report.get("groups", []):
            for member in group.get("boxes", []):
                if not member.get("source"):
                    member["source"] = frozen_sources.get(member.get("signal"))
        report["confidence"] = confidence_for(snapshot["score_pct"], calibration)
        report["confidence"]["cohort"] = cohort
        return {
            "ok": True,
            "snapshot_semantics": "immutable_at_prediction_issuance",
            "prediction_id": prediction_id,
            "issued_at": snapshot["issued_at"],
            "evidence": frozen_evidence,
            **report,
        }
    except Exception:
        return JSONResponse({"ok": False, "error": "database_unavailable"}, status_code=503)


@router.get("/api/ghost/checklist/{symbol}")
def checklist_endpoint(symbol: str, direction: str = "UP"):
    """Today's checklist read for one symbol/direction, with calibrated confidence.

    score_pct is checklist completeness, never a probability by itself.
    confidence_pct is filled in only once that completeness band has enough
    resolved history behind it (min_band_samples); until then it is null and
    the UI must render the plain-English explanation instead, never fall back
    to displaying score_pct as if it were confidence.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return JSONResponse({"ok": False, "error": "symbol required"}, status_code=400)
    direction = (direction or "UP").strip().upper()
    if direction not in ("UP", "DOWN"):
        return JSONResponse({"ok": False, "error": "direction must be UP or DOWN"}, status_code=400)
    try:
        from core.catalyst_checklist import evaluate_checklist
        from core.checklist_calibration import build_calibration, confidence_for
        from core.checklist_evidence import collect_evidence, sources_for
        from core.checklist_ledger import resolved_samples_for_calibration

        evidence = collect_evidence(sym)
        report = evaluate_checklist(sym, direction, evidence)
        evidence_sources = sources_for(evidence)
        for box in report.get("groups", []):
            for member in box.get("boxes", []):
                member["source"] = evidence_sources.get(member.get("signal"))

        try:
            from core.checklist_ledger import DEFAULT_OUTCOME_CONTRACT

            cohort = {
                "checklist_version": report["checklist_version"],
                "hold_bars": report["hold_bars"],
                "outcome_contract": DEFAULT_OUTCOME_CONTRACT,
                "direction": direction,
                "symbol": None,
                "scope": "global",
            }
            samples = resolved_samples_for_calibration(
                checklist_version=cohort["checklist_version"],
                hold_bars=cohort["hold_bars"],
                outcome_contract=cohort["outcome_contract"],
                direction=cohort["direction"],
            )
            calibration = build_calibration(samples, cohort=cohort)
            calibration_status = "ok"
        except Exception:
            calibration = None  # DB unavailable — still return the live checklist itself
            calibration_status = "unavailable"

        confidence = confidence_for(report["score_pct"], calibration)
        confidence["cohort"] = (calibration or {}).get("cohort")
        if calibration is None:
            # A DB failure must not read as "this band has never resolved."
            confidence["confidence_pct"] = None
            confidence["proven"] = False
            confidence["explanation"] = "Calibration history is temporarily unavailable."
        confidence["calibration_status"] = calibration_status
        report["confidence"] = confidence
        report["evidence"] = evidence
        report["snapshot_semantics"] = "live_now_not_issued_snapshot"
        return {"ok": True, "symbol": sym, **report}
    except Exception:
        LOGGER = logging.getLogger("ghost.api.checklist")
        LOGGER.exception("checklist_endpoint failed for %s", sym)
        return JSONResponse({"ok": False, "error": "checklist_unavailable"}, status_code=503)


@router.get("/api/ghost/checklist/{symbol}/calibration")
def checklist_calibration_endpoint(symbol: str, direction: str = "UP", scope: str = "global"):
    """Exact-cohort reliability table; scope is explicitly global or symbol."""
    sym = (symbol or "").strip().upper()
    direction = (direction or "UP").strip().upper()
    scope = (scope or "global").strip().lower()
    if direction not in ("UP", "DOWN"):
        return JSONResponse({"ok": False, "error": "direction must be UP or DOWN"}, status_code=400)
    if scope not in ("global", "symbol"):
        return JSONResponse({"ok": False, "error": "scope must be global or symbol"}, status_code=400)
    try:
        from core.catalyst_checklist import CHECKLIST_VERSION, HOLD_BARS
        from core.checklist_calibration import build_calibration, calibration_gap
        from core.checklist_ledger import DEFAULT_OUTCOME_CONTRACT, resolved_samples_for_calibration

        cohort = {
            "checklist_version": CHECKLIST_VERSION,
            "hold_bars": HOLD_BARS,
            "outcome_contract": DEFAULT_OUTCOME_CONTRACT,
            "direction": direction,
            "symbol": sym if scope == "symbol" else None,
            "scope": scope,
        }
        samples = resolved_samples_for_calibration(
            checklist_version=CHECKLIST_VERSION,
            hold_bars=HOLD_BARS,
            outcome_contract=DEFAULT_OUTCOME_CONTRACT,
            direction=direction,
            symbol=cohort["symbol"],
        )
        calibration = build_calibration(samples, cohort=cohort)
        return {"ok": True, "calibration": calibration, "gap": calibration_gap(calibration)}
    except Exception:
        return JSONResponse({"ok": False, "error": "database_unavailable"}, status_code=503)


@router.get("/api/ghost/checklist/{symbol}/record")
def checklist_record_endpoint(symbol: str, limit: int = 20):
    """Resolved and open checklist history for one symbol — the Record tab."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return JSONResponse({"ok": False, "error": "symbol required"}, status_code=400)
    try:
        from core.checklist_ledger import recent_snapshots
        return {"ok": True, "symbol": sym, "snapshots": recent_snapshots(sym, limit=limit)}
    except Exception:
        return JSONResponse({"ok": False, "error": "database_unavailable"}, status_code=503)


@router.get("/api/squeeze/hunter/ledger")
def squeeze_hunter_ledger_endpoint(symbol: str = "", limit: int = 50):
    """Read the Squeeze Hunter's point-in-time audit trail.

    Append-only evaluations + resolutions. This is the raw evidence a future
    calibration step consumes — it does NOT itself claim any accuracy.
    """
    try:
        from core.squeeze_hunter_ledger import recent_evaluations
        result = recent_evaluations(symbol=(symbol or "").strip().upper() or None,
                                    limit=max(1, min(200, int(limit))))
        if not result.get("ok", False):
            return JSONResponse({"ok": False, "error": "database_unavailable", "rows": []}, status_code=503)
        return result
    except Exception:
        return JSONResponse({"ok": False, "error": "database_unavailable", "rows": []}, status_code=503)


@router.post("/api/squeeze/hunter/resolve")
def squeeze_hunter_resolve_endpoint(x_cron_secret: str = Header(default="")):
    """Manually resolve pending Hunter evaluations (ops/backfill). Cron-gated."""
    from wolf_app import _cron_ok  # late import — shared state + monkeypatch-safe
    if not _cron_ok(x_cron_secret, strict=True):
        raise HTTPException(status_code=403)
    try:
        from core.squeeze_hunter_ledger import resolve_hunter_predictions
        result = resolve_hunter_predictions(limit=200)
        if not result.get("ok", False):
            return JSONResponse({"ok": False, "error": "database_unavailable"}, status_code=503)
        return result
    except Exception:
        return JSONResponse({"ok": False, "error": "database_unavailable"}, status_code=503)


@router.get("/api/squeeze/hunter/{symbol}")
def squeeze_hunter_endpoint(symbol: str):
    """GHOST SQUEEZE HUNTER — fuel/trigger/confirmation + pressure score +
    7-stage lifecycle + explosion projection for one symbol.

    Read-only intelligence from the completed scheduled snapshot. Public
    requests never call market providers or persist calibration samples.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return JSONResponse({"ok": False, "error": "symbol required"}, status_code=400)
    try:
        from core.squeeze_hunter import get_hunter_symbol_snapshot

        result = get_hunter_symbol_snapshot(sym)
        if not result.get("ok", False):
            return JSONResponse(result, status_code=404 if result.get("status") == "not_in_snapshot" else 503)
        return result
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


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


@router.get("/api/symbol/timeline")
def symbol_timeline_endpoint(symbol: str = "WOLF"):
    """Unified per-symbol detection timeline (squeeze + observations + news).

    Makes the full detection history visible in one place — the ARCT post-mortem
    showed the radar caught the move but the UI hid it. Read-only.
    """
    try:
        from core.symbol_timeline import build_symbol_timeline
        result = build_symbol_timeline(symbol)
        if result.get("status") == "unavailable":
            return JSONResponse(result, status_code=503)
        return result
    except ValueError:
        return JSONResponse(
            {"ok": False, "status": "invalid_request", "error": "invalid_symbol"},
            status_code=422,
        )
    except Exception:
        return JSONResponse(
            {"ok": False, "status": "error", "error": "timeline_service_error"},
            status_code=500,
        )


@router.get("/api/intelligence/external-discovery")
def external_discovery_snapshot(limit: int = Query(default=50, ge=1, le=200)):
    """Read persisted advisory discovery; never poll an external provider."""
    try:
        from core.external_context_ledger import recent_external_discoveries
        return recent_external_discoveries(limit=limit)
    except Exception:
        logging.getLogger("ghost.api.external_context").exception(
            "external discovery snapshot unavailable"
        )
        return JSONResponse({
            "ok": False, "status": "unavailable",
            "error": "external_discovery_unavailable",
            "advisory_only": True, "decision_eligible": False,
        }, status_code=503)


@router.get("/api/intelligence/external-radar")
def external_radar_snapshot():
    """Read batch-enriched external observations; never poll providers."""
    try:
        from core.external_context_ledger import latest_external_radar_snapshot
        result = latest_external_radar_snapshot()
        if result.get("status") == "unavailable":
            return JSONResponse(result, status_code=503)
        return result
    except Exception:
        logging.getLogger("ghost.api.external_context").exception(
            "external radar snapshot unavailable"
        )
        return JSONResponse({
            "ok": False, "status": "unavailable", "items": [],
            "error": "external_radar_unavailable",
            "advisory_only": True, "decision_eligible": False,
        }, status_code=503)


@router.get("/api/intelligence/broad-market")
def broad_market_snapshot():
    """Read the leader-refreshed display-only broad-market snapshot."""
    try:
        from core.broad_market_context import get_broad_market_context
        result = get_broad_market_context()
        if not result.get("ok"):
            return JSONResponse(result, status_code=503)
        return result
    except Exception:
        logging.getLogger("ghost.api.external_context").exception(
            "broad market snapshot unavailable"
        )
        return JSONResponse({
            "ok": False, "status": "unavailable",
            "error": "broad_market_context_unavailable",
            "display_only": True, "decision_eligible": False,
        }, status_code=503)


@router.get("/api/explosion/benchmark")
def explosion_benchmark_endpoint():
    """Preregistered explosion-event detection benchmark (recall, not precision).

    Reports per-tier recall of +20/30/50/100% events and whether Ghost observed
    them before +10%/+20%. Read-only; never blocks a pick.
    """
    try:
        from core.explosion_benchmark import benchmark_summary
        return benchmark_summary()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/data/quorum")
def data_quorum_endpoint(symbol: str = "WOLF"):
    """Cached advisory multi-provider corroboration for one equity symbol."""
    try:
        from core.data_quorum import evaluate_quorum
        return evaluate_quorum(symbol, use_cache=True)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=422)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=503)


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
