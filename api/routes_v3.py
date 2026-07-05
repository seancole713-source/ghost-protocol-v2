"""api/routes_v3.py — endpoint group split out of wolf_app.py (PR #130).

Endpoint bodies late-import shared helpers from wolf_app at request time so
tests that monkeypatch wolf_app attributes (db_conn, _cron_ok, ...) keep
working, and so this module never imports wolf_app at import time (no cycle).
wolf_app re-exports every endpoint name for backward compatibility.
"""
import os, sys, time, json, logging, threading, hmac, math, asyncio, base64  # noqa: F401,E401

from fastapi import APIRouter, Header, HTTPException, Request, Depends  # noqa: F401
from fastapi.responses import JSONResponse, HTMLResponse, Response, PlainTextResponse  # noqa: F401

router = APIRouter()

@router.post("/api/v3/backtest")
def v3_backtest(x_cron_secret: str = Header(default=""), symbol: str = "WOLF", asset_type: str = "stock"):
    """
    Historical samples for v3 training: TP/SL WIN before stop within N daily bars
    (same rules as live reconcile / core.vol_targets).
    """
    from wolf_app import _cron_ok  # late import — shared state + monkeypatch-safe
    if not _cron_ok(x_cron_secret):
        return JSONResponse({"ok":False,"error":"Forbidden"}, status_code=403)
    try:
        from core.signal_engine import backtest_symbol, V3_LABEL_HOLD_BARS, LABEL_TYPE
        from core.vol_targets import base_vol_pct
        up_rows, down_rows = backtest_symbol(symbol, asset_type)
        rows = up_rows  # primary analysis on UP rows
        if not rows and not down_rows:
            return {"ok": False, "error": "No data for " + symbol}
        total = len(rows)
        hits = sum(1 for r in rows if r['label'] == 1)
        expired = sum(1 for r in rows if r.get('outcome') == 'EXPIRED')
        losses = sum(1 for r in rows if r.get('outcome') == 'LOSS')
        # DOWN stats
        down_total = len(down_rows)
        down_hits = sum(1 for r in down_rows if r['label'] == 1) if down_rows else 0
        vol_pct = base_vol_pct(symbol, asset_type)
        indicators = {
            'rsi_oversold': lambda f: f.get('rsi_oversold', 0) == 1,
            'macd_bullish': lambda f: f.get('macd_bullish', 0) == 1,
            'near_low': lambda f: f.get('near_low', 0) == 1,
            'volume_spike': lambda f: f.get('volume_spike', 0) == 1,
            'all_signals': lambda f: f.get('rsi_oversold',0)==1 and f.get('macd_bullish',0)==1,
        }
        results = {}
        for name, fn in indicators.items():
            fired = [r for r in rows if fn(r['features'])]
            if fired:
                acc = sum(1 for r in fired if r['label']==1) / len(fired)
                results[name] = {"fired": len(fired), "tp_sl_win_pct": round(acc*100,1)}
        return {
            "ok": True, "symbol": symbol, "total_samples": total,
            "label_type": LABEL_TYPE,
            "natural_tp_sl_win_pct": round(hits/total*100,1) if total else 0,
            "outcome_mix_pct": {
                "WIN": round(hits/total*100,1) if total else 0,
                "LOSS": round(losses/total*100,1) if total else 0,
                "EXPIRED": round(expired/total*100,1) if total else 0,
            },
            "down_samples": down_total,
            "down_natural_tp_sl_win_pct": round(down_hits/down_total*100,1) if down_total else 0,
            "vol_target_frac": vol_pct,
            "label_lookahead_daily_bars": V3_LABEL_HOLD_BARS,
            "indicators": results,
        }
    except Exception as e:
        return JSONResponse({"ok":False,"error":str(e)}, status_code=500)


@router.get("/api/v3/explain/{symbol}")
def v3_explain(symbol: str):
    """SHAP waterfall for the latest prediction on a symbol (P2-5 audit).

    Returns the top 5 features driving the model's up_prob, ranked by
    SHAP magnitude. Requires the trained model to be loadable and the
    latest feature snapshot to exist. Public, read-only.
    """
    from wolf_app import db_conn  # late import — shared state + monkeypatch-safe
    try:
        sym = (symbol or "WOLF").upper()
        from core.signal_engine import load_model, FEATURE_COLS
        model, feature_cols, meta = load_model(sym)
        if model is None:
            return {"ok": False, "error": f"no loadable model for {sym}", "symbol": sym}
        # Get latest feature snapshot
        import json as _j
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT payload FROM ghost_feature_snapshots "
                "WHERE symbol = %s AND payload IS NOT NULL "
                "ORDER BY feature_asof_ts DESC LIMIT 1",
                (sym,),
            )
            row = cur.fetchone()
        if not row or not row[0]:
            return {"ok": False, "error": f"no feature snapshot for {sym}", "symbol": sym}
        snap = row[0] if isinstance(row[0], dict) else _j.loads(row[0])
        # Build feature vector in the model's column order
        vec = []
        for col in feature_cols:
            vec.append(float(snap.get(col, 0.0)))
        import numpy as np
        X = np.array([vec])
        up_prob = float(model.predict_proba(X)[0][1])
        # SHAP waterfall (top 5 features by magnitude)
        shap_values = []
        try:
            import shap
            explainer = shap.TreeExplainer(model)
            shap_vals = explainer.shap_values(X)
            if isinstance(shap_vals, list):
                sv = shap_vals[1][0] if len(shap_vals) > 1 else shap_vals[0][0]
            else:
                sv = shap_vals[0]
            base = float(explainer.expected_value[1]) if isinstance(explainer.expected_value, list) else float(explainer.expected_value)
            for i, col in enumerate(feature_cols):
                shap_values.append({
                    "feature": col,
                    "value": round(float(vec[i]), 6),
                    "shap": round(float(sv[i]), 6),
                    "abs_shap": round(abs(float(sv[i])), 6),
                })
            shap_values.sort(key=lambda x: x["abs_shap"], reverse=True)
        except ImportError:
            shap_values = [{"feature": "shap_unavailable", "note": "pip install shap"}]
        except Exception as _se:
            shap_values = [{"feature": "shap_error", "note": str(_se)[:120]}]
        return {
            "ok": True,
            "symbol": sym,
            "up_prob": round(up_prob, 4),
            "base_value": round(base, 4) if 'base' in dir() else None,
            "feature_cols": feature_cols,
            "top_features": shap_values[:5],
            "all_features": shap_values,
            "model_meta": {
                "accuracy": meta.get("accuracy"),
                "trained_at": meta.get("trained_at"),
                "calibrated": meta.get("calibrated", False),
            },
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/v3/lineage")
def v3_lineage(limit: int = 50):
    """Model lineage (audit) — rolling history of training runs (accuracy/edge/
    pass per symbol) so /admin can show how the model evolved across retrains.
    Newest first. Public, read-only."""
    from wolf_app import db_conn, ensure_ghost_state  # late import — shared state + monkeypatch-safe
    try:
        import json as _j
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_ghost_state(cur)
            cur.execute("SELECT val FROM ghost_state WHERE key='model_lineage'")
            row = cur.fetchone()
        hist = []
        if row and row[0]:
            try:
                hist = _j.loads(row[0])
            except Exception:
                hist = []
        if not isinstance(hist, list):
            hist = []
        lim = max(1, min(200, int(limit)))
        recent = list(reversed(hist))[:lim]
        return {"ok": True, "count": len(recent), "runs": recent}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/v3/status")
def v3_status():
    """Full system-health snapshot (audit), built on the v3 model status.

    Preserves the model-status contract (trained / models / symbols / accuracy)
    that /admin and health_audit consume, and adds a `system` block: engine
    heartbeat, kill-condition + pause state, coverage, recent activity, realized
    P&L, and a healthy/degraded/critical roll-up.

    Exposes full model coverage and includes watchlist coverage telemetry so
    operators can see which configured symbols are still missing a trained model.
    """
    from wolf_app import _v3_system_health, _v3_train_collect_symbols  # late import — shared state + monkeypatch-safe
    from core.signal_engine import get_model_status
    st = get_model_status() or {}
    syms = st.get("symbols") or {}
    st["symbols"] = {str(k).upper(): v for k, v in syms.items()}
    st["models"] = len(st["symbols"])
    expected = sorted({sym for sym, _atype in _v3_train_collect_symbols()})
    available = set(st["symbols"].keys())
    st["watchlist_expected_symbols"] = expected
    st["watchlist_missing_models"] = [sym for sym in expected if sym not in available]
    gate = st.get("last_train_gate") or {}
    if gate:
        st["last_train_gate_passed"] = gate.get("gate_passed")
        st["last_train_gate_attempted"] = gate.get("gate_attempted")
    st["system"] = _v3_system_health(st)
    if isinstance(st.get("system"), dict):
        coverage = st["system"].setdefault("coverage", {})
        coverage["expected_symbols"] = expected
        coverage["missing_models"] = st["watchlist_missing_models"]
    return st


@router.post("/api/v3/train")
def v3_train(x_cron_secret: str = Header(default=""), force: bool = False):
    """
    Train v3 XGBoost model on 1yr historical data (watchlist-aware).
    Takes 2-5 minutes. Runs in background, returns immediately.
    Model only deployed if accuracy > 52% on holdout (and the rest of
    the v3.2 quality gates pass: walk-forward, edge, min wins).

    `force` is currently a no-op safety flag — v3_train has no cooldown
    or lock of its own, so manual invocations always run regardless.
    It's reserved for future use if a guard is ever added and signals
    operator intent to bypass any such guard. The scheduler-driven
    _weekly_retrain has its own 7-day cooldown which is unrelated.

    PR #14 diag: emits PR14_DIAG markers at endpoint entry, background-
    thread start, and post-train_and_validate so a missing link reveals
    exactly where the chain breaks in Railway logs.

    PR #18: also records per-phase state into ghost_state so the
    /api/v3/train/last endpoint can report the actual outcome of the
    most recent invocation (passed/failed + accuracy + error message).
    The cockpit's "Refresh Status" button reads this.
    """
    from wolf_app import LOGGER, _RETRAIN_JOB_LOCK, _auto_purge_bad_models, _bump_cockpit_db_cache, _cron_ok, _purge_v3_stale_or_weak, _record_v3_train_state, _v3_train_collect_symbols  # late import — shared state + monkeypatch-safe
    LOGGER.info(f"[v3_train] PR14_DIAG ENDPOINT_INVOKED force={force}")
    if not _cron_ok(x_cron_secret):
        return JSONResponse({"ok":False,"error":"Forbidden"}, status_code=403)
    # PR #80: prevent concurrent training jobs from racing model writes
    if _RETRAIN_JOB_LOCK.locked():
        return JSONResponse({"ok": False, "error": "training_already_running"}, status_code=409)
    _RETRAIN_JOB_LOCK.acquire()
    started_at = int(time.time())
    _record_v3_train_state(
        ts=started_at, state="started", force=str(force).lower(),
        accuracy="", passed="", error="", models_before="", models_after="",
    )
    import threading
    def _train():
        try:
            LOGGER.info("[v3_train] PR14_DIAG BG_THREAD_STARTED importing train_and_validate")
            from core.signal_engine import train_and_validate, get_model_status
            stocks = _v3_train_collect_symbols()
            try:
                models_before = int((get_model_status() or {}).get("models", 0))
            except Exception:
                models_before = 0
            _record_v3_train_state(state="running", stocks=str(stocks), models_before=models_before)
            LOGGER.info(f"[v3_train] PR14_DIAG calling train_and_validate(stocks={stocks})")
            model, accuracy, passed = train_and_validate(stocks)
            LOGGER.info(f"[v3_train] PR14_DIAG train_and_validate returned passed={passed} acc={accuracy}")
            LOGGER.info(f"v3 training complete: accuracy={round((accuracy or 0)*100,1)}% passed={passed}")
            _bump_cockpit_db_cache()
            try:
                purged = _auto_purge_bad_models()
                pv = _purge_v3_stale_or_weak()
                LOGGER.info(f"Post-train purge: legacy={purged} v3={pv}")
            except Exception as _pe:
                LOGGER.warning("Auto-purge after train failed: "+str(_pe)[:60])
            try:
                models_after = int((get_model_status() or {}).get("models", 0))
            except Exception:
                models_after = 0
            _record_v3_train_state(
                state="passed" if passed else "failed",
                accuracy=f"{(accuracy or 0):.4f}",
                passed=str(bool(passed)).lower(),
                models_after=models_after,
                finished_at=int(time.time()),
                error="",
            )
        except Exception as e:
            LOGGER.error("v3 training failed: " + str(e))
            _record_v3_train_state(
                state="exception",
                error=str(e)[:300],
                finished_at=int(time.time()),
            )
        finally:
            try:
                _RETRAIN_JOB_LOCK.release()
            except Exception:
                pass
    threading.Thread(target=_train, daemon=True).start()
    # PR #19: response now includes _pr_version so the operator can verify
    # from a single curl whether the deployed code is fresh or stale. If the
    # response is missing this field, Railway is serving a pre-PR-#19 version.
    return {"ok": True, "message": "Training started in background. Check /api/v3/train/last in 3-5 minutes.",
            "force": force, "started_at": started_at, "_pr_version": 19}


@router.get("/api/v3/train/last")
def v3_train_last():
    """Return the most recent v3_train invocation result from ghost_state.

    Public read-only — same convention as /api/v3/status. Returns a flat
    dict of the last_v3_train_* fields so the cockpit can render a
    "Last training result" panel without needing the cron secret.
    """
    from wolf_app import db_conn, ensure_ghost_state  # late import — shared state + monkeypatch-safe
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_ghost_state(cur)
            cur.execute(
                "SELECT key, val FROM ghost_state WHERE key LIKE 'last_v3_train_%'"
            )
            rows = cur.fetchall()
        out = {}
        for key, val in rows:
            short = key.replace("last_v3_train_", "", 1)
            out[short] = val
        # Coerce numeric fields when present
        for num_key in ("ts", "finished_at", "models_before", "models_after"):
            if num_key in out and out[num_key]:
                try:
                    out[num_key] = int(out[num_key])
                except Exception:
                    pass
        if "accuracy" in out and out["accuracy"]:
            try:
                out["accuracy"] = float(out["accuracy"])
            except Exception:
                pass
        if "passed" in out:
            out["passed"] = out["passed"].lower() == "true"
        if "force" in out:
            out["force"] = out["force"].lower() == "true"
        try:
            import json as _json
            with db_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT val FROM ghost_state WHERE key='last_train_details'")
                row = cur.fetchone()
            if row and row[0]:
                out["train_details"] = _json.loads(row[0])
        except Exception:
            pass
        return {"ok": True, "last": out or None}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.post("/api/v3/train/sync")
def v3_train_sync(x_cron_secret: str = Header(default=""), force: bool = False):
    """Synchronous v3 training — runs train_and_validate in the request
    thread and returns the actual outcome directly.

    Use this when the async /api/v3/train silently fails to produce a
    model and you can't tell why. The HTTP request blocks for the full
    training duration (typically 60-300s) but the response payload
    contains the definitive result: passed bool, accuracy, error string.

    Caveats:
      - HTTP client must allow long-running requests (Hoppscotch ok,
        browsers may timeout at 30s-2min depending on platform)
      - Holds a worker thread for the duration — don't call repeatedly
      - Still records the same ghost_state phase markers as the async
        endpoint, so /api/v3/train/last reflects this run too
    """
    from wolf_app import LOGGER, _RETRAIN_JOB_LOCK, _RUNNING_PR_VERSION, _auto_purge_bad_models, _bump_cockpit_db_cache, _cron_ok, _purge_v3_stale_or_weak, _record_v3_train_state, _v3_train_collect_symbols, db_conn  # late import — shared state + monkeypatch-safe
    LOGGER.info(f"[v3_train_sync] PR19_DIAG ENDPOINT_INVOKED force={force}")
    if not _cron_ok(x_cron_secret):
        return JSONResponse({"ok": False, "error": "Forbidden"}, status_code=403)
    # PR #80: prevent concurrent training jobs from racing model writes
    if _RETRAIN_JOB_LOCK.locked():
        return JSONResponse({"ok": False, "error": "training_already_running"}, status_code=409)
    _RETRAIN_JOB_LOCK.acquire()
    started_at = int(time.time())
    _record_v3_train_state(
        ts=started_at, state="started", force=str(force).lower(),
        accuracy="", passed="", error="", models_before="", models_after="",
    )
    try:
        from core.signal_engine import train_and_validate, get_model_status
        stocks = _v3_train_collect_symbols()
        try:
            models_before = int((get_model_status() or {}).get("models", 0))
        except Exception:
            models_before = 0
        _record_v3_train_state(state="running", stocks=str(stocks), models_before=models_before)
        LOGGER.info(f"[v3_train_sync] PR19_DIAG calling train_and_validate(stocks={stocks})")
        model, accuracy, passed = train_and_validate(stocks)
        LOGGER.info(f"[v3_train_sync] PR19_DIAG train_and_validate returned passed={passed} acc={accuracy}")
        _bump_cockpit_db_cache()
        try:
            purged = _auto_purge_bad_models()
            pv = _purge_v3_stale_or_weak()
            LOGGER.info(f"Post-train purge (sync): legacy={purged} v3={pv}")
        except Exception as _pe:
            LOGGER.warning("Auto-purge after sync train failed: " + str(_pe)[:60])
        try:
            models_after = int((get_model_status() or {}).get("models", 0))
        except Exception:
            models_after = 0
        finished_at = int(time.time())
        # PR #20: surface per-symbol gate-fail detail in the response so
        # the operator doesn't have to grep Railway logs for RETRAIN lines.
        # train_and_validate persists this to ghost_state.last_train_details.
        train_details = None
        try:
            import json as _json
            with db_conn() as _dc:
                _dcur = _dc.cursor()
                _dcur.execute("SELECT val FROM ghost_state WHERE key='last_train_details'")
                _drow = _dcur.fetchone()
                if _drow and _drow[0]:
                    train_details = _json.loads(_drow[0])
        except Exception as _de:
            LOGGER.warning("train detail read failed: " + str(_de)[:120])

        result = {
            "ok": True,
            "_pr_version": _RUNNING_PR_VERSION,
            "passed": bool(passed),
            "accuracy": round((accuracy or 0) * 100, 2),
            "stocks": stocks,
            "models_before": models_before,
            "models_after": models_after,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_s": finished_at - started_at,
            "train_details": train_details,
        }
        _record_v3_train_state(
            state="passed" if passed else "failed",
            accuracy=f"{(accuracy or 0):.4f}",
            passed=str(bool(passed)).lower(),
            models_after=models_after,
            finished_at=finished_at,
            error="",
        )
        return result
    except Exception as e:
        finished_at = int(time.time())
        err_str = str(e)[:300]
        LOGGER.error("v3_train_sync failed: " + err_str)
        _record_v3_train_state(
            state="exception",
            error=err_str,
            finished_at=finished_at,
        )
        return JSONResponse({
            "ok": False,
            "_pr_version": _RUNNING_PR_VERSION,
            "error": err_str,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_s": finished_at - started_at,
        }, status_code=500)


@router.post("/api/retrain")
def retrain(x_cron_secret: str = Header(default="")):
    """Train XGBoost on ghost_prediction_outcomes. Inline - no import needed."""
    from wolf_app import LOGGER, _cron_ok, db_conn  # late import — shared state + monkeypatch-safe
    if not _cron_ok(x_cron_secret):
        raise HTTPException(status_code=403)
    try:
        import xgboost as xgb, numpy as np, json as _json, time as _time
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT COALESCE(gpo.predicted_direction,'UP'), COALESCE(gpo.predicted_confidence,0.5),
                       gpo.price_at_prediction, gpo.realized_move_pct,
                       EXTRACT(EPOCH FROM gpo.created_at)::BIGINT,
                       gpo.symbol,
                       CASE WHEN gpo.hit_direction=1 THEN 1 ELSE 0 END
                FROM ghost_prediction_outcomes gpo
                WHERE gpo.hit_direction IN (0,1) AND gpo.price_at_prediction > 0
                ORDER BY gpo.created_at DESC LIMIT 5000
            """)
            rows = cur.fetchall()
        if len(rows) < 100:
            return JSONResponse({"ok": False, "error": "Only " + str(len(rows)) + " rows"}, status_code=400)
        import datetime as _dt, collections
        sym_wins = collections.defaultdict(lambda: [0,0])
        for row in rows:
            sym = row[5]
            sym_wins[sym][1] += 1
            if row[6] == 1: sym_wins[sym][0] += 1
        X, y = [], []
        for direction, conf, entry, pnl, ts, sym, label in rows:
            if not entry or entry <= 0: continue
            wr = sym_wins[sym][0]/sym_wins[sym][1] if sym_wins[sym][1] else 0.5
            sc = min(sym_wins[sym][1], 100) / 100
            pct = abs(pnl)/100 if pnl else 0.05
            h, dow = 0, 0
            if ts:
                dt = _dt.datetime.fromtimestamp(float(ts))
                h, dow = dt.hour, dt.weekday()
            X.append([float(conf), 1.0 if direction=="UP" else 0.0, 0.0,
                       float(pct), 0.03, float(pct)/0.03 if pct else 1.0,
                       float(wr), float(sc), float(min(entry,10000))/10000,
                       float(h)/24, float(dow)/7])
            y.append(label)
        X_np, y_np = np.array(X), np.array(y)
        split = int(len(X_np) * 0.8)
        model = xgb.XGBClassifier(n_estimators=200, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8, eval_metric="logloss", random_state=42)
        model.fit(X_np[:split], y_np[:split], eval_set=[(X_np[split:], y_np[split:])], verbose=False)
        val_acc = float(np.mean(model.predict(X_np[split:]) == y_np[split:]))
        train_acc = float(np.mean(model.predict(X_np[:split]) == y_np[:split]))
        model_path = "/tmp/ghost_v2.json"
        model.save_model(model_path)
        from core import prediction as _pred
        _pred._model = model
        meta = {"ok": True, "samples": len(X), "train_acc": round(train_acc*100,1),
                "val_acc": round(val_acc*100,1), "model_path": model_path}
        LOGGER.info("Retrain done: " + str(meta))
        return meta
    except Exception as e:
        LOGGER.error("Retrain error: " + str(e))
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
