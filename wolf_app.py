import os, sys, time, logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from core.db import db_conn, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stdout,
)
LOGGER = logging.getLogger("ghost")
CRON_SECRET = os.getenv("CRON_SECRET", "")


def _has_any_v3_model():
    """True when at least one v3.2 TP/SL model exists (label_type=tp_sl_daily on meta_*)."""
    import json as _j
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM ghost_v3_model WHERE key ~ '^meta_'")
            for (val,) in cur.fetchall():
                try:
                    m = _j.loads(val)
                    if m.get("label_type") == "tp_sl_daily":
                        return True
                except Exception:
                    continue
        return False
    except Exception:
        return False


def _purge_v3_stale_or_weak():
    """Remove v3 models below V3_MIN_HOLDOUT_ACC or pre-v3.2 label schema."""
    import json as _j
    floor = float(os.getenv("V3_MIN_HOLDOUT_ACC", "0.55"))
    wf_floor = float(os.getenv("V3_MIN_WF_ACC_MEAN", "0.60"))
    min_edge = float(os.getenv("V3_MIN_EDGE", "0.05"))
    min_wf_folds = max(2, int(os.getenv("V3_MIN_WF_FOLDS", "3")))
    purged = 0
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM ghost_v3_model WHERE key LIKE 'meta_%'")
            for key, val in cur.fetchall():
                sym = key.replace("meta_", "")
                try:
                    meta = _j.loads(val)
                    weak = float(meta.get("accuracy", 0)) < floor or float(meta.get("edge", 0)) < min_edge
                    wf_folds = int(meta.get("wf_fold_count", 0))
                    wf_acc = float(meta.get("wf_acc_mean", meta.get("accuracy", 0)))
                    wf_edge = float(meta.get("wf_edge_mean", meta.get("edge", 0)))
                    wf_weak = wf_folds < min_wf_folds or wf_acc < wf_floor or wf_edge < min_edge
                    if meta.get("label_type") != "tp_sl_daily" or weak or wf_weak:
                        cur.execute(
                            "DELETE FROM ghost_v3_model WHERE key IN (%s,%s)",
                            (f"model_{sym}", f"meta_{sym}"),
                        )
                        purged += 1
                except Exception:
                    pass
        return purged
    except Exception:
        return 0


def _expire_open_picks_without_v3_model():
    """Expire active picks for symbols that currently have no v3 TP/SL model."""
    expired = 0
    now = int(time.time())
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT key FROM ghost_v3_model WHERE key LIKE 'meta_%'")
            model_syms = {row[0].replace("meta_", "") for row in cur.fetchall()}
            cur.execute(
                "SELECT id, symbol FROM predictions "
                "WHERE outcome IS NULL AND expires_at > %s",
                (now,),
            )
            rows = cur.fetchall()
            for pid, sym in rows:
                if (sym or "").upper() not in model_syms:
                    cur.execute(
                        "UPDATE predictions SET outcome='EXPIRED', resolved_at=%s WHERE id=%s",
                        (now, pid),
                    )
                    expired += 1
        return expired
    except Exception:
        return 0


def _morning_card_job():
    """Run prediction cycle and send morning Telegram card."""
    import datetime as _dt, pytz as _pytz, time as _t2
    from core.prediction import run_prediction_cycle
    from core.telegram import send_morning_card
    from core.db import db_conn
    # Dedup: only fire once per CT calendar day
    _ct_tz = _pytz.timezone("America/Chicago")
    _today_ct = _dt.datetime.now(_ct_tz).strftime("%Y-%m-%d")
    try:
        with db_conn() as _dc2:
            _cur_d = _dc2.cursor()
            _cur_d.execute("SELECT val FROM ghost_state WHERE key='last_morning_card_date'")
            _row = _cur_d.fetchone()
            if _row and _row[0] == _today_ct:
                LOGGER.info("Morning card already sent today ("+_today_ct+") — skipping duplicate")
                return []
    except Exception as _de:
        LOGGER.warning("Dedup check failed: "+str(_de)[:60])
    picks = run_prediction_cycle()
    # Get week stats
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cutoff = int(time.time()) - 7*86400
            cur.execute(
                "SELECT outcome, pnl_pct FROM predictions WHERE resolved_at > %s AND outcome IN ('WIN','LOSS') AND direction='UP'",
                (cutoff,)
            )
            rows = cur.fetchall()
            wins = sum(1 for r in rows if r[0] == "WIN")
            losses = len(rows) - wins
            # $100 per trade simulation
            # Correct P&L: $100 per trade simulation using pnl_pct
            pnl = sum((100 * (r[1] or 0) / 100) for r in rows)  # dollar gain per $100 bet
            # Scope to v2 predictions only (predicted_at is set, not NULL)
            cur.execute("SELECT outcome FROM predictions WHERE outcome IN ('WIN','LOSS') AND predicted_at IS NOT NULL ORDER BY id DESC LIMIT 2000")
            all_rows = cur.fetchall()
            # Only count WIN/LOSS — exclude EXPIRED from denominator
            resolved = [r for r in all_rows if r[0] in ("WIN","LOSS")]
            all_wins = sum(1 for r in resolved if r[0] == "WIN")
            alltime_wr = round(all_wins/len(resolved)*100,1) if resolved else 0
    except:
        wins, losses, pnl, alltime_wr = 0, 0, 0.0, 0
    week_stats = {"wins": wins, "losses": losses, "pnl_usd": pnl, "alltime_wr": alltime_wr}
    # Record card fire time for startup self-healing check
    try:
        import datetime as _dt2, pytz as _pytz2
        _ct2 = _pytz2.timezone("America/Chicago")
        _date_str = _dt2.datetime.now(_ct2).strftime("%Y-%m-%d")
        with db_conn() as _tc:
            _cur_tc = _tc.cursor()
            _cur_tc.execute(
                "INSERT INTO ghost_state(key,val) VALUES('last_morning_card_ts',%s) ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                (str(int(time.time())),),
            )
            _cur_tc.execute(
                "INSERT INTO ghost_state(key,val) VALUES('last_morning_card_date',%s) ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                (_date_str,),
            )
    except Exception:
        pass
    if picks:
        send_morning_card(picks, week_stats)
    else:
        try:
            with db_conn() as _oc:
                _cur2 = _oc.cursor()
                _cur2.execute(
                    "SELECT symbol,direction,confidence,entry_price,target_price,stop_price,expires_at FROM predictions WHERE outcome IS NULL AND expires_at > %s ORDER BY confidence DESC LIMIT 10",
                    (int(time.time()),)
                )
                _open = [{"symbol":r[0],"direction":r[1],"confidence":r[2],"entry_price":r[3],"target_price":r[4],"stop_price":r[5],"expires_at":r[6],"pos_size_pct":2.0} for r in _cur2.fetchall()]
            if _open:
                # Only send OPEN POSITIONS if picks have changed since last send
                import hashlib as _hl
                _pick_hash = _hl.md5(','.join(sorted(p['symbol'] for p in _open)).encode()).hexdigest()[:8]
                _hash_key = "last_open_pos_hash"
                try:
                    with db_conn() as _hc:
                        _hcur = _hc.cursor()
                        _hcur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
                        _hcur.execute("SELECT val FROM ghost_state WHERE key=%s", (_hash_key,))
                        _hrow = _hcur.fetchone()
                        _last_hash = _hrow[0] if _hrow else ""
                    if _pick_hash != _last_hash:
                        send_morning_card(_open, week_stats, is_update=True)
                        with db_conn() as _hc2:
                            _hc2.cursor().execute("INSERT INTO ghost_state(key,val) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val", (_hash_key, _pick_hash))
                except Exception:
                    send_morning_card(_open, week_stats, is_update=True)
            else:
                # Rate-limit: only send "no picks" message once per 4 hours
                import time as _rt
                _last_key = "last_no_picks_sent"
                _now = int(_rt.time())
                try:
                    with db_conn() as _rc:
                        _npcur = _rc.cursor()
                        _npcur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
                        _npcur.execute("SELECT val FROM ghost_state WHERE key=%s", (_last_key,))
                        _last_row = _npcur.fetchone()
                        _last_sent = int(_last_row[0]) if _last_row else 0
                    if _now - _last_sent > 14400:  # 4 hours
                        from core.telegram import _send
                        _send("Ghost Protocol v2 -- No new picks today. Market conditions not met.")
                        with db_conn() as _rc2:
                            _rc2.cursor().execute("INSERT INTO ghost_state(key,val) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val", (_last_key, str(_now)))
                except Exception: pass
        except Exception as _oe:
            LOGGER.warning("Open positions update failed: " + str(_oe))
    return picks

def _weekly_summary_job():
    """Fire weekly summary on Fridays at 4 PM CT (22:00 UTC). Skips other days."""
    import datetime, pytz
    ct = pytz.timezone("America/Chicago")
    now_ct = datetime.datetime.now(ct)
    # Only fire Friday (weekday=4) between 4:00-4:59 PM CT
    if not (now_ct.weekday() == 4 and now_ct.hour == 16):
        return  # Not Friday 4 PM CT, skip silently
    from core.telegram import send_weekly_summary
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cutoff = int(time.time()) - 7*86400
            cur.execute("SELECT outcome, pnl_pct FROM predictions WHERE predicted_at > %s AND outcome IN ('WIN','LOSS')", (cutoff,))
            rows = cur.fetchall()
        wins = sum(1 for r in rows if r[0] == "WIN")
        losses = len(rows) - wins
        pnl = sum((r[1] or 0) / 100 * 100 for r in rows)
        send_weekly_summary({"wins": wins, "losses": losses, "pnl": round(pnl, 2)})
        LOGGER.info("Weekly summary sent: " + str(wins) + "W/" + str(losses) + "L")
    except Exception as e:
        LOGGER.error("Weekly summary failed: " + str(e))

@asynccontextmanager
async def lifespan(app: FastAPI):
    LOGGER.info("Ghost Protocol v2 starting...")
    init_db()
    # Purge weak / legacy-schema models on startup
    try:
        purged = _auto_purge_bad_models()
        if purged: LOGGER.info(f"Boot purge: removed {purged} legacy ghost_models below floor")
        pv = _purge_v3_stale_or_weak()
        if pv: LOGGER.info(f"Boot v3 purge: removed {pv} stale or sub-floor TP/SL models")
        expired_orphans = _expire_open_picks_without_v3_model()
        if expired_orphans:
            LOGGER.info("Boot pick cleanup: expired %s active picks with no model", expired_orphans)
    except Exception as _bpe:
        LOGGER.warning("Boot purge failed: "+str(_bpe)[:60])

    # Self-healing: if app restarts between 8AM-noon CT and last card was >8h ago, fire now
    # Prevents silent card misses when Railway restarts during cron window
    try:
        import datetime as _sdt, pytz as _stz
        _ct = _stz.timezone("America/Chicago")
        _now_ct = _sdt.datetime.now(_ct)
        _hour_ct = _now_ct.hour
        if 8 <= _hour_ct < 12:  # morning window
            with db_conn() as _sc:
                _scur = _sc.cursor()
                _scur.execute("SELECT val FROM ghost_state WHERE key='last_morning_card_ts'")
                _row = _scur.fetchone()
                _last_ts = int(_row[0]) if _row else 0
                _hours_ago = (time.time() - _last_ts) / 3600
            if _hours_ago > 8:
                LOGGER.warning(f"Startup recovery: last card {_hours_ago:.1f}h ago, firing now (hour={_hour_ct} CT)")
                import asyncio as _aio
                _aio.get_event_loop().run_in_executor(None, _morning_card_job)
    except Exception as _se:
        LOGGER.warning(f"Startup card recovery failed: {_se}")

    from core import scheduler
    from core.prediction import reconcile_outcomes
    from core.news import run_news_cycle
    scheduler.register("morning_card", _morning_card_job, interval_s=86400)
    # Watchdog: real-time hit alerts every 5 minutes
    from core.watchdog import run_watchdog
    scheduler.register("watchdog", run_watchdog, interval_s=300)
    # Weekly summary: every Friday at 4 PM CT = 22:00 UTC = 79200s from midnight
    # Approximated as 7-day interval - fires on first Friday after deploy
    scheduler.register("weekly_summary", _weekly_summary_job, interval_s=604800)
    scheduler.register("reconcile", reconcile_outcomes, interval_s=900)
    # T19: Auto-refresh portfolio stock prices every 15 min
    from core.portfolio_routes import auto_refresh_portfolio_prices
    scheduler.register("portfolio_price_refresh", auto_refresh_portfolio_prices, interval_s=900)
    scheduler.register("news", run_news_cycle, interval_s=1800)
    # Weekly model retrain — keeps models fresh as market conditions change
    from core.signal_engine import train_and_validate as _tv
    def _weekly_retrain():
        try:
            from core.prediction import CRYPTO_SYMBOLS, STOCK_SYMBOLS
            syms = [(s.strip(), "crypto") for s in CRYPTO_SYMBOLS if s.strip()] + [
                (s.strip(), "stock") for s in STOCK_SYMBOLS if s.strip()
            ]
            trained, failed = 0, len(syms)
            try:
                # train_and_validate expects one list of (symbol, asset_type), not per-symbol calls
                _, acc_ratio, _ok = _tv(syms)
                trained = int(round(acc_ratio * len(syms))) if syms else 0
                failed = len(syms) - trained
            except Exception as _e:
                LOGGER.warning("Weekly retrain failed: " + str(_e)[:80])
            LOGGER.info("Weekly retrain complete: " + str(trained) + " trained, " + str(failed) + " failed")
            try:
                purged = _auto_purge_bad_models()
                pv = _purge_v3_stale_or_weak()
                LOGGER.info("Weekly retrain purge: legacy=%s v3=%s", purged, pv)
            except Exception as _pe:
                LOGGER.warning("Weekly purge failed: "+str(_pe)[:60])
        except Exception as _e:
            LOGGER.warning("Weekly retrain error: "+str(_e)[:80])
    scheduler.register("weekly_retrain", _weekly_retrain, interval_s=604800)
    scheduler.start()
    # Ghost v3: auto-train on startup if no model in DB
    def _startup_train():
        try:
            from core.signal_engine import train_and_validate
            import os
            if not _has_any_v3_model():
                LOGGER.info("No v3.2 TP/SL model found — training on startup...")
                crypto = [(s.strip(),"crypto") for s in os.getenv("CRYPTO_SYMBOLS","").split(",") if s.strip()]
                stocks = [(s.strip(),"stock") for s in os.getenv("STOCK_SYMBOLS","").split(",") if s.strip()]
                try:
                    from core.db import db_conn as _dbc
                    with _dbc() as _c:
                        _curp = _c.cursor()
                        _curp.execute("SELECT DISTINCT symbol, asset_type FROM user_portfolio")
                        for sym, at in _curp.fetchall():
                            _entry = (sym.strip().upper(), (at or "stock").strip().lower())
                            if _entry[1] == "crypto":
                                if _entry not in crypto:
                                    crypto.append(_entry)
                            else:
                                if _entry not in stocks:
                                    stocks.append((_entry[0], "stock"))
                except Exception:
                    pass
                m, acc, passed = train_and_validate(crypto + stocks)
                LOGGER.info(f"Startup training: acc={round(acc*100,1)}% passed={passed}")
                try:
                    purged = _auto_purge_bad_models()
                    pv = _purge_v3_stale_or_weak()
                    LOGGER.info(f"Startup purge: legacy={purged} v3={pv}")
                except Exception as _spe:
                    LOGGER.warning("Startup purge failed: "+str(_spe)[:60])
            else:
                LOGGER.info("v3 TP/SL model loaded from DB — ready")
                try:
                    purged = _auto_purge_bad_models()
                    pv = _purge_v3_stale_or_weak()
                    if purged or pv:
                        LOGGER.info(f"Startup cleanup: legacy={purged} v3={pv}")
                except Exception:
                    pass
        except Exception as _te:
            LOGGER.warning("Startup training failed: " + str(_te))
    import threading as _th
    _th.Thread(target=_startup_train, daemon=True).start()
    LOGGER.info("Ghost Protocol v2 ready.")
    yield
    scheduler.stop()

APP = FastAPI(title="Ghost Protocol v2", version="2.0.0", lifespan=lifespan)
APP.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Mount portfolio router — WOLF position tracking, price refresh, ghost predictions
from core.portfolio_routes import portfolio_router
APP.include_router(portfolio_router)



@APP.get("/api/diagnostics")
async def diagnostics():
    """Full logic correctness check — catches bugs /health misses."""
    import time as _t, json as _j2, datetime as _dt, pytz as _tz
    _now = int(_t.time())
    _passed = []
    _warnings = []
    _errors = []
    _score = 100

    # helpers — plain list appends, no nonlocal/closures
    def _ok(name, detail=""):
        _passed.append({"check": name, "detail": detail})

    def _warn(name, detail):
        _warnings.append({"check": name, "detail": detail})
        return 5

    def _fail(name, detail, deduct=10):
        _errors.append({"check": name, "detail": detail})
        return deduct

    try:
        from core.db import db_conn
        from core import scheduler as _sched
        from core.prices import check_feeds
        from core.signal_engine import FEATURE_COLS

        # ── 1. Scheduler integrity ────────────────────────────────────────
        _ws = _sched._tasks.get("weekly_summary")
        if not _ws:
            _score -= _fail("scheduler.weekly_summary", "not registered")
        elif _ws.interval_s != 604800:
            _score -= _fail("scheduler.weekly_summary", f"interval={_ws.interval_s}s want 604800 — will spam")
        else:
            _ok("scheduler.weekly_summary", f"1x at {_ws.interval_s}s")

        # Check for duplicate weekly_summary registrations
        _ws_count = sum(1 for k in _sched._tasks if k == "weekly_summary")
        if _ws_count > 1:
            _score -= _fail("scheduler.weekly_summary_dup", f"registered {_ws_count}x — hourly spam bug")

        _wd = _sched._tasks.get("watchdog")
        if not _wd:
            _score -= _fail("scheduler.watchdog", "not registered — picks never resolve", 20)
        else:
            _ok("scheduler.watchdog", f"every {_wd.interval_s}s")

        _mc = _sched._tasks.get("morning_card")
        if not _mc:
            _score -= _fail("scheduler.morning_card", "not registered — no 8 AM picks", 20)
        else:
            _ok("scheduler.morning_card", f"every {_mc.interval_s}s")

        # ── 2. Telegram dedup state ───────────────────────────────────────
        try:
            with db_conn() as _conn:
                _cur = _conn.cursor()
                _cur.execute("SELECT key, val FROM ghost_state WHERE key IN ('last_open_pos_hash','last_no_picks_sent')")
                _state = {r[0]: r[1] for r in _cur.fetchall()}
            if "last_open_pos_hash" in _state:
                _ok("telegram.open_pos_gate", f"hash={_state['last_open_pos_hash']}")
            else:
                _score -= _warn("telegram.open_pos_gate", "hash missing — open positions may spam on restart")
        except Exception as _e:
            _score -= _warn("telegram.open_pos_gate", f"state check failed: {_e}")

        # ── 3. Active pick expiry ─────────────────────────────────────────
        with db_conn() as _conn:
            _cur = _conn.cursor()
            _cur.execute("""SELECT symbol, asset_type, expires_at, predicted_at
                            FROM predictions WHERE outcome IS NULL AND expires_at > %s""", (_now,))
            _active = _cur.fetchall()

        _weekend = []
        _stale = []
        for _sym, _atype, _exp, _pred in _active:
            if _atype == "stock":
                _exp_dt = _dt.datetime.fromtimestamp(_exp, tz=_tz.timezone("America/Chicago"))
                if _exp_dt.weekday() in (5, 6):
                    _weekend.append(f"{_sym} expires {_exp_dt.strftime('%a')}")
            if (_now - _pred) > 96 * 3600:
                _stale.append(f"{_sym} open {int((_now-_pred)/3600)}h")

        if _weekend:
            _score -= _fail("picks.weekend_expiry", f"Stock picks expiring on weekend: {_weekend}", 15)
        else:
            _ok("picks.weekend_expiry", "no stock picks expiring on weekend")

        if _stale:
            _score -= _warn("picks.stale_open", f"Picks open >96h: {_stale}")
        else:
            _ok("picks.stale_open", "all picks within 96h window")

        # ── 4. Resolution rate (7-day window) ────────────────────────────
        _7d = _now - 7 * 86400
        with db_conn() as _conn:
            _cur = _conn.cursor()
            _cur.execute("""SELECT outcome, COUNT(*) FROM predictions
                            WHERE outcome IN ('WIN','LOSS','EXPIRED')
                            AND predicted_at > %s
                            GROUP BY outcome""", (_7d,))
            _7d_rows = {r[0]: r[1] for r in _cur.fetchall()}
            # All-time win rate
            _cur.execute("SELECT outcome, COUNT(*) FROM predictions WHERE outcome IN ('WIN','LOSS') GROUP BY outcome")
            _at_rows = {r[0]: r[1] for r in _cur.fetchall()}

        _7w = _7d_rows.get("WIN", 0)
        _7l = _7d_rows.get("LOSS", 0)
        _7e = _7d_rows.get("EXPIRED", 0)
        _7tot = _7w + _7l + _7e
        _7res = _7w + _7l

        if _7tot > 0:
            _res_rate = round(_7res / _7tot * 100, 1)
            if _res_rate < 10:
                _score -= _fail("resolution.rate", f"Last 7d: {_res_rate}% resolve ({_7w}W/{_7l}L/{_7e}E) — feed/expiry broken", 20)
            elif _res_rate < 30:
                _score -= _warn("resolution.rate", f"Last 7d: {_res_rate}% resolve ({_7w}W/{_7l}L/{_7e}E)")
            else:
                _ok("resolution.rate", f"Last 7d: {_res_rate}% ({_7w}W/{_7l}L/{_7e}E)")
        else:
            _score -= _warn("resolution.rate", "No resolved picks in last 7 days")

        _atw = _at_rows.get("WIN", 0)
        _atl = _at_rows.get("LOSS", 0)
        _at_wr = round(_atw / (_atw + _atl) * 100, 1) if (_atw + _atl) > 0 else 0
        _ok("win_rate.alltime", f"{_at_wr}% WIN/(WIN+LOSS) all-time ({_atw}W/{_atl}L)")

        # ── 5. Loss streak ────────────────────────────────────────────────
        with db_conn() as _conn:
            _cur = _conn.cursor()
            _cur.execute("""SELECT outcome FROM predictions
                            WHERE outcome IN ('WIN','LOSS')
                            ORDER BY resolved_at DESC LIMIT 10""")
            _recent_outcomes = [r[0] for r in _cur.fetchall()]

        _streak = 0
        for _o in _recent_outcomes:
            if _o == "LOSS":
                _streak += 1
            else:
                break
        if _streak >= 5:
            _score -= _fail("signal.loss_streak", f"{_streak} consecutive losses — retrain needed", 15)
        elif _streak >= 3:
            _score -= _warn("signal.loss_streak", f"{_streak} consecutive losses")
        else:
            _ok("signal.loss_streak", f"{_streak} consecutive losses" if _streak else "no streak")

        # ── 6. Model freshness and engine version ─────────────────────────
        with db_conn() as _conn:
            _cur = _conn.cursor()
            _cur.execute("SELECT key, value FROM ghost_v3_model WHERE key LIKE 'meta_%'")
            _model_rows = _cur.fetchall()

        _stale_models = []
        _old_engine = []
        _drift = []
        _weak_wf = []
        _expected_features = len(FEATURE_COLS)
        for _k, _v in _model_rows:
            _sym = _k.replace("meta_", "")
            _m = _j2.loads(_v)
            _age = (_now - _m.get("trained_at", 0)) / 86400
            if _age > 14:
                _stale_models.append(f"{_sym} ({_age:.0f}d)")
            _engine = _m.get("engine_version", "v3.0")
            if ("v3.1_ema_adx_atr_obv_stoch" not in _engine) and ("v3.2_tp_sl_daily" not in _engine):
                _old_engine.append(_sym)
            _fc = _m.get("feature_cols", [])
            if len(_fc) != _expected_features:
                _drift.append(f"{_sym}: {len(_fc)} vs {_expected_features}")
            _wf_folds = int(_m.get("wf_fold_count", 0))
            _wf_acc = float(_m.get("wf_acc_mean", _m.get("accuracy", 0)))
            _wf_edge = float(_m.get("wf_edge_mean", _m.get("edge", 0)))
            _wf_min_folds = max(2, int(os.getenv("V3_MIN_WF_FOLDS", "3")))
            _wf_floor = float(os.getenv("V3_MIN_WF_ACC_MEAN", "0.60"))
            _wf_edge_floor = float(os.getenv("V3_MIN_EDGE", "0.05"))
            if _wf_folds < _wf_min_folds or _wf_acc < _wf_floor or _wf_edge < _wf_edge_floor:
                _weak_wf.append(_sym)

        if _stale_models:
            _score -= _warn("models.freshness", f"Stale models (>14d): {_stale_models}")
        else:
            _ok("models.freshness", f"{len(_model_rows)} models within 14 days")

        if _old_engine:
            _score -= _warn("models.engine", f"Unrecognized engine version: {_old_engine}")
        else:
            _ok("models.engine", f"all {len(_model_rows)} models on accepted engines (v3.1/v3.2)")

        if _drift:
            _score -= _warn("models.feature_drift", f"Feature mismatch: {_drift}")
        else:
            _ok("models.feature_drift", f"all models match {_expected_features}-feature engine")

        if _weak_wf:
            _score -= _warn("models.walk_forward", f"Models below walk-forward floor: {_weak_wf}")
        else:
            _ok("models.walk_forward", "all models pass walk-forward floor")

        # Active picks with no model
        _active_syms = set(r[0] for r in _active)
        _model_syms = set(k.replace("meta_","") for k,_ in _model_rows)
        _no_model = _active_syms - _model_syms
        if _no_model:
            _score -= _warn("models.coverage", f"Active picks with no model: {list(_no_model)}")
        else:
            _ok("models.coverage", "all active picks have v3 models")

        # ── 7. Confidence calibration ─────────────────────────────────────
        with db_conn() as _conn:
            _cur = _conn.cursor()
            _cur.execute("""SELECT confidence, outcome FROM predictions
                            WHERE outcome IN ('WIN','LOSS') AND confidence IS NOT NULL
                            ORDER BY resolved_at DESC LIMIT 100""")
            _cal = _cur.fetchall()

        if len(_cal) >= 10:
            _hi = [(c,o) for c,o in _cal if c >= 0.9]
            _lo = [(c,o) for c,o in _cal if c < 0.9]
            _hi_wr = round(sum(1 for c,o in _hi if o=="WIN")/len(_hi)*100) if _hi else None
            _lo_wr = round(sum(1 for c,o in _lo if o=="WIN")/len(_lo)*100) if _lo else None
            if _hi_wr is not None and _lo_wr is not None:
                if _hi_wr < _lo_wr:
                    _score -= _warn("confidence.calibration",
                        f"HIGH conf {_hi_wr}% WR < LOW conf {_lo_wr}% WR — confidence not meaningful")
                else:
                    _ok("confidence.calibration", f"high {_hi_wr}% WR vs low {_lo_wr}% WR — calibrated")

        # ── 8. Price feeds ────────────────────────────────────────────────
        _feeds = check_feeds()
        _working = sum(1 for v in _feeds.values() if v is True)
        _total = sum(1 for v in _feeds.values() if isinstance(v, bool))
        if _working == 0:
            _score -= _fail("price_feeds", "0 feeds responding — watchdog blind", 20)
        elif _working < 2:
            _score -= _warn("price_feeds", f"Only {_working}/{_total} feeds")
        else:
            _ok("price_feeds", f"{_working}/{_total} feeds responding")

    except Exception as _ex:
        _errors.append({"check": "diagnostics.crashed", "detail": str(_ex)})

    # morning_card.today: flag if no card today after 9AM CT
    try:
        import datetime as _mcdt, pytz as _mcpytz
        _mc_ct = _mcpytz.timezone("America/Chicago")
        _mc_now = _mcdt.datetime.now(_mc_ct)
        _mc_today = _mc_now.strftime("%Y-%m-%d")
        _mc_last = None
        try:
            with db_conn() as _mc_conn:
                _mc_cur = _mc_conn.cursor()
                _mc_cur.execute("SELECT val FROM ghost_state WHERE key='last_morning_card_date'")
                _mc_row = _mc_cur.fetchone()
                _mc_last = _mc_row[0] if _mc_row else None
        except Exception: pass
        if _mc_now.hour >= 9:
            if _mc_last == _mc_today:
                _passed.append({"check":"morning_card.today","detail":"Card sent today "+_mc_today,"status":"pass"})
            else:
                _errors.append({"check":"morning_card.today","detail":"No card today ("+_mc_today+") last:"+str(_mc_last),"status":"error"})
                _score -= 10
        else:
            _passed.append({"check":"morning_card.today","detail":"Before 9AM CT — OK","status":"pass"})
    except Exception as _mc_ex:
        _warnings.append({"check":"morning_card.today","detail":"Cannot verify: "+str(_mc_ex)[:60],"status":"warning"})

    _score = max(0, _score)
    return {
        "score": _score,
        "status": "healthy" if _score >= 80 else "degraded" if _score >= 50 else "critical",
        "checks_passed": len(_passed),
        "warnings": len(_warnings),
        "errors": len(_errors),
        "details": {"passed": _passed, "warnings": _warnings, "errors": _errors},
        "timestamp": _now,
    }



def _auto_purge_bad_models():
    """Purge all sub-52% accuracy models from DB. Called after every retrain."""
    try:
        MIN_ACC = 0.52
        from core.db import db_conn as _dbc
        import json as _j
        with _dbc() as _c:
            cur = _c.cursor()
            cur.execute("DELETE FROM ghost_models WHERE symbol='ARB'")
            cur.execute("SELECT id, symbol, metadata FROM ghost_models")
            rows = cur.fetchall()
            purged = 0
            for rid, sym, meta in rows:
                try:
                    m = _j.loads(meta) if isinstance(meta, str) else (meta or {})
                    acc = float(m.get('accuracy', 1.0))
                    if acc < MIN_ACC:
                        cur.execute("DELETE FROM ghost_models WHERE id=%s", (rid,))
                        purged += 1
                except Exception: pass
        return purged
    except Exception: return 0

@APP.post("/api/admin/delete-model")
async def delete_model(x_cron_secret: str = Header(None)):
    """Delete models below accuracy threshold. Purges bad models so they stop generating picks."""
    if x_cron_secret != CRON_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    import json as _j
    from core.db import db_conn
    deleted = []
    kept = []
    ACCURACY_FLOOR = float(os.getenv("V3_MIN_HOLDOUT_ACC", "0.55"))
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            # Always delete ARB (43 bars, never trains)
            cur.execute("DELETE FROM ghost_v3_model WHERE key IN ('model_ARB','meta_ARB')")
            deleted.append("ARB")
            # Delete all models below accuracy floor
            cur.execute("SELECT key, value FROM ghost_v3_model WHERE key LIKE 'meta_%'")
            rows = cur.fetchall()
            for key, val in rows:
                sym = key.replace("meta_", "")
                try:
                    meta = _j.loads(val)
                    acc = meta.get("accuracy", 0)
                    if acc < ACCURACY_FLOOR:
                        cur.execute("DELETE FROM ghost_v3_model WHERE key IN (%s, %s)",
                                   (f"model_{sym}", f"meta_{sym}"))
                        deleted.append(f"{sym}(acc={round(acc*100,1)}%)")
                    else:
                        kept.append(f"{sym}(acc={round(acc*100,1)}%)")
                except Exception: pass
        return {"ok": True, "deleted": deleted, "kept": kept}
    except Exception as e:
        return {"ok": False, "error": str(e)}



@APP.post("/api/admin/fix-stock-expiry")
async def fix_stock_expiry(x_cron_secret: str = Header(None)):
    """Fix stock picks that were created before the weekend-expiry fix and expire before market open."""
    if x_cron_secret != CRON_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    import time as _ft, datetime as _fdt, pytz as _ftz
    from core.db import db_conn
    _ct = _ftz.timezone("America/Chicago")
    _now = int(_ft.time())
    updated = []
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            # Find open stock picks expiring before 4 PM on their expiry day
            cur.execute("""SELECT id, symbol, expires_at FROM predictions
                           WHERE outcome IS NULL AND asset_type='stock'
                           AND expires_at > %s""", (_now,))
            picks = cur.fetchall()
            for pid, sym, exp_ts in picks:
                exp_dt = _fdt.datetime.fromtimestamp(exp_ts, tz=_ct)
                # If expiry hour is before 16 (4 PM), push to 4 PM same day
                if exp_dt.hour < 16:
                    fixed_dt = exp_dt.replace(hour=16, minute=0, second=0, microsecond=0)
                    # Skip weekends
                    if fixed_dt.weekday() == 5: fixed_dt += _fdt.timedelta(days=2)
                    elif fixed_dt.weekday() == 6: fixed_dt += _fdt.timedelta(days=1)
                    fixed_ts = int(fixed_dt.timestamp())
                    cur.execute("UPDATE predictions SET expires_at=%s WHERE id=%s", (fixed_ts, pid))
                    updated.append(f"{sym}: {exp_dt.strftime('%a %I:%M %p')} -> {fixed_dt.strftime('%a %I:%M %p')} CT")
        return {"ok": True, "fixed": len(updated), "details": updated}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@APP.get("/health")
def health():
    import os, time as _t
    from core.prices import check_feeds
    from core import scheduler
    issues = []
    warnings = []

    # 1. DB
    db_ok = False
    try:
        with db_conn() as conn: conn.cursor().execute("SELECT 1")
        db_ok = True
    except Exception as e:
        issues.append("DB failed: " + str(e)[:60])

    # 2. Price feeds
    feeds = {"coingecko": False, "coinbase": False, "binance": False, "polygon": False, "summary": "0/4 feeds responding"}
    try:
        feeds = check_feeds()
        feeds_ok = sum(1 for k,v in feeds.items() if k != "summary" and v)
        if feeds_ok < 2:
            warnings.append(feeds.get("summary", "<2 feeds responding"))
    except Exception: pass

    # 3. Prediction freshness
    freshness_min = None
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT predicted_at FROM predictions WHERE predicted_at IS NOT NULL ORDER BY predicted_at DESC LIMIT 1")
            row = cur.fetchone()
            if row and row[0]:
                freshness_min = int((_t.time() - float(row[0])) / 60)
        if freshness_min and freshness_min > 2880:  # 48h — only flag if no picks for 2+ days
            issues.append("Predictions stale: " + str(freshness_min) + "m")
    except Exception: pass

    # 4. Telegram
    tg_ok = bool(os.getenv("TELEGRAM_BOT_TOKEN","") and os.getenv("TELEGRAM_CHAT_ID",""))
    if not tg_ok:
        issues.append("Telegram credentials missing")

    # 5. Open picks + dedup
    open_picks = 0
    dedup_blocked = False
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM predictions WHERE outcome IS NULL AND expires_at > %s", (_t.time(),))
            open_picks = cur.fetchone()[0]
        total_syms = len([s for s in os.getenv("CRYPTO_SYMBOLS","").split(",") if s.strip()]) +                      len([s for s in os.getenv("STOCK_SYMBOLS","").split(",") if s.strip()])
        if open_picks >= total_syms > 0:
            dedup_blocked = True
            warnings.append("Dedup blocking all " + str(total_syms) + " symbols")
        if dedup_blocked:
            try:
                with db_conn() as _fc:
                    _fc.cursor().execute(
                        "UPDATE predictions SET outcome='EXPIRED', resolved_at=%s WHERE outcome IS NULL AND predicted_at < %s",
                        (int(_t.time()), int(_t.time() - 50*3600))
                    )
            except Exception: pass
    except Exception: pass

    # 6. Confidence floor
    conf_floor = float(os.getenv("MIN_ALERT_CONFIDENCE","0.75"))
    if conf_floor < 0.70:
        warnings.append("Confidence floor " + str(conf_floor) + " is low")

    # 7. Tasks
    tasks = []
    last_card_min = None
    try:
        tasks = scheduler.status()
        mc = next((t for t in tasks if t["name"] == "morning_card"), None)
        if mc:
            last_card_min = int(mc.get("last_run_ago_s", 0) / 60)
            if last_card_min > 1440:
                issues.append("Morning card last ran " + str(last_card_min) + "m ago")
    except Exception: pass

    score = max(0, min(100, 100 - len(issues)*20 - len(warnings)*5))
    status_str = "healthy" if score >= 80 and not issues else "degraded" if score >= 50 else "critical"
    return {
        "status": status_str, "score": score, "db": db_ok,
        "telegram_configured": tg_ok, "predictions_freshness_min": freshness_min,
        "open_picks": open_picks, "dedup_blocked": dedup_blocked,
        "last_morning_card_min": last_card_min, "confidence_floor": conf_floor,
        "price_feeds": feeds, "tasks": tasks, "issues": issues, "warnings": warnings,
    }


@APP.get("/api/schema")
def get_schema():
    tables = {}
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema='public' ORDER BY table_name, ordinal_position")
        for table, col in cur.fetchall():
            if table not in tables: tables[table] = []
            tables[table].append(col)
    return {"ok": True, "tables": tables}

def _norm_pred(r):
    _conf = r.get("confidence") or r.get("confidence_score") or 0
    if _conf >= 0.90:   _pos = 5.0
    elif _conf >= 0.85: _pos = 4.0
    elif _conf >= 0.80: _pos = 3.0
    elif _conf >= 0.75: _pos = 2.0
    else:               _pos = 1.0
    return {
        "id": r.get("id"),
        "symbol": r.get("symbol",""),
        "direction": r.get("direction",""),
        "confidence": _conf,
        "pos_size_pct": _pos,
        "entry_price": r.get("entry_price") or r.get("entry") or 0,
        "target_price": r.get("target_price") or r.get("target") or 0,
        "stop_price": r.get("stop_price") or r.get("stop") or 0,
        "predicted_at": r.get("predicted_at") or r.get("run_at") or 0,
        "expires_at": r.get("expires_at") or 0,
        "outcome": r.get("outcome") or r.get("result"),
        "exit_price": r.get("exit_price"),
        "pnl_pct": r.get("pnl_pct") or r.get("pnl"),
        "asset_type": r.get("asset_type","crypto"),
    }

@APP.get("/api/picks")
def get_picks():
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM predictions ORDER BY id DESC LIMIT 50")
            cols = [d[0] for d in cur.description]
            rows = [_norm_pred(dict(zip(cols, r))) for r in cur.fetchall()]
        active = [r for r in rows if r["outcome"] is None]
        resolved = [r for r in rows if r["outcome"] is not None]
        wins = sum(1 for r in resolved if r["outcome"] == "WIN")
        total = len(resolved)
        return {"ok": True, "active": active, "recent": resolved[:20],
                "accuracy_pct": round(wins/total*100,1) if total else 0,
                "wins": wins, "losses": total-wins, "total": total}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@APP.get("/api/history")
def get_history(limit: int = 200):
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM predictions ORDER BY id DESC LIMIT %s", (limit,))
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

@APP.get("/api/news")
def get_news():
    try:
        from core.news import get_recent_articles
        articles = get_recent_articles(20)
        return {"ok": True, "articles": articles, "count": len(articles)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@APP.post("/api/run-predictions")
def trigger_predictions(x_cron_secret: str = Header(default="")):
    """Run prediction cycle only. Does NOT send Telegram (use /api/morning-card for that)."""
    if CRON_SECRET and x_cron_secret != CRON_SECRET:
        raise HTTPException(status_code=403)
    from core.prediction import run_prediction_cycle
    picks = run_prediction_cycle()
    return {"ok": True, "picks_generated": len(picks), "picks": picks}

@APP.post("/api/morning-card")
def trigger_morning_card(x_cron_secret: str = Header(default="")):
    """Run prediction cycle AND send Telegram card. Use for cron-job.org trigger."""
    if CRON_SECRET and x_cron_secret != CRON_SECRET:
        raise HTTPException(status_code=403)
    picks = _morning_card_job()
    return {"ok": True, "picks_generated": len(picks)}

@APP.post("/api/reconcile")
def trigger_reconcile(x_cron_secret: str = Header(default="")):
    if CRON_SECRET and x_cron_secret != CRON_SECRET:
        raise HTTPException(status_code=403)
    from core.prediction import reconcile_outcomes
    count = reconcile_outcomes()
    return {"ok": True, "resolved": count}

@APP.post("/api/test-alert")
def test_alert():
    """Send test message to Telegram to verify connection."""
    from core.telegram import send_test
    ok = send_test()
    return {"ok": ok, "message": "Test alert sent to Telegram + Discord"}

@APP.post("/api/retrain")
def retrain(x_cron_secret: str = Header(default="")):
    """Train XGBoost on ghost_prediction_outcomes. Inline - no import needed."""
    if CRON_SECRET and x_cron_secret != CRON_SECRET:
        raise HTTPException(status_code=403)
    try:
        import xgboost as xgb, numpy as np, json as _json, time as _time
        CRYPTO = {'BTC','ETH','SOL','XRP','ADA','DOT','LINK','AVAX','MATIC','LTC','ATOM','UNI','TRX','BCH','CHZ','TURBO','ZEC','RNDR'}
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
            X.append([float(conf), 1.0 if direction=="UP" else 0.0, 1.0 if sym in CRYPTO else 0.0,
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

@APP.get("/api/price/{symbol}")
def get_price_endpoint(symbol: str, asset_type: str = "crypto"):
    from core.prices import get_price
    price = get_price(symbol, asset_type)
    return {"ok": price is not None, "symbol": symbol, "price": price}

@APP.post("/api/migrate-outcomes")
def migrate_outcomes(x_cron_secret: str = Header(default="")):
    """INSERT from ghost_prediction_outcomes (13k rows) into predictions."""
    if CRON_SECRET and x_cron_secret != CRON_SECRET:
        raise HTTPException(status_code=403)
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO predictions
                    (symbol, direction, confidence, entry_price, target_price, stop_price,
                     run_at, predicted_at, expires_at, resolved_at, outcome, exit_price, pnl_pct, asset_type)
                SELECT
                    gpo.symbol,
                    COALESCE(gpo.predicted_direction, 'UP'),
                    COALESCE(gpo.predicted_confidence, 0.5),
                    gpo.price_at_prediction,
                    gpo.price_at_prediction * 1.06,
                    gpo.price_at_prediction * 0.97,
                    EXTRACT(EPOCH FROM gpo.created_at)::BIGINT,
                    EXTRACT(EPOCH FROM gpo.created_at)::BIGINT,
                    EXTRACT(EPOCH FROM COALESCE(gpo.closed_at, gpo.created_at + INTERVAL '48 hours'))::BIGINT,
                    EXTRACT(EPOCH FROM gpo.closed_at)::BIGINT,
                    CASE WHEN gpo.hit_direction = 1 THEN 'WIN' ELSE 'LOSS' END,
                    gpo.price_at_resolution,
                    gpo.realized_move_pct,
                    CASE WHEN gpo.symbol = ANY(ARRAY['BTC','ETH','SOL','XRP','ADA','DOT','LINK','AVAX','MATIC','LTC','ATOM','UNI','TRX','BCH','CHZ','TURBO','ZEC','RNDR']) THEN 'crypto' ELSE 'stock' END
                FROM ghost_prediction_outcomes gpo
                WHERE gpo.hit_direction IS NOT NULL
                AND gpo.price_at_prediction IS NOT NULL
                AND gpo.price_at_prediction > 0
                AND gpo.closed_at IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM predictions p2
                    WHERE p2.symbol = gpo.symbol
                    AND p2.resolved_at = EXTRACT(EPOCH FROM gpo.closed_at)::BIGINT
                    AND p2.outcome IS NOT NULL
                )
            """)
            inserted = cur.rowcount
            cur.execute("SELECT outcome, COUNT(*) FROM predictions WHERE outcome IS NOT NULL GROUP BY outcome")
            counts = {r[0]: r[1] for r in cur.fetchall()}
            cur.execute("SELECT COUNT(*) FROM ghost_prediction_outcomes WHERE hit_direction IS NOT NULL")
            source_rows = cur.fetchone()[0]
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True, "inserted": inserted, "source_rows": source_rows, "outcome_counts": counts}

@APP.get("/api/stats/v32")
def get_stats_v32():
    """Win rate for v3.2-era predictions only (post 2026-04-05). Use this for decisioning, not all-time."""
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            # Only count predictions made after v3.2 deploy, BUY only, WIN/LOSS only
            cur.execute("""
                SELECT outcome, COUNT(*) FROM predictions
                WHERE direction='UP'
                AND predicted_at >= '2026-04-05 00:00:00'
                AND outcome IN ('WIN','LOSS')
                GROUP BY outcome
            """)
            rows = {r[0]: r[1] for r in cur.fetchall()}
            wins = rows.get('WIN', 0)
            losses = rows.get('LOSS', 0)
            total = wins + losses
            wr = round(wins / total * 100, 1) if total else 0
            # Also get open picks from v3.2 era
            cur.execute("""
                SELECT COUNT(*) FROM predictions
                WHERE direction='UP'
                AND predicted_at >= '2026-04-05 00:00:00'
                AND outcome IS NULL
                AND expires_at > %s
            """, (int(__import__('time').time()),))
            open_picks = cur.fetchone()[0]
        return {"ok": True, "era": "v3.2", "since": "2026-04-05",
                "wins": wins, "losses": losses, "total": total,
                "win_rate_pct": wr, "open_picks": open_picks,
                "verdict": "on_track" if wr >= 55 else "watch" if wr >= 45 else "review"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}

@APP.get("/api/stats")
def get_stats():
    """Overall accuracy stats across all sources."""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT outcome, COUNT(*) FROM predictions WHERE outcome IN ('WIN','LOSS') AND predicted_at IS NOT NULL GROUP BY outcome")
        rows = {r[0]: r[1] for r in cur.fetchall()}
        wins = rows.get("WIN", 0)
        losses = rows.get("LOSS", 0)
        total = wins + losses
        cur.execute("SELECT COUNT(*) FROM predictions WHERE outcome IS NULL AND entry_price IS NOT NULL")
        open_count = cur.fetchone()[0]
    return {"ok": True, "wins": wins, "losses": losses, "total": total,
            "win_rate_pct": round(wins/total*100,1) if total else 0,
            "open_positions": open_count}

@APP.get("/api/db-probe")
def db_probe():
    """Count rows in v1 outcome tables to find where data lives."""
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
        except: pass
        try:
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='ghost_predictions' ORDER BY ordinal_position")
            counts["ghost_predictions_cols"] = [r[0] for r in cur.fetchall()][:10]
        except: pass
        try:
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='money_game_trades' ORDER BY ordinal_position")
            counts["money_game_trades_cols"] = [r[0] for r in cur.fetchall()]
        except: pass
    return {"ok": True, "counts": counts}

@APP.get("/api/symbol-accuracy")
def symbol_accuracy():
    """Show per-symbol win rates from ghost_prediction_outcomes. Ground truth."""
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
    return {"ok": True, "total_symbols": len(symbols), "symbols_with_edge": len(edges), "data": symbols}

@APP.post("/api/clean-garbage")
def clean_garbage(x_cron_secret: str = Header(default="")):
    """Delete broken predictions from the $0.50 entry price bug."""
    if CRON_SECRET and x_cron_secret != CRON_SECRET:
        raise HTTPException(status_code=403)
    with db_conn() as conn:
        cur = conn.cursor()
        # Count first
        cur.execute("SELECT COUNT(*) FROM predictions WHERE entry_price BETWEEN 0.49 AND 0.51 AND predicted_at IS NOT NULL")
        garbage_count = cur.fetchone()[0]
        # Delete predictions where entry_price was $0.50 (the placeholder confidence value)
        cur.execute("DELETE FROM predictions WHERE entry_price BETWEEN 0.49 AND 0.51 AND predicted_at IS NOT NULL")
        deleted = cur.rowcount
        # Recount clean predictions
        cur.execute("SELECT outcome, COUNT(*) FROM predictions WHERE outcome IN ('WIN','LOSS') AND predicted_at IS NOT NULL GROUP BY outcome")
        counts = {r[0]: r[1] for r in cur.fetchall()}
    return {"ok": True, "deleted": deleted, "remaining": counts}

@APP.post("/api/watchdog")
def run_watchdog(x_cron_secret: str = Header(default="")):
    """Check open picks vs live prices. Send Telegram alert if target or stop hit."""
    if CRON_SECRET and x_cron_secret != CRON_SECRET:
        raise HTTPException(status_code=403)
    from core.prediction import reconcile_outcomes
    from core.telegram import send_position_alert
    from core.prices import get_price
    from core.db import db_conn
    import time
    alerted = []
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id,symbol,direction,entry_price,target_price,stop_price,asset_type,confidence FROM predictions"
                " WHERE outcome IS NULL AND predicted_at IS NOT NULL AND entry_price > 0"
                " AND target_price IS NOT NULL AND stop_price IS NOT NULL LIMIT 50"
            )
            open_picks = cur.fetchall()
        for pred_id, symbol, direction, entry, target, stop, asset_type, conf in open_picks:
            price = get_price(symbol, asset_type or "crypto")
            if not price: continue
            hit = None
            if direction == "UP":
                if price >= target: hit = "WIN"
                elif price <= stop: hit = "LOSS"
            else:
                if price <= target: hit = "WIN"
                elif price >= stop: hit = "LOSS"
            if hit:
                pnl = (price-entry)/entry*100 if direction=="UP" else (entry-price)/entry*100
                with db_conn() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "UPDATE predictions SET outcome=%s,exit_price=%s,pnl_pct=%s,resolved_at=%s WHERE id=%s",
                        (hit, price, round(pnl,3), int(time.time()), pred_id))
                try:
                    send_position_alert(symbol, direction, entry, price, hit, round(pnl,2), conf or 0)
                except Exception as e:
                    LOGGER.error("watchdog alert " + symbol + ": " + str(e))
                alerted.append({"symbol":symbol,"outcome":hit,"pnl":round(pnl,2)})
    except Exception as e:
        return JSONResponse({"ok":False,"error":str(e)}, status_code=500)
    return {"ok": True, "alerted": len(alerted), "hits": alerted}

@APP.get("/api/debug-signal/{symbol}")
def debug_signal(symbol: str):
    """Step-by-step trace of signal logic - exposes every intermediate value."""
    from core.db import db_conn
    from core.prices import get_price
    import os, traceback
    result = {"symbol": symbol, "steps": []}
    try:
        price = get_price(symbol, "crypto")
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
    price = get_price(symbol, "crypto")
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

@APP.get("/cockpit")
def cockpit():
    html = ("<h1>Ghost Protocol v2</h1><ul>"
           "<li><a href=/health>/health</a></li>"
           "<li><a href=/api/picks>/api/picks</a></li>"
           "<li><a href=/api/history>/api/history</a></li>"
           "<li><a href=/api/news>/api/news</a></li>"
           "<li><a href=/api/schema>/api/schema</a></li>"
           "</ul><p>Full dashboard coming Week 4.</p>")
    return HTMLResponse(html)

if os.path.exists("static"):
    APP.mount("/static", StaticFiles(directory="static"), name="static")

# ════════════════════════════════════════════════════════════
# GHOST v3 ENDPOINTS — Backtested signal engine
# ════════════════════════════════════════════════════════════

@APP.get("/api/v3/status")
def v3_status():
    """Model status — accuracy, edge over random, top features."""
    from core.signal_engine import get_model_status
    return get_model_status()

@APP.post("/api/v3/train")
def v3_train(x_cron_secret: str = Header(default="")):
    """
    Train v3 XGBoost model on 6mo historical data.
    Takes 2-5 minutes. Runs in background, returns immediately.
    Model only deployed if accuracy > 52% on holdout.
    """
    import os
    if x_cron_secret != os.getenv("CRON_SECRET",""):
        return JSONResponse({"ok":False,"error":"Forbidden"}, status_code=403)
    import threading
    def _train():
        try:
            from core.signal_engine import train_and_validate
            import os
            crypto = [(s.strip(), "crypto") for s in os.getenv("CRYPTO_SYMBOLS","").split(",") if s.strip()]
            stocks = [(s.strip(), "stock") for s in os.getenv("STOCK_SYMBOLS","").split(",") if s.strip()]
            # Also include portfolio symbols
            try:
                from core.db import db_conn
                with db_conn() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT DISTINCT symbol, asset_type FROM user_portfolio")
                    for sym, at in cur.fetchall():
                        entry = (sym.strip(), (at or "stock").strip())
                        if entry not in crypto and entry not in stocks:
                            stocks.append(entry)
            except Exception: pass
            model, accuracy, passed = train_and_validate(crypto + stocks)
            LOGGER.info(f"v3 training complete: accuracy={round(accuracy*100,1)}% passed={passed}")
            try:
                purged = _auto_purge_bad_models()
                pv = _purge_v3_stale_or_weak()
                LOGGER.info(f"Post-train purge: legacy={purged} v3={pv}")
            except Exception as _pe:
                LOGGER.warning("Auto-purge after train failed: "+str(_pe)[:60])
        except Exception as e:
            LOGGER.error("v3 training failed: " + str(e))
    threading.Thread(target=_train, daemon=True).start()
    return {"ok": True, "message": "Training started in background. Check /api/v3/status in 3-5 minutes."}

@APP.post("/api/v3/backtest")
def v3_backtest(x_cron_secret: str = Header(default=""), symbol: str = "LTC", asset_type: str = "crypto"):
    """
    Historical samples for v3 training: TP/SL WIN before stop within N daily bars
    (same rules as live reconcile / core.vol_targets).
    """
    import os
    if x_cron_secret != os.getenv("CRON_SECRET",""):
        return JSONResponse({"ok":False,"error":"Forbidden"}, status_code=403)
    try:
        from core.signal_engine import backtest_symbol, V3_LABEL_HOLD_BARS, LABEL_TYPE
        from core.vol_targets import base_vol_pct
        rows = backtest_symbol(symbol, asset_type)
        if not rows:
            return {"ok": False, "error": "No data for " + symbol}
        total = len(rows)
        hits = sum(1 for r in rows if r['label'] == 1)
        expired = sum(1 for r in rows if r.get('outcome') == 'EXPIRED')
        losses = sum(1 for r in rows if r.get('outcome') == 'LOSS')
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
            "vol_target_frac": vol_pct,
            "label_lookahead_daily_bars": V3_LABEL_HOLD_BARS,
            "indicators": results,
        }
    except Exception as e:
        return JSONResponse({"ok":False,"error":str(e)}, status_code=500)
