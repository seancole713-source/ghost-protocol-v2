"""api/routes_data.py — endpoint group split out of wolf_app.py (PR #130).

Endpoint bodies late-import shared helpers from wolf_app at request time so
tests that monkeypatch wolf_app attributes (db_conn, _cron_ok, ...) keep
working, and so this module never imports wolf_app at import time (no cycle).
wolf_app re-exports every endpoint name for backward compatibility.
"""
import os, sys, time, json, logging, threading, hmac, math, asyncio, base64  # noqa: F401,E401

from fastapi import APIRouter, Header, HTTPException, Request, Depends  # noqa: F401
from fastapi.responses import JSONResponse, HTMLResponse, Response, PlainTextResponse  # noqa: F401

router = APIRouter()

@router.get("/api/picks")
def get_picks(symbol: str = "ALL", asset_type: str = None, limit: int = 50, offset: int = 0):
    """Recent picks with pagination. Defaults to ALL watchlist symbols.
    ?symbol=WOLF filters to one ticker; ?asset_type= filters by type.
    ?limit=&offset= page resolved picks newest-first (default limit 50, max 200).
    Rows with entry_price=0 or non-stock asset_type are excluded (crypto-era junk)."""
    from wolf_app import _norm_pred, _picks_where, db_conn  # late import — shared state + monkeypatch-safe
    try:
        lim = max(1, min(200, int(limit)))
        off = max(0, int(offset))
        clauses, params = _picks_where(symbol, asset_type)
        where = " WHERE " + " AND ".join(clauses)
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM predictions" + where + " AND outcome IS NULL "
                "ORDER BY predicted_at DESC NULLS LAST, id DESC",
                tuple(params),
            )
            cols = [d[0] for d in cur.description]
            active = [_norm_pred(dict(zip(cols, r))) for r in cur.fetchall()]
            cur.execute(
                "SELECT outcome, COUNT(*) FROM predictions" + where
                + " AND outcome IN ('WIN','LOSS') GROUP BY outcome",
                tuple(params),
            )
            tally = {r[0]: r[1] for r in cur.fetchall()}
            wins = tally.get("WIN", 0)
            losses = tally.get("LOSS", 0)
            total = wins + losses
            cur.execute(
                "SELECT * FROM predictions" + where + " AND outcome IS NOT NULL "
                "ORDER BY predicted_at DESC NULLS LAST, id DESC LIMIT %s OFFSET %s",
                tuple(params) + (lim, off),
            )
            cols = [d[0] for d in cur.description]
            resolved = [_norm_pred(dict(zip(cols, r))) for r in cur.fetchall()]
        return {
            "ok": True,
            "symbol": symbol,
            "asset_type": asset_type,
            "limit": lim,
            "offset": off,
            "active": active,
            "recent": resolved,
            "has_more": off + len(resolved) < total,
            "accuracy_pct": round(wins / total * 100, 1) if total else 0,
            "wins": wins,
            "losses": losses,
            "total": total,
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/api/history")
def get_history(limit: int = 200):
    from wolf_app import REAL_TRADE_WHERE, _norm_pred, db_conn  # late import — shared state + monkeypatch-safe
    try:
        lim = max(1, min(500, int(limit)))
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM predictions WHERE " + REAL_TRADE_WHERE
                + " ORDER BY predicted_at DESC NULLS LAST, id DESC LIMIT %s",
                (lim,),
            )
            cols = [d[0] for d in cur.description]
            rows = [_norm_pred(dict(zip(cols, r))) for r in cur.fetchall()]
        resolved = [r for r in rows if r["outcome"] is not None]
        wins = sum(1 for r in resolved if r["outcome"] == "WIN")
        wl = [r for r in resolved if r["outcome"] in ("WIN","LOSS")]
        total_pnl = sum(r["pnl_pct"] or 0 for r in resolved)
        return {"ok": True, "trades": resolved, "total": len(resolved), "wins": wins,
                "losses": len(wl)-wins,
                "win_rate_pct": round(wins/len(wl)*100,1) if wl else 0,
                "total_pnl_pct": round(total_pnl,2)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/api/news")
def get_news():
    """WOLF-relevant news only (audit v2 #4). The raw feed leaked off-topic
    market-roundup articles (Zoom, Ross Stores, …); now filtered to the WOLF
    text match used by /api/wolf/news."""
    from wolf_app import _is_wolf_relevant  # late import — shared state + monkeypatch-safe
    try:
        from core.news import get_recent_articles
        raw = get_recent_articles(50) or []
        articles = [a for a in raw if _is_wolf_relevant(a)][:20]
        return {"ok": True, "articles": articles, "count": len(articles)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/api/schema")
def get_schema():
    from wolf_app import db_conn  # late import — shared state + monkeypatch-safe
    tables = {}
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema='public' ORDER BY table_name, ordinal_position")
        for table, col in cur.fetchall():
            if table not in tables: tables[table] = []
            tables[table].append(col)
    return {"ok": True, "tables": tables}


@router.get("/api/stats")
def get_stats():
    """Overall accuracy stats across all sources."""
    from wolf_app import _compute_get_stats, db_conn  # late import — shared state + monkeypatch-safe
    with db_conn() as conn:
        return _compute_get_stats(conn.cursor())


@router.get("/api/stats/v32")
def get_stats_v32():
    """
    BUY-only WIN/LOSS in the same v3.2 window as /api/stats post_v32
    (V3_STATS_START_TS or min tp_sl_daily trained_at).
    """
    from wolf_app import NON_RESEARCH_WHERE, _v32_stats_start_ts, db_conn  # late import — shared state + monkeypatch-safe
    import datetime as _dt

    try:
        with db_conn() as conn:
            cur = conn.cursor()
            v32_start_ts = _v32_stats_start_ts(cur)
            now_ts = int(time.time())
            if v32_start_ts <= 0:
                return {
                    "ok": True,
                    "era": "v3.2",
                    "start_ts": 0,
                    "since": None,
                    "since_iso": None,
                    "wins": 0,
                    "losses": 0,
                    "total": 0,
                    "win_rate_pct": 0.0,
                    "open_picks": 0,
                    "verdict": "review",
                    "note": "No v3.2 cutover timestamp; set V3_STATS_START_TS or train tp_sl_daily models.",
                }
            cur.execute(
                """
                SELECT outcome, COUNT(*) FROM predictions
                WHERE direction IN ('UP','BUY')
                AND predicted_at IS NOT NULL AND predicted_at >= %s
                AND outcome IN ('WIN','LOSS')
                AND """ + NON_RESEARCH_WHERE + """
                GROUP BY outcome
                """,
                (v32_start_ts,),
            )
            rows = {r[0]: r[1] for r in cur.fetchall()}
            wins = rows.get("WIN", 0)
            losses = rows.get("LOSS", 0)
            total = wins + losses
            wr = round(wins / total * 100, 1) if total else 0
            cur.execute(
                """
                SELECT outcome, COUNT(*) FROM predictions
                WHERE direction IN ('UP','BUY')
                AND resolved_at IS NOT NULL AND resolved_at >= %s
                AND outcome IN ('WIN','LOSS')
                AND """ + NON_RESEARCH_WHERE + """
                GROUP BY outcome
                """,
                (v32_start_ts,),
            )
            rrows = {r[0]: r[1] for r in cur.fetchall()}
            rw = rrows.get("WIN", 0)
            rl = rrows.get("LOSS", 0)
            rt = rw + rl
            rwr = round(rw / rt * 100, 1) if rt else 0
            cur.execute(
                """
                SELECT COUNT(*) FROM predictions
                WHERE direction IN ('UP','BUY')
                AND predicted_at >= %s AND predicted_at IS NOT NULL
                AND outcome IS NULL
                AND expires_at > %s
                """,
                (v32_start_ts, now_ts),
            )
            open_picks = cur.fetchone()[0]
        since_iso = _dt.datetime.fromtimestamp(v32_start_ts, tz=_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        since_day = since_iso[:10]
        verdict = "on_track" if wr >= 55 else "watch" if wr >= 45 else "review"
        return {
            "ok": True,
            "era": "v3.2",
            "start_ts": v32_start_ts,
            "since": since_day,
            "since_iso": since_iso,
            "wins": wins,
            "losses": losses,
            "total": total,
            "win_rate_pct": wr,
            "resolved_wins": rw,
            "resolved_losses": rl,
            "resolved_total": rt,
            "resolved_win_rate_pct": rwr,
            "open_picks": open_picks,
            "verdict": verdict,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}


@router.get("/api/stats/confidence-buckets")
def get_stats_confidence_buckets():
    """Realized WIN/LOSS per confidence bucket since the v3.2 cutover.

    Public read-only — same convention as /api/stats and /api/stats/v32.
    Diagnostic for confidence calibration: if a high bucket wins at chance
    rate, confidence carries no signal and the engine needs recalibration.
    """
    from wolf_app import _v32_stats_start_ts, db_conn  # late import — shared state + monkeypatch-safe
    buckets_spec = [
        ("<60", 0.00, 0.60),
        ("60-70", 0.60, 0.70),
        ("70-80", 0.70, 0.80),
        ("80-90", 0.80, 0.90),
        ("90+", 0.90, 1.01),
    ]
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            v32_start_ts = _v32_stats_start_ts(cur)
            out = []
            for label, lo, hi in buckets_spec:
                if v32_start_ts > 0:
                    cur.execute(
                        "SELECT outcome, COUNT(*) FROM predictions "
                        "WHERE outcome IN ('WIN','LOSS') "
                        "AND predicted_at IS NOT NULL AND predicted_at >= %s "
                        "AND confidence >= %s AND confidence < %s "
                        "GROUP BY outcome",
                        (v32_start_ts, lo, hi),
                    )
                else:
                    cur.execute(
                        "SELECT outcome, COUNT(*) FROM predictions "
                        "WHERE outcome IN ('WIN','LOSS') "
                        "AND predicted_at IS NOT NULL "
                        "AND confidence >= %s AND confidence < %s "
                        "GROUP BY outcome",
                        (lo, hi),
                    )
                rows = {r[0]: r[1] for r in cur.fetchall()}
                w = rows.get("WIN", 0)
                l = rows.get("LOSS", 0)
                tot = w + l
                out.append({
                    "label": label,
                    "min": lo,
                    "max": hi,
                    "wins": w,
                    "losses": l,
                    "total": tot,
                    "win_rate_pct": round(w / tot * 100, 1) if tot else 0.0,
                })
        return {"ok": True, "start_ts": v32_start_ts, "buckets": out}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/symbol-accuracy")
def symbol_accuracy():
    """Show per-symbol win rates from ghost_prediction_outcomes. Ground truth."""
    from wolf_app import db_conn  # late import — shared state + monkeypatch-safe
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                symbol,
                COUNT(*) as total,
                SUM(CASE WHEN hit_direction = 1 THEN 1 ELSE 0 END) as wins,
                ROUND(100.0 * SUM(CASE WHEN hit_direction = 1 THEN 1 ELSE 0 END) / COUNT(*), 1) as win_rate,
                AVG(CASE WHEN predicted_direction = 'UP' THEN 1.0 ELSE 0.0 END) as pct_up_picks
            FROM ghost_prediction_outcomes
            WHERE hit_direction IN (0, 1)
            GROUP BY symbol
            HAVING COUNT(*) >= 10
            ORDER BY win_rate DESC
        """)
        rows = cur.fetchall()
    symbols = [{"symbol": r[0], "total": r[1], "wins": r[2], "win_rate": float(r[3]), "pct_up": round(float(r[4] or 0), 2)} for r in rows]
    edges = [s for s in symbols if s["win_rate"] > 55]
    return {
        "ok": True,
        "legacy": True,
        "warning": ("LEGACY TABLE: ghost_prediction_outcomes predates the v3.2 engine and "
                    "the 70% contract, and includes crypto-era picks. Do NOT read these "
                    "win rates as current v3 stock performance — use /api/stats and "
                    "/api/v3/status instead. (PR #135 audit)"),
        "total_symbols": len(symbols), "symbols_with_edge": len(edges), "data": symbols,
    }


@router.get("/api/telegram/status")
def telegram_status():
    """Telegram delivery visibility for the cockpit.

    Public read (matches /api/v3/status convention). Returns:
      - configured: whether Telegram env vars are set
      - last_cron_ts / last_cron_sent: from ghost_state (PR #8 signal-alert)
      - recent_alerts: last 5 rows from wolf_signal_alerts table
    """
    from wolf_app import db_conn, ensure_ghost_state  # late import — shared state + monkeypatch-safe
    out = {
        "ok": True,
        "configured": bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")),
        "last_cron_ts": None,
        "last_cron_sent": None,
        "recent_alerts": [],
    }
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_ghost_state(cur)
            cur.execute("SELECT val FROM ghost_state WHERE key='last_signal_cron_ts'")
            row = cur.fetchone()
            if row and row[0]:
                try:
                    out["last_cron_ts"] = int(row[0])
                except Exception:
                    pass
            cur.execute("SELECT val FROM ghost_state WHERE key='last_signal_cron_sent'")
            row = cur.fetchone()
            if row and row[0]:
                try:
                    out["last_cron_sent"] = int(row[0])
                except Exception:
                    pass
            # wolf_signal_alerts table is created lazily by signal-alert/check;
            # tolerate it not existing yet on fresh deploys.
            try:
                cur.execute(
                    "SELECT prediction_id, sent_at, direction, entry_price, target_price, confidence "
                    "FROM wolf_signal_alerts ORDER BY sent_at DESC LIMIT 5"
                )
                for r in cur.fetchall():
                    out["recent_alerts"].append({
                        "prediction_id": int(r[0]),
                        "sent_at": int(r[1]) if r[1] else None,
                        "direction": r[2],
                        "entry_price": float(r[3]) if r[3] is not None else None,
                        "target_price": float(r[4]) if r[4] is not None else None,
                        "confidence": float(r[5]) if r[5] is not None else None,
                    })
            except Exception:
                pass
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)
    return out


@router.get("/api/coverage")
def coverage_status():
    """Coverage maintenance status for monitoring/ops."""
    from wolf_app import _COVERAGE_RETRAIN_RUNNING, db_conn, ensure_ghost_state  # late import — shared state + monkeypatch-safe
    now = int(time.time())
    enabled = os.getenv("AUTO_COVERAGE_RETRAIN_ENABLED", "1").strip() in ("1", "true", "TRUE", "yes", "on")
    min_models = max(1, int(os.getenv("MODEL_COVERAGE_MIN_MODELS", "3")))
    cooldown_s = max(900, int(os.getenv("COVERAGE_RETRAIN_COOLDOWN_SEC", "21600")))
    check_interval_s = max(900, int(os.getenv("COVERAGE_CHECK_INTERVAL_SEC", "3600")))

    try:
        from core.signal_engine import get_model_status
        st = get_model_status() or {}
    except Exception as e:
        st = {"trained": False, "reason": "status_error: " + str(e)[:80]}
    loaded_models = int(st.get("models", 0)) if st.get("trained") else 0

    last_ts = 0
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_ghost_state(cur)
            cur.execute("SELECT val FROM ghost_state WHERE key='last_coverage_retrain_ts'")
            row = cur.fetchone()
            last_ts = int(row[0]) if row and row[0] else 0
    except Exception:
        last_ts = 0

    since_last_s = (now - last_ts) if last_ts else None
    cooldown_remaining_s = max(0, cooldown_s - since_last_s) if since_last_s is not None else 0
    below_floor = loaded_models < min_models
    eligible_now = enabled and below_floor and cooldown_remaining_s == 0 and (not _COVERAGE_RETRAIN_RUNNING)

    return {
        "ok": True,
        "now_ts": now,
        "coverage": {
            "loaded_models": loaded_models,
            "min_models_floor": min_models,
            "below_floor": below_floor,
        },
        "maintenance": {
            "enabled": enabled,
            "running": _COVERAGE_RETRAIN_RUNNING,
            "check_interval_s": check_interval_s,
            "cooldown_s": cooldown_s,
            "last_retrain_ts": last_ts or None,
            "since_last_retrain_s": since_last_s,
            "cooldown_remaining_s": cooldown_remaining_s,
            "eligible_now": eligible_now,
        },
        "model_status": st,
    }


@router.get("/api/db-probe")
def db_probe():
    """Count rows in v1 outcome tables to find where data lives."""
    from wolf_app import db_conn  # late import — shared state + monkeypatch-safe
    tables = [
        "accuracy_forecasts", "ghost_predictions", "ghost_prediction_outcomes",
        "ghost_tracked_picks", "ai_memory", "outcomes", "ghost_accuracy_stats",
        "predictions", "paper_trades", "money_game_trades",
    ]
    counts = {}
    with db_conn() as conn:
        cur = conn.cursor()
        for t in tables:
            try:
                cur.execute("SELECT COUNT(*) FROM " + t)
                counts[t] = cur.fetchone()[0]
            except Exception as e:
                conn.rollback()
                counts[t] = "ERR: " + str(e)[:60]
        # Also check ghost_tracked_picks columns
        try:
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='ghost_tracked_picks' ORDER BY ordinal_position")
            counts["ghost_tracked_picks_cols"] = [r[0] for r in cur.fetchall()]
        except Exception:
            pass
        try:
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='ghost_predictions' ORDER BY ordinal_position")
            counts["ghost_predictions_cols"] = [r[0] for r in cur.fetchall()][:10]
        except Exception:
            pass
        try:
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='money_game_trades' ORDER BY ordinal_position")
            counts["money_game_trades_cols"] = [r[0] for r in cur.fetchall()]
        except Exception:
            pass
    return {"ok": True, "counts": counts}


@router.get("/api/research/status")
def research_status_endpoint():
    """Public read-only: research pick mode status + resolved count + gate info."""
    try:
        from core.prediction import (
            RESEARCH_PICK_ENABLED, RESEARCH_CONFIDENCE_FLOOR,
            RESEARCH_MIN_RESOLVED, RESEARCH_DAILY_CAP, RESEARCH_STALL_HOURS,
        )
        from core.db import db_conn
        resolved = 0
        research_today = 0
        active = 0
        recent_fires = 0
        stalled = False
        try:
            with db_conn() as rc:
                cur = rc.cursor()
                cur.execute("SELECT COUNT(*) FROM predictions WHERE outcome IS NOT NULL AND asset_type='stock'")
                resolved = int(cur.fetchone()[0])
                cur.execute(
                    "SELECT COUNT(*) FROM predictions WHERE scores->>'research_pick' = 'true' AND predicted_at > %s",
                    (int(time.time()) - 86400,),
                )
                research_today = int(cur.fetchone()[0])
                cur.execute(
                    "SELECT COUNT(*) FROM predictions WHERE outcome IS NULL AND expires_at > %s",
                    (int(time.time()),),
                )
                active = int(cur.fetchone()[0])
                stall_cutoff = int(time.time()) - RESEARCH_STALL_HOURS * 3600
                cur.execute(
                    "SELECT COUNT(*) FROM predictions WHERE predicted_at > %s",
                    (stall_cutoff,),
                )
                recent_fires = int(cur.fetchone()[0])
                stalled = active == 0 and recent_fires == 0 and resolved >= RESEARCH_MIN_RESOLVED
        except Exception:
            pass
        research_active = RESEARCH_PICK_ENABLED and (resolved < RESEARCH_MIN_RESOLVED or stalled)
        return {
            "ok": True,
            "research_enabled": RESEARCH_PICK_ENABLED,
            "research_active": research_active,
            "research_reason": "cold_start" if resolved < RESEARCH_MIN_RESOLVED else ("stall" if stalled else None),
            "resolved_picks": resolved,
            "min_for_exit": RESEARCH_MIN_RESOLVED,
            "remaining": max(0, RESEARCH_MIN_RESOLVED - resolved),
            "confidence_floor": RESEARCH_CONFIDENCE_FLOOR if research_active else None,
            "research_today": research_today,
            "research_daily_cap": RESEARCH_DAILY_CAP,
            "active_picks": active,
            "recent_fires_24h": recent_fires,
            "stall_hours": RESEARCH_STALL_HOURS,
            "stalled": stalled,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.get("/api/debug-signal/{symbol}")
def debug_signal(symbol: str):
    """Step-by-step trace of signal logic - exposes every intermediate value."""
    from core.db import db_conn
    from core.prices import get_price
    import os, traceback
    result = {"symbol": symbol, "steps": []}
    try:
        price = get_price(symbol, "stock")
        result["price"] = price
        result["steps"].append("price=" + str(price))
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT predicted_direction, hit_direction FROM ghost_prediction_outcomes WHERE symbol=%s AND hit_direction IN (0,1) ORDER BY created_at DESC LIMIT 200", (symbol,))
            gpo_rows = cur.fetchall()
            result["gpo_count"] = len(gpo_rows)
            result["steps"].append("gpo_rows=" + str(len(gpo_rows)))
            cur.execute("SELECT direction, CASE WHEN outcome='WIN' THEN 1 ELSE 0 END FROM predictions WHERE symbol=%s AND outcome IN ('WIN','LOSS') ORDER BY id DESC LIMIT 50", (symbol,))
            v2_rows = cur.fetchall()
            result["v2_count"] = len(v2_rows)
            result["steps"].append("v2_rows=" + str(len(v2_rows)))
        # Circuit breaker check
        if len(v2_rows) >= 8:
            last8 = [r[1] for r in v2_rows[:8]]
            cb_fires = all(x == 0 for x in last8)
            result["circuit_breaker"] = {"would_fire": cb_fires, "last8": last8}
            result["steps"].append("circuit_breaker would_fire=" + str(cb_fires))
            if cb_fires:
                result["final"] = "BENCHED_BY_CIRCUIT_BREAKER"
                return result
        else:
            result["circuit_breaker"] = {"would_fire": False, "v2_count_lt_8": len(v2_rows)}
        # Combine rows
        rows = list(v2_rows) + list(v2_rows) + list(gpo_rows)
        result["combined_rows"] = len(rows)
        result["steps"].append("combined=" + str(len(rows)))
        # Check MIN_SAMPLES (10)
        if len(rows) < 10:
            result["final"] = "TOO_FEW_SAMPLES"
            return result
        total = len(rows)
        wins = sum(1 for _, o in rows if o == 1 or o == "WIN")
        win_rate = wins / total
        up_picks = sum(1 for ddd, _ in rows if ddd == "UP")
        down_picks = total - up_picks
        up_wins = sum(1 for ddd, o in rows if ddd == "UP" and (o == 1 or o == "WIN"))
        down_wins = sum(1 for ddd, o in rows if ddd == "DOWN" and (o == 1 or o == "WIN"))
        up_wr = up_wins / max(up_picks, 1)
        down_wr = down_wins / max(down_picks, 1)
        result["computed"] = {
            "total": total, "wins": wins, "win_rate": round(win_rate,3),
            "up_picks": up_picks, "down_picks": down_picks,
            "up_win_rate": round(up_wr,3), "down_win_rate": round(down_wr,3),
            "edge_threshold": 0.6, "inverse_threshold": 0.4,
            "sample_directions": list(set(r[0] for r in rows[:20])),
            "sample_outcomes": list(set(str(r[1]) for r in rows[:20])),
        }
        # Decision
        if up_wr > down_wr and up_wr > 0.6:
            result["final"] = "FIRE_UP " + str(round(up_wr,3))
        elif down_wr > up_wr and down_wr > 0.6:
            result["final"] = "FIRE_DOWN " + str(round(down_wr,3))
        elif win_rate < 0.4:
            result["final"] = "INVERT"
        else:
            result["final"] = "BENCH (no edge)"
    except Exception as e:
        result["error"] = str(e)
        result["traceback"] = traceback.format_exc()
    return result
    """Call actual prediction functions and return detailed trace."""
    import os
    from core.prices import get_price
    from core.prediction import _get_symbol_signal, _check_regime, CONFIDENCE_FLOOR, EDGE_THRESHOLD, INVERSE_THRESHOLD, MIN_SAMPLES
    price = get_price(symbol)
    regime = _check_regime()
    signal_error = None
    try:
        signal = _get_symbol_signal(symbol, price or 1.0)
    except Exception as e:
        signal = None
        signal_error = str(e)
    env_conf_floor = os.getenv("MIN_ALERT_CONFIDENCE", "NOT_SET")
    return {
        "symbol": symbol,
        "price": price,
        "signal": signal, "signal_error": signal_error,
        "would_pass_floor": signal is not None and signal[1] >= CONFIDENCE_FLOOR,
        "regime": regime,
        "env_MIN_ALERT_CONFIDENCE": env_conf_floor,
        "CONFIDENCE_FLOOR": CONFIDENCE_FLOOR,
        "EDGE_THRESHOLD": EDGE_THRESHOLD,
        "INVERSE_THRESHOLD": INVERSE_THRESHOLD,
        "MIN_SAMPLES": MIN_SAMPLES,
    }
    from core.db import db_conn
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT predicted_direction, hit_direction, COUNT(*) as cnt
            FROM ghost_prediction_outcomes
            WHERE symbol = %s AND hit_direction IN (0,1)
            GROUP BY predicted_direction, hit_direction
        """, (symbol,))
        gpo_breakdown = [{"dir":r[0],"hit":r[1],"count":r[2]} for r in cur.fetchall()]
        cur.execute(
            "SELECT direction, outcome, id FROM predictions WHERE symbol=%s AND outcome IN ('WIN','LOSS') ORDER BY id DESC LIMIT 10",
            (symbol,))
        v2_results = [{"dir":r[0],"outcome":r[1],"id":r[2]} for r in cur.fetchall()]
        cur.execute(
            "SELECT direction, outcome FROM predictions WHERE symbol=%s AND outcome IS NULL AND predicted_at IS NOT NULL LIMIT 5",
            (symbol,))
        v2_open = [{"dir":r[0]} for r in cur.fetchall()]
    # Compute win rates same way as _get_symbol_signal
    rows = [(r["dir"], r["hit"]) for r in gpo_breakdown for _ in range(r["count"])]
    total = len(rows)
    wins = sum(1 for _,o in rows if o==1)
    win_rate = wins/total if total else 0
    up_picks = sum(1 for d,_ in rows if d=="UP")
    down_picks = total - up_picks
    up_wins = sum(1 for d,o in rows if d=="UP" and o==1)
    down_wins = sum(1 for d,o in rows if d=="DOWN" and o==1)
    up_wr = up_wins/max(up_picks,1)
    down_wr = down_wins/max(down_picks,1)
    return {
        "symbol": symbol,
        "gpo_breakdown": gpo_breakdown,
        "v2_resolved": v2_results,
        "v2_open_count": len(v2_open),
        "computed": {
            "total":total,"wins":wins,"win_rate":round(win_rate,3),
            "up_picks":up_picks,"down_picks":down_picks,
            "up_win_rate":round(up_wr,3),"down_win_rate":round(down_wr,3),
            "would_fire": up_wr>0.60 or down_wr>0.60 or win_rate<0.40,
            "direction": "UP" if up_wr>down_wr and up_wr>0.60 else ("DOWN" if down_wr>up_wr and down_wr>0.60 else ("INVERT" if win_rate<0.40 else "BENCH"))
        }
    }


@router.get("/api/market/session/{symbol}")
def get_market_session_endpoint(symbol: str):
    """Live intraday session OHLC for the prediction mirror (PR #87).

    The unified console mirrors each prediction against real market truth:
    predicted open/ref vs live open, predicted low/stop vs the session low,
    predicted high/target vs the session high. ``/api/price`` only returns the
    spot price, so the mirror's live low/high/open were blank for Super Ghost
    pool symbols. This exposes core.prices.get_intraday_session(), which already
    computes today's open/high/low + last price via the Alpaca→yfinance chain.
    Read-only, best-effort: returns ok:false with a spot-price fallback rather
    than raising when feeds are unavailable.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"ok": False, "error": "symbol required"}
    try:
        from core.prices import get_intraday_session, get_price
        sess = get_intraday_session(sym) or {}
        has_ohlc = sess.get("today_open") is not None or sess.get("today_high") is not None
        if not sess.get("price"):
            spot = get_price(sym)
            if spot is not None:
                sess["price"] = spot
        # Recompute change_pct after price patch — get_intraday_session may
        # have prev_close but no last_price when Alpaca breaker is open.
        if sess.get("price") and sess.get("previous_close") and sess["previous_close"] > 0:
            if sess.get("change_pct") is None:
                chg = round(sess["price"] - sess["previous_close"], 4)
                sess["change_abs"] = chg
                sess["change_pct"] = round(chg / sess["previous_close"] * 100, 3)
        out = {
            "ok": bool(sess.get("price") is not None or has_ohlc),
            "symbol": sym,
            "price": sess.get("price"),
            "previous_close": sess.get("previous_close"),
            "session": sess.get("session"),
            "session_label": sess.get("session_label"),
            "market_date": sess.get("market_date"),
            "live_open": sess.get("today_open"),
            "live_high": sess.get("today_high"),
            "live_low": sess.get("today_low"),
            "change_abs": sess.get("change_abs"),
            "change_pct": sess.get("change_pct"),
            "feed": sess.get("feed"),
            "as_of_ts": sess.get("as_of_ts"),
        }
        return out
    except Exception as e:
        try:
            from core.prices import get_price
            spot = get_price(sym)
        except Exception:
            spot = None
        return {"ok": spot is not None, "symbol": sym, "price": spot,
                "live_open": None, "live_high": None, "live_low": None,
                "error": str(e)[:160]}


@router.get("/api/price/{symbol}")
def get_price_endpoint(symbol: str, asset_type: str = "stock"):
    """WOLF-only mode: asset_type is ignored, always returns stock price."""
    from core.prices import get_price
    price = get_price(symbol)
    return {"ok": price is not None, "symbol": symbol, "price": price}


@router.post("/api/run-predictions")
def trigger_predictions(x_cron_secret: str = Header(default="")):
    """Run prediction cycle only. Does NOT send Telegram (use /api/morning-card for that)."""
    from wolf_app import _cron_ok  # late import — shared state + monkeypatch-safe
    if not _cron_ok(x_cron_secret):
        raise HTTPException(status_code=403)
    from core.prediction import run_prediction_cycle
    picks = run_prediction_cycle()
    return {"ok": True, "picks_generated": len(picks), "picks": picks}


@router.post("/api/morning-card")
def trigger_morning_card(x_cron_secret: str = Header(default="")):
    """Run prediction cycle AND send Telegram card. Use for cron-job.org trigger."""
    from wolf_app import _cron_ok, _morning_card_job  # late import — shared state + monkeypatch-safe
    if not _cron_ok(x_cron_secret):
        raise HTTPException(status_code=403)
    picks = _morning_card_job()
    return {"ok": True, "picks_generated": len(picks)}


@router.post("/api/reconcile")
def trigger_reconcile(x_cron_secret: str = Header(default="")):
    from wolf_app import _cron_ok  # late import — shared state + monkeypatch-safe
    if not _cron_ok(x_cron_secret):
        raise HTTPException(status_code=403)
    from core.prediction import reconcile_outcomes
    count = reconcile_outcomes()
    return {"ok": True, "resolved": count}


@router.get("/api/news/events")
def get_news_events(symbol: str = "", limit: int = 50):
    """Structured news events (PR #134). Read-only; newest first."""
    from core.news_events import recent_events_for_symbol, news_available
    lim = max(1, min(200, int(limit)))
    if symbol:
        events = recent_events_for_symbol(symbol, lookback_s=7 * 86400)[:lim]
        return {"ok": True, "symbol": symbol.upper(), "available": news_available(),
                "events": events}
    from core.db import db_conn
    from core.news_events import ensure_news_tables
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_news_tables(cur)
            cur.execute(
                """SELECT symbol, event_type, direction_hint, materiality, confidence,
                          confirmation_status, evidence, asof_ts
                   FROM ghost_news_events ORDER BY asof_ts DESC LIMIT %s""", (lim,))
            keys = ("symbol", "event_type", "direction_hint", "materiality",
                    "confidence", "confirmation_status", "evidence", "asof_ts")
            events = [dict(zip(keys, r)) for r in cur.fetchall()]
        return {"ok": True, "available": news_available(), "events": events}
    except Exception as exc:
        return JSONResponse(status_code=200, content={"ok": False, "error": str(exc)[:120]})


@router.post("/api/news/ingest")
def trigger_news_ingest(x_cron_secret: str = Header(default="")):
    """Manually run one news-ingest polling pass (PR #134). Cron-gated."""
    from wolf_app import _cron_ok  # late import — shared state + monkeypatch-safe
    if not _cron_ok(x_cron_secret):
        raise HTTPException(status_code=403)
    from core.news_ingest import run_news_ingest_cycle
    return run_news_ingest_cycle()


@router.get("/api/market/sessions")
def get_market_sessions_batch(symbols: str = "", max_fresh: int = -1):
    """Batch market sessions with freshness truth (PR #136, live-market audit P1+P2).

    Cache-first; at most `max_fresh` symbols hit providers per call so a full
    watchlist sweep can never trip the breakers. Partial results by design.
    """
    from core.market_sessions import get_market_sessions
    if symbols.strip():
        syms = [s for s in symbols.split(",") if s.strip()]
    else:
        from config.symbols import OFFICIAL_WATCHLIST
        syms = list(OFFICIAL_WATCHLIST)
    mf = None if max_fresh < 0 else max_fresh
    return get_market_sessions(syms, max_fresh=mf)


@router.get("/api/wallet")
def get_wallet():
    """Paper wallet summary — FAKE money, Cash-App-style view (PR #138)."""
    from core.paper_wallet import wallet_summary
    return wallet_summary()


@router.post("/api/wallet/config")
def set_wallet_config(request: Request, x_cron_secret: str = Header(default="")):
    """Reset the paper wallet with a new starting balance. Admin/cron gated."""
    from wolf_app import _ADMIN_COOKIE, _admin_token_valid, _cron_ok  # late import — shared state + monkeypatch-safe
    tok = request.cookies.get(_ADMIN_COOKIE, "")
    if not (_cron_ok(x_cron_secret) or _admin_token_valid(tok)):
        raise HTTPException(status_code=403, detail="admin login or cron secret required")
    bal = request.query_params.get("starting_balance")
    if bal is None:
        raise HTTPException(status_code=422, detail="starting_balance query param required")
    try:
        bal_f = float(bal)
    except ValueError:
        raise HTTPException(status_code=422, detail="starting_balance must be a number")
    goal = request.query_params.get("monthly_goal")
    goal_f = None
    if goal is not None:
        try:
            goal_f = float(goal)
        except ValueError:
            raise HTTPException(status_code=422, detail="monthly_goal must be a number")
    from core.paper_wallet import reset_wallet
    return reset_wallet(bal_f, monthly_goal=goal_f)


@router.post("/api/wallet/cycle")
def trigger_wallet_cycle(x_cron_secret: str = Header(default="")):
    """Run one paper-wallet engine pass manually. Cron-gated."""
    from wolf_app import _cron_ok  # late import — shared state + monkeypatch-safe
    if not _cron_ok(x_cron_secret):
        raise HTTPException(status_code=403)
    from core.paper_wallet import run_wallet_cycle
    return run_wallet_cycle()


@router.get("/api/report/daily")
def get_daily_report():
    """Consolidated 'today's report' (PR #157) — everything Ghost did + why,
    in one payload with a plain-English narrative. Read-only aggregation."""
    from core.daily_report import build_daily_report
    return build_daily_report()
