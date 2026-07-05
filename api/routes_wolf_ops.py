"""api/routes_wolf_ops.py — endpoint group split out of wolf_app.py (PR #130).

Endpoint bodies late-import shared helpers from wolf_app at request time so
tests that monkeypatch wolf_app attributes (db_conn, _cron_ok, ...) keep
working, and so this module never imports wolf_app at import time (no cycle).
wolf_app re-exports every endpoint name for backward compatibility.
"""
import os, sys, time, json, logging, threading, hmac, math, asyncio, base64  # noqa: F401,E401

from fastapi import APIRouter, Header, HTTPException, Request, Depends  # noqa: F401
from fastapi.responses import JSONResponse, HTMLResponse, Response, PlainTextResponse  # noqa: F401

router = APIRouter()

@router.get("/api/wolf/gate-status")
def wolf_gate_status():
    """Live diagnostic of the prediction gating chain (PR #27).

    Surfaces, for the /admin monitor:
      - active objective mode + effective thresholds (target_wr,
        min_samples, bootstrap_min_conf, lookback_days) and whether
        the gate is enforced / auto-mode is on
      - the MIN_ALERT_CONFIDENCE floor
      - WOLF's resolved-pick stats (bootstrap vs established phase)
      - a LIVE model prediction for WOLF with per-gate pass/fail so the
        operator can see exactly where each cycle lands relative to the
        gates after the aggressive-mode env change.

    Read-only; runs the model once per call (~1-2s). No auth (same
    convention as /api/v3/status); the /admin page that consumes it is
    behind Basic Auth.
    """
    out = {"ok": True}
    try:
        from core import prediction as _pred
        cfg = _pred._objective_effective_config()
        enforced = _pred._objective_enforced()
        floor = _pred.CONFIDENCE_FLOOR
        out["objective"] = {
            "enforced": enforced,
            "auto_mode_enabled": _pred._objective_auto_enabled(),
            "mode": cfg.get("mode"),
            "target_wr": cfg.get("target_wr"),
            "min_samples": cfg.get("min_samples"),
            "bootstrap_min_conf": cfg.get("bootstrap_min_conf"),
            "lookback_days": cfg.get("lookback_days"),
        }
        out["confidence_floor"] = floor

        # WOLF resolved-pick stats → bootstrap vs established phase
        try:
            stats = _pred._objective_symbol_stats("WOLF", "UP")
            total = int(stats.get("combined_total", 0))
            out["symbol_stats"] = {
                "combined_total": total,
                "combined_wins": stats.get("combined_wins"),
                "combined_wr": stats.get("combined_wr"),
                "phase": "established" if total >= int(cfg["min_samples"]) else "bootstrap",
            }
        except Exception as e:
            out["symbol_stats"] = {"error": str(e)[:120]}

        # Live model prediction + per-gate analysis. Pass a scores dict so we can
        # surface up_prob and the binding threshold even on cycles that don't fire.
        try:
            from core.signal_engine import predict_live_ex
            _scores = {}
            signal, reason = predict_live_ex("WOLF", "stock", scores=_scores)
            lp = {"reason": reason}

            phase = (out.get("symbol_stats") or {}).get("phase")
            boot_conf = float(cfg.get("bootstrap_min_conf"))
            # Binding confidence requirement: in the bootstrap phase the objective
            # gate needs conf >= bootstrap_min_conf; the floor needs conf >= floor.
            binding_conf = max(float(floor), boot_conf) if phase == "bootstrap" else float(floor)
            up_prob = _scores.get("up_prob")
            mm = _scores.get("model_meta") or {}
            acc = mm.get("accuracy")
            min_p = mm.get("min_win_proba")
            lp["up_prob"] = up_prob
            lp["calibrated"] = bool(mm.get("calibrated", False))
            lp["calibration_method"] = mm.get("calibration_method")
            lp["regime"] = _scores.get("regime")
            lp["binding_confidence_threshold"] = round(binding_conf, 3)
            lp["bootstrap_min_conf"] = round(boot_conf, 3)
            # up_prob needed to clear the binding threshold, inverting
            # conf = clamp(accuracy + (up_prob - min_p) * CONFIDENCE_SLOPE, 0.75, 0.95):
            # P1-1 (audit): CONFIDENCE_SLOPE env-var replaces the heuristic 4.0 multiplier.
            # Default 4.0; calibrate against resolved picks to find the empirical slope.
            _conf_slope = float(os.getenv("CONFIDENCE_SLOPE", "4.0"))
            if acc is not None and min_p is not None:
                needed = min_p + (binding_conf - acc) / max(_conf_slope, 0.5)
                needed = max(needed, min_p)   # must also exceed min_p to emit UP
                lp["up_prob_needed_to_fire"] = round(needed, 4)
                if up_prob is not None:
                    lp["up_prob_gap"] = round(up_prob - needed, 4)

            if signal:
                direction, conf = signal
                conf = float(conf)
                passes_floor = conf >= float(floor)
                obj_ok, obj_skip = True, None
                if enforced:
                    obj_ok, obj_skip, _ = _pred._objective_gate("WOLF", direction, conf)
                sell_blocked = (direction == "DOWN")
                lp.update({
                    "direction": direction,
                    "confidence": round(conf, 3),
                    "model_emitted": True,
                    "passes_confidence_floor": passes_floor,
                    "passes_objective_gate": bool(obj_ok),
                    "objective_skip_reason": obj_skip,
                    "sell_blocked": sell_blocked,
                    "would_alert": bool(passes_floor and obj_ok and not sell_blocked),
                })
            else:
                lp.update({
                    "direction": None, "confidence": None, "model_emitted": False,
                    "would_alert": False,
                })
            out["live_prediction"] = lp
        except Exception as e:
            out["live_prediction"] = {"error": str(e)[:160]}
        return out
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/wolf/gate-history")
def wolf_gate_history(limit: int = 50):
    """Rolling per-cycle gate-outcome history (PR #29).

    Each prediction cycle records {ts, scanned, candidates, saved,
    dedup_blocked, would_fire, top_skip, skip_counts} to
    ghost_state.gate_outcome_history (last 50 cycles). This lets the
    operator review whether any recent cycle cleared the gates — and
    which gate was binding when none did — without watching the live
    monitor. Newest first. Read-only.
    """
    from wolf_app import db_conn, ensure_ghost_state  # late import — shared state + monkeypatch-safe
    try:
        import json as _j
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_ghost_state(cur)
            cur.execute("SELECT val FROM ghost_state WHERE key='gate_outcome_history'")
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
        recent = list(reversed(hist))[:lim]   # newest first
        fired = sum(1 for h in recent if h.get("would_fire"))
        # Aggregate which gate was binding across the window
        binding = {}
        closest = None   # best (highest up_prob) near-miss across the window
        from core.prediction import backfill_near_miss_for_display
        for h in recent:
            ts_skip = h.get("binding_skip") or h.get("top_skip")
            if ts_skip:
                binding[ts_skip] = binding.get(ts_skip, 0) + 1
            nm = backfill_near_miss_for_display(h.get("near_miss"))
            if nm:
                h["near_miss"] = nm
            if nm and nm.get("up_prob") is not None:
                if closest is None or nm["up_prob"] > closest.get("up_prob", -1):
                    closest = dict(nm, ts=h.get("ts"))
        return {
            "ok": True,
            "count": len(recent),
            "fired_count": fired,
            "binding_gates": binding,
            "closest_near_miss": closest,
            "history": recent,
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/wolf/kill-status")
def wolf_kill_status():
    """Live kill-condition dashboard (audit §2). Evaluates the env-tunable
    safety thresholds (win rate / Brier / consecutive losses / expectancy) over
    the rolling resolved-pick history and returns per-condition current-vs-
    threshold with a green/red/insufficient flag. Read-only — does not enforce.
    """
    from wolf_app import db_conn  # late import — shared state + monkeypatch-safe
    try:
        from core.db import pool_stats
        from core.prediction import evaluate_kill_conditions

        out = evaluate_kill_conditions(include_pause=True)
        if isinstance(out, dict):
            out["pool"] = pool_stats()
            # Honesty: this endpoint shows the ALL-TIME rolling window, but the
            # enforcer evaluates only outcomes since the last manual resume
            # (window reset). Surface that second view so a red all-time flag
            # with paused=false reads as "window reset", not "kill switch broken".
            try:
                resume_ts = 0
                with db_conn() as _c:
                    _cur = _c.cursor()
                    _cur.execute("SELECT val FROM ghost_state WHERE key='engine_pause_resume_ts'")
                    _row = _cur.fetchone()
                    if _row and _row[0]:
                        resume_ts = int(_row[0])
                if resume_ts:
                    ew = evaluate_kill_conditions(since_ts=resume_ts)
                    out["enforcement_window"] = {
                        "since_ts": resume_ts,
                        "note": "enforcement counts only outcomes resolved after the last manual resume",
                        "conditions": ew.get("conditions"),
                        "any_triggered": any(c.get("triggered") for c in (ew.get("conditions") or [])),
                    }
                else:
                    out["enforcement_window"] = {"since_ts": 0, "note": "no manual resume — enforcement uses the same all-time window"}
            except Exception:
                pass
        return out
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/wolf/pnl")
def wolf_pnl(symbol: str = "WOLF"):
    """Realized-P&L tracker (audit §5). Turns the per-pick entry/exit ledger into
    an aggregate: sequential-compounding equity curve plus profit factor, max
    drawdown, expectancy and dollar P&L. Resolved v3.2-era picks only, oldest
    first. Public, read-only. Bankroll/stake via GHOST_PNL_BANKROLL /
    GHOST_PNL_STAKE_FRACTION env."""
    from wolf_app import _V32_ERA_MIN_ID, db_conn  # late import — shared state + monkeypatch-safe
    try:
        from core.pnl import realized_pnl
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT resolved_at,symbol,outcome,pnl_pct,entry_price,exit_price "
                "FROM predictions WHERE symbol=%s AND id >= %s AND outcome IS NOT NULL "
                "AND pnl_pct IS NOT NULL ORDER BY resolved_at ASC NULLS LAST, id ASC",
                (symbol, _V32_ERA_MIN_ID))
            rows = cur.fetchall()
        trades = [{
            "resolved_at": r[0], "symbol": r[1], "outcome": r[2],
            "pnl_pct": float(r[3]) if r[3] is not None else None,
            "entry_price": float(r[4]) if r[4] is not None else None,
            "exit_price": float(r[5]) if r[5] is not None else None,
        } for r in rows]
        out = realized_pnl(trades)
        out["symbol"] = symbol
        return out
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/wolf/daily-summary")
def wolf_daily_summary(limit: int = 30):
    """Stored daily engine summaries (roadmap #3b): per-day scans, candidates,
    saves, would-fire cycles, resolutions and engine-pause state. Newest first.
    Public, read-only."""
    from wolf_app import _build_daily_summary, db_conn, ensure_ghost_state  # late import — shared state + monkeypatch-safe
    try:
        import json as _j
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_ghost_state(cur)
            cur.execute("SELECT val FROM ghost_state WHERE key='daily_summary_history'")
            row = cur.fetchone()
        hist = []
        if row and row[0]:
            try:
                hist = _j.loads(row[0])
            except Exception:
                hist = []
        if not isinstance(hist, list):
            hist = []
        lim = max(1, min(90, int(limit)))
        recent = list(reversed(hist))[:lim]
        return {"ok": True, "count": len(recent), "days": recent, "today": _build_daily_summary()}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/wolf/pick-journal")
def wolf_pick_journal(limit: int = 50, offset: int = 0, symbol: str = "ALL"):
    """Pick journal — the credibility ledger (blueprint module 7).

    Every historical v3.2-era pick with full audit trail: confidence, the
    specialist score vector + regime-at-issuance (predictions.scores), entry/
    target/stop, resolution, exit, P&L. Defaults to ALL watchlist symbols;
    pass ?symbol=WOLF for one ticker. Plus aggregate honesty metrics computed
    over ALL resolved picks (not just the page): win rate with a 95% Wilson CI,
    expectancy, Brier score, and the pre-registered falsification verdict
    (core.prediction.FALSIFICATION_THRESHOLD, blueprint §10). Paginated, newest
    first. Public, read-only — this is the auditable record the 80% claim rests on.
    """
    from wolf_app import NON_RESEARCH_WHERE, REAL_TRADE_WHERE, _V32_ERA_MIN_ID, _coerce_json, _pick_journal_scope, db_conn  # late import — shared state + monkeypatch-safe
    import math as _m
    from core.prediction import FALSIFICATION_THRESHOLD
    try:
        lim = max(1, min(200, int(limit)))
        off = max(0, int(offset))
        scope_sql, scope_params, sym_label = _pick_journal_scope(symbol)
        base_where = scope_sql + "id >= %s AND " + REAL_TRADE_WHERE
        base_params = scope_params + (_V32_ERA_MIN_ID,)
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM predictions WHERE " + base_where,
                base_params,
            )
            total = cur.fetchone()[0]
            cur.execute(
                "SELECT id,symbol,direction,confidence,entry_price,target_price,stop_price,"
                "predicted_at,expires_at,resolved_at,outcome,exit_price,pnl_pct,features,scores "
                "FROM predictions WHERE " + base_where + " "
                "ORDER BY predicted_at DESC NULLS LAST, id DESC LIMIT %s OFFSET %s",
                base_params + (lim, off),
            )
            rows = cur.fetchall()
            # Credibility metrics + falsification verdict exclude research picks
            # (low-bar by design); the paginated journal listing still shows them.
            cur.execute(
                "SELECT confidence,outcome,pnl_pct FROM predictions WHERE "
                + scope_sql + "id >= %s AND outcome IS NOT NULL AND " + REAL_TRADE_WHERE
                + " AND " + NON_RESEARCH_WHERE,
                base_params,
            )
            resolved = cur.fetchall()

        picks = []
        for r in rows:
            (pid, sym, direction, conf, entry, target, stop, pred_at, exp_at,
             res_at, outcome, exit_p, pnl, feats, scrs) = r
            _sc = _coerce_json(scrs)
            # Flatten the indicator vector at issuance (audit §4) for direct display.
            _fv = (_sc.get("features") if isinstance(_sc, dict) else None) or {}
            _rg = (_sc.get("regime") if isinstance(_sc, dict) else None) or {}
            indicators = None
            if _fv:
                indicators = {
                    "rsi": _fv.get("rsi"), "macd_hist": _fv.get("macd_hist"),
                    "pct_b": _fv.get("pct_b"), "atr_pct": _fv.get("atr_pct"),
                    "volume_ratio": _fv.get("volume_ratio"), "mom_4h": _fv.get("mom_4h"),
                    "adx": _fv.get("adx"), "regime": _rg.get("label"),
                }
            picks.append({
                "id": pid, "symbol": sym, "direction": direction,
                "confidence": float(conf) if conf is not None else None,
                "entry_price": entry, "target_price": target, "stop_price": stop,
                "predicted_at": pred_at, "expires_at": exp_at, "resolved_at": res_at,
                "outcome": outcome, "exit_price": exit_p,
                "pnl_pct": float(pnl) if pnl is not None else None,
                "features": _coerce_json(feats), "scores": _sc,
                "indicators": indicators,
            })

        n = len(resolved)
        wins = sum(1 for c, o, p in resolved if o == "WIN")
        losses = sum(1 for c, o, p in resolved if o == "LOSS")
        expired = sum(1 for c, o, p in resolved if o == "EXPIRED")
        win_rate = (wins / n) if n else None
        pnls = [float(p) for c, o, p in resolved if p is not None]
        expectancy_pct = (sum(pnls) / len(pnls)) if pnls else None
        win_pnls = [float(p) for c, o, p in resolved if o == "WIN" and p is not None]
        loss_pnls = [float(p) for c, o, p in resolved if o in ("LOSS", "EXPIRED") and p is not None]
        avg_win_pct = (sum(win_pnls) / len(win_pnls)) if win_pnls else None
        avg_loss_pct = (sum(loss_pnls) / len(loss_pnls)) if loss_pnls else None
        # Brier: p = stated confidence (P(win)); y = 1 if WIN else 0. Lower is better.
        brier_terms = [(float(c) - (1.0 if o == "WIN" else 0.0)) ** 2
                       for c, o, p in resolved if c is not None]
        brier = (sum(brier_terms) / len(brier_terms)) if brier_terms else None
        # 95% Wilson score interval on win rate (robust at small N, never leaves [0,1])
        ci_low = ci_high = None
        if n:
            z = 1.96
            phat = wins / n
            denom = 1.0 + z * z / n
            center = (phat + z * z / (2 * n)) / denom
            margin = (z * _m.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))) / denom
            ci_low = max(0.0, center - margin)
            ci_high = min(1.0, center + margin)

        ft = FALSIFICATION_THRESHOLD
        falsified = False
        fal_status = "insufficient_samples"
        if n >= ft["min_samples"]:
            ci_excludes_north_star = (ci_high is not None and ci_high < ft["north_star"])
            if win_rate is not None and win_rate < ft["win_rate_floor"] and ci_excludes_north_star:
                falsified = True
                fal_status = "ABANDON_80_CLAIM"
            elif win_rate is not None and win_rate >= ft["win_rate_floor"]:
                fal_status = "on_track"
            else:
                fal_status = "watch"   # below floor but CI still admits 80% — not yet falsified

        return {
            "ok": True,
            "symbol": sym_label,
            "total": total,
            "limit": lim,
            "offset": off,
            "returned": len(picks),
            "picks": picks,
            "metrics": {
                "resolved": n, "wins": wins, "losses": losses, "expired": expired,
                "win_rate": round(win_rate, 4) if win_rate is not None else None,
                "win_rate_ci95": [round(ci_low, 4), round(ci_high, 4)] if ci_low is not None else None,
                "expectancy_pct": round(expectancy_pct, 4) if expectancy_pct is not None else None,
                "avg_win_pct": round(avg_win_pct, 4) if avg_win_pct is not None else None,
                "avg_loss_pct": round(avg_loss_pct, 4) if avg_loss_pct is not None else None,
                "brier": round(brier, 4) if brier is not None else None,
            },
            "verdict": {
                "falsification": {
                    "status": fal_status,
                    "falsified": falsified,
                    "threshold": ft,
                    "samples": n,
                    "win_rate": round(win_rate, 4) if win_rate is not None else None,
                    "ci95_high": round(ci_high, 4) if ci_high is not None else None,
                },
            },
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/wolf/performance-log/cycles")
def wolf_perf_log_cycles(limit: int = 50, offset: int = 0, since: int = 0):
    """Paginated prediction-cycle log (full detail in /cycles/{id}). Newest first."""
    try:
        from core.performance_log import fetch_cycles
        since_ts = int(since) if since else None
        out = fetch_cycles(limit=limit, offset=offset, since_ts=since_ts)
        return {"ok": True, **out}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/wolf/performance-log/cycles/{cycle_id}")
def wolf_perf_log_cycle_detail(cycle_id: int, symbol_limit: int = 200):
    """One cycle with per-symbol gate evaluations and full JSON context."""
    try:
        from core.performance_log import fetch_cycle_detail
        detail = fetch_cycle_detail(cycle_id, symbol_limit=symbol_limit)
        if not detail:
            return JSONResponse({"ok": False, "error": "cycle_not_found"}, status_code=404)
        return {"ok": True, "cycle": detail}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/wolf/performance-log/events")
def wolf_perf_log_events(
    limit: int = 100,
    offset: int = 0,
    event_type: str = "",
    prediction_id: int = 0,
    since: int = 0,
):
    """Pick lifecycle events: pick_saved, pick_resolved, pick_expired, cycle_complete."""
    try:
        from core.performance_log import fetch_events
        out = fetch_events(
            limit=limit,
            offset=offset,
            event_type=event_type or None,
            prediction_id=prediction_id or None,
            since_ts=int(since) if since else None,
        )
        return {"ok": True, **out}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/wolf/performance-log/progress")
def wolf_perf_log_progress(days: int = 7):
    """Track prediction progress: open picks, recent resolves, cycle rollups."""
    try:
        from core.performance_log import fetch_progress_summary
        out = fetch_progress_summary(days=days)
        return {"ok": True, **out}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/wolf/performance-log/symbols/{symbol}")
def wolf_perf_log_symbol(symbol: str, limit: int = 100, since: int = 0):
    """Per-symbol evaluation history across cycles (skip codes, up_prob, confidence)."""
    try:
        from core.performance_log import fetch_symbol_eval_history
        since_ts = int(since) if since else None
        out = fetch_symbol_eval_history(symbol, limit=limit, since_ts=since_ts)
        return {"ok": True, **out}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.post("/api/wolf/signal-alert/check")
def wolf_signal_alert_check(x_cron_secret: str = Header(default="")):
    """Scan recent WOLF picks for unalerted high-confidence signals; fire Telegram.

    Throttling:
      - Confidence floor: 0.80 (only high-conviction signals alert)
      - Per-pick dedup: each prediction id only alerts once (wolf_signal_alerts table)
      - Daily cap: max 2 alerts per UTC day

    Designed to be called from a cron after /api/run-predictions or
    /api/morning-card. Safe to call repeatedly — dedup prevents duplicates.
    """
    from wolf_app import _cron_ok, db_conn, non_research_where  # late import — shared state + monkeypatch-safe
    if not _cron_ok(x_cron_secret):
        raise HTTPException(status_code=403)

    conf_floor = float(os.getenv("WOLF_ALERT_CONFIDENCE_FLOOR", "0.80"))
    daily_cap = int(os.getenv("WOLF_ALERT_DAILY_CAP", "2"))
    day_start = int(time.time()) - (int(time.time()) % 86400)

    sent: list[dict] = []
    errors: list[str] = []
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS wolf_signal_alerts (
                    prediction_id BIGINT PRIMARY KEY,
                    sent_at BIGINT NOT NULL,
                    direction TEXT,
                    entry_price DOUBLE PRECISION,
                    target_price DOUBLE PRECISION,
                    confidence DOUBLE PRECISION
                )
                """
            )
            cur.execute(
                "SELECT COUNT(*) FROM wolf_signal_alerts WHERE sent_at >= %s",
                (day_start,),
            )
            sent_today = int(cur.fetchone()[0] or 0)
            remaining = max(0, daily_cap - sent_today)
            if remaining <= 0:
                return {"ok": True, "sent": [], "skipped_reason": "daily cap reached",
                        "sent_today": sent_today, "daily_cap": daily_cap}

            # Research picks are learning probes fired below the accuracy
            # contract — they must NEVER be alerted as live BUY/SELL signals.
            cur.execute(
                """
                SELECT p.id, p.direction, p.confidence, p.entry_price, p.target_price,
                       p.stop_price, p.expires_at, p.predicted_at
                FROM predictions p
                LEFT JOIN wolf_signal_alerts a ON a.prediction_id = p.id
                WHERE p.symbol = 'WOLF'
                  AND p.outcome IS NULL
                  AND p.confidence >= %s
                  AND p.predicted_at >= %s
                  AND a.prediction_id IS NULL
                  AND """ + non_research_where("p") + """
                ORDER BY p.confidence DESC, p.predicted_at DESC
                LIMIT %s
                """,
                (conf_floor, day_start, remaining),
            )
            candidates = cur.fetchall()

            from core.telegram import _send
            for row in candidates:
                pid, direction, conf, entry, target, stop, expires, predicted = row
                buy_dir = direction in ("UP", "BUY")
                head = "BUY SIGNAL" if buy_dir else "SELL SIGNAL"
                entry_label = "Buy at" if buy_dir else "Short at"
                target_label = "Target" if buy_dir else "Cover at"
                hrs = max(0, int(((expires or 0) - time.time()) // 3600)) if expires else None
                body = (
                    f"\U0001F43A {head}: WOLF\n"
                    f"{entry_label} ${float(entry):.2f}\n"
                    f"{target_label} ${float(target):.2f}\n"
                    f"Stop ${float(stop):.2f}\n"
                    f"Confidence: {round(float(conf) * 100, 1)}%"
                    + (f"\nWindow: ~{hrs}h" if hrs is not None else "")
                )
                try:
                    ok = _send(body)
                    if not ok:
                        errors.append(f"id={pid} telegram: dead-lettered after retries")
                        continue
                except Exception as _se:
                    errors.append(f"id={pid} telegram: {str(_se)[:80]}")
                    continue
                # Out-of-band fire alert (roadmap #1d) — email/SMS, env-gated,
                # best-effort. Same dedup as Telegram (one row per prediction id).
                try:
                    from core.notify import notify_pick_fired
                    notify_pick_fired("Ghost Protocol — WOLF " + head, body)
                except Exception as _ne:
                    errors.append(f"id={pid} notify: {str(_ne)[:60]}")
                cur.execute(
                    "INSERT INTO wolf_signal_alerts(prediction_id, sent_at, direction, "
                    "entry_price, target_price, confidence) VALUES (%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (prediction_id) DO NOTHING",
                    (int(pid), int(time.time()), direction, float(entry) if entry else None,
                     float(target) if target else None, float(conf) if conf else None),
                )
                sent.append({
                    "prediction_id": int(pid), "direction": direction,
                    "entry_price": float(entry) if entry else None,
                    "target_price": float(target) if target else None,
                    "confidence": float(conf) if conf else None,
                })
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200], "sent": sent, "errors": errors}, status_code=500)

    return {"ok": True, "sent": sent, "sent_today": sent_today + len(sent),
            "daily_cap": daily_cap, "errors": errors}


@router.post("/api/cron/signal-check")
def cron_signal_check(x_cron_secret: str = Header(default="")):
    """Cron-triggered Telegram signal-alert sweep.

    Thin wrapper around wolf_signal_alert_check that also records the
    cron invocation in ghost_state for ops visibility. Wire this to your
    Railway cron schedule (cron-job.org / Railway scheduled jobs) alongside
    the existing prediction cycle — typical cadence: every 5-15 minutes
    during market hours. Throttling and dedup live inside the underlying
    check, so calling more frequently than needed is safe.
    """
    from wolf_app import LOGGER, _cron_ok, db_conn, ensure_ghost_state, wolf_signal_alert_check  # late import — shared state + monkeypatch-safe
    if not _cron_ok(x_cron_secret):
        raise HTTPException(status_code=403)
    ran_at = int(time.time())
    alert_result = wolf_signal_alert_check(x_cron_secret=x_cron_secret)
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_ghost_state(cur)
            cur.execute(
                "INSERT INTO ghost_state(key,val) VALUES('last_signal_cron_ts',%s) "
                "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                (str(ran_at),),
            )
            sent_count = len(alert_result.get("sent", [])) if isinstance(alert_result, dict) else 0
            cur.execute(
                "INSERT INTO ghost_state(key,val) VALUES('last_signal_cron_sent',%s) "
                "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                (str(sent_count),),
            )
    except Exception as _e:
        LOGGER.warning("cron_signal_check state write failed: " + str(_e)[:120])
    return {"ok": True, "cron": "signal-check", "ran_at": ran_at, "alert_result": alert_result}
