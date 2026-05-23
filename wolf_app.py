import os, sys, time, logging, threading, hmac, secrets as _secrets
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from core.db import db_conn, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stdout,
)
LOGGER = logging.getLogger("ghost")

# PR #15 cache-bust banner. Logged once at module import. If this line is
# missing from Railway logs after a deploy, the container is stale (the
# Procfile boot echo is the shell-level twin of this check).
LOGGER.info(
    "[wolf_app] BOOT_BANNER PR34_CACHEBUST "
    "DEPLOY_VERSION=%s GIT_SHA=%s DEPLOY_ID=%s",
    os.getenv("DEPLOY_VERSION", "unset"),
    os.getenv("RAILWAY_GIT_COMMIT_SHA", "unset"),
    os.getenv("RAILWAY_DEPLOYMENT_ID", "unset"),
)

CRON_SECRET = os.getenv("CRON_SECRET", "")


def _cron_ok(provided: str, strict: bool = False) -> bool:
    """Constant-time check for x-cron-secret header.

    strict=False (default): if no CRON_SECRET is configured, allow (dev mode).
    strict=True: if no CRON_SECRET is configured, REJECT. Use on endpoints
                 that must never be exposed without explicit auth, even in dev.
    """
    secret = os.environ.get("CRON_SECRET", "")
    if not secret:
        return not strict
    return hmac.compare_digest((provided or "").encode("utf-8"),
                               secret.encode("utf-8"))

_COVERAGE_RETRAIN_RUNNING = False
_RETRAIN_JOB_LOCK = threading.Lock()
_APP_BOOT_TS = time.time()

_COCKPIT_DB_CACHE = {"t": 0.0, "stats": None, "direction": None, "v3": None, "activity": None}


def _bump_cockpit_db_cache():
    _COCKPIT_DB_CACHE["t"] = 0.0
    for _k in ("stats", "direction", "v3", "activity"):
        _COCKPIT_DB_CACHE[_k] = None


def _v32_stats_start_ts(cur):
    """Unix start of v3.2 stats window with non-drifting persistence.

    Priority:
    1) V3_STATS_START_TS env override (if set)
    2) persisted ghost_state.v32_stats_start_ts (sticky, never move forward)
    3) bootstrap candidate from model metas + recent symbol history
    """
    import json as _json

    # 1) Hard override from env
    try:
        _env_ts = int(os.getenv("V3_STATS_START_TS", "0") or 0)
        if _env_ts > 0:
            return _env_ts
    except Exception:
        pass

    # Ensure state table exists (shared with other lightweight state keys)
    try:
        cur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
    except Exception:
        pass

    # Correct bad persisted cutover (Apr 8 = 1775606400 -> Apr 5 = 1775347200)
    CORRECT_V32_TS = 1775347200  # 2026-04-05 00:00 UTC — v3.2 deploy date
    try:
        cur.execute("SELECT val FROM ghost_state WHERE key='v32_stats_start_ts'")
        _row = cur.fetchone()
        if _row and int(_row[0]) >= 1775606400:
            cur.execute("UPDATE ghost_state SET val=%s WHERE key='v32_stats_start_ts'", (str(CORRECT_V32_TS),))
            LOGGER.info("v32_stats_start_ts corrected to Apr 5 2026")
    except Exception: pass

    # 2) Existing sticky cutover if present
    sticky_ts = 0
    try:
        cur.execute("SELECT val FROM ghost_state WHERE key='v32_stats_start_ts'")
        _row = cur.fetchone()
        if _row and _row[0]:
            sticky_ts = int(_row[0])
    except Exception:
        sticky_ts = 0

    # 3) Bootstrap candidate (if sticky missing or to allow safe backward correction only)
    model_ts = 0
    model_syms = []
    try:
        cur.execute("SELECT key, value FROM ghost_v3_model WHERE key LIKE 'meta_%'")
        trained = []
        for key, val in cur.fetchall():
            try:
                m = _json.loads(val)
                if m.get("label_type") != "tp_sl_daily":
                    continue
                ts = int(m.get("trained_at", 0) or 0)
                if ts > 0:
                    trained.append(ts)
                sym = str(key or "").replace("meta_", "").strip().upper()
                if sym:
                    model_syms.append(sym)
            except Exception:
                continue
        if trained:
            model_ts = min(trained)
    except Exception:
        model_ts = 0

    # Recent symbol-history anchor (helps recover when model_ts drifts forward after retrain churn)
    # Scoped to recent history to avoid pulling legacy-era rows.
    hist_ts = 0
    try:
        model_syms = sorted(set(model_syms))
        if model_syms:
            placeholders = ",".join(["%s"] * len(model_syms))
            cur.execute(
                f"SELECT MIN(predicted_at) FROM predictions "
                f"WHERE predicted_at IS NOT NULL AND predicted_at >= %s "
                f"AND symbol IN ({placeholders})",
                [int(time.time()) - 90 * 86400, *model_syms],
            )
            _h = cur.fetchone()
            if _h and _h[0]:
                hist_ts = int(_h[0])
    except Exception:
        hist_ts = 0

    candidates = [t for t in (model_ts, hist_ts) if t > 0]
    candidate_ts = min(candidates) if candidates else 0

    # Never move cutover forward implicitly; allow only first set or backward correction.
    if sticky_ts > 0 and candidate_ts > 0:
        final_ts = min(sticky_ts, candidate_ts)
    else:
        final_ts = sticky_ts or candidate_ts or 0

    if final_ts > 0 and final_ts != sticky_ts:
        try:
            cur.execute(
                "INSERT INTO ghost_state(key,val) VALUES('v32_stats_start_ts',%s) "
                "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                (str(final_ts),),
            )
        except Exception:
            pass
    return final_ts


def _compute_get_stats(cur):
    """Payload for GET /api/stats using an existing cursor."""
    cur.execute(
        "SELECT outcome, COUNT(*) FROM predictions WHERE outcome IN ('WIN','LOSS') "
        "AND predicted_at IS NOT NULL GROUP BY outcome"
    )
    rows = {r[0]: r[1] for r in cur.fetchall()}
    wins = rows.get("WIN", 0)
    losses = rows.get("LOSS", 0)
    total = wins + losses
    cur.execute("SELECT COUNT(*) FROM predictions WHERE outcome IS NULL AND entry_price IS NOT NULL")
    open_count = cur.fetchone()[0]
    v32_start_ts = _v32_stats_start_ts(cur)
    v32_wins = v32_losses = v32_total = 0
    v32r_wins = v32r_losses = v32r_total = 0
    if v32_start_ts > 0:
        cur.execute(
            "SELECT outcome, COUNT(*) FROM predictions "
            "WHERE outcome IN ('WIN','LOSS') AND predicted_at IS NOT NULL AND predicted_at >= %s "
            "GROUP BY outcome",
            (v32_start_ts,),
        )
        v32_rows = {r[0]: r[1] for r in cur.fetchall()}
        v32_wins = v32_rows.get("WIN", 0)
        v32_losses = v32_rows.get("LOSS", 0)
        v32_total = v32_wins + v32_losses
        # Closes after cutover (matches "Recent Results" feel; can include picks issued before cutover)
        cur.execute(
            "SELECT outcome, COUNT(*) FROM predictions "
            "WHERE outcome IN ('WIN','LOSS') AND resolved_at IS NOT NULL AND resolved_at >= %s "
            "GROUP BY outcome",
            (v32_start_ts,),
        )
        v32r_rows = {r[0]: r[1] for r in cur.fetchall()}
        v32r_wins = v32r_rows.get("WIN", 0)
        v32r_losses = v32r_rows.get("LOSS", 0)
        v32r_total = v32r_wins + v32r_losses
    scan_stocks = [s.strip().upper() for s in os.getenv("STOCK_SYMBOLS", "WOLF").split(",") if s.strip()] or ["WOLF"]
    # WOLF-only hardening (PR #8): refuse to surface non-WOLF symbols in the
    # scan list even if the Railway env var is stale. WOLF is always included.
    scan_stocks = [s for s in scan_stocks if s == "WOLF"] or ["WOLF"]
    return {
        "ok": True,
        "wins": wins,
        "losses": losses,
        "total": total,
        "win_rate_pct": round(wins / total * 100, 1) if total else 0,
        "open_positions": open_count,
        "post_v32": {
            "start_ts": v32_start_ts,
            "wins": v32_wins,
            "losses": v32_losses,
            "total": v32_total,
            "win_rate_pct": round(v32_wins / v32_total * 100, 1) if v32_total else 0.0,
        },
        "post_v32_resolved": {
            "start_ts": v32_start_ts,
            "wins": v32r_wins,
            "losses": v32r_losses,
            "total": v32r_total,
            "win_rate_pct": round(v32r_wins / v32r_total * 100, 1) if v32r_total else 0.0,
        },
        "scan_symbols": {"stocks": scan_stocks},
    }


def _cockpit_activity_on_cursor(cur):
    """Summary counts embedded in /api/cockpit/context."""
    cur.execute(
        "SELECT COUNT(*) FROM predictions WHERE outcome IS NULL AND expires_at > extract(epoch from now())"
    )
    open_predictions = cur.fetchone()[0]
    cur.execute(
        "SELECT COUNT(*) FROM predictions WHERE resolved_at > extract(epoch from now()) - 86400"
    )
    resolved_24h = cur.fetchone()[0]
    cur.execute(
        "SELECT outcome, COUNT(*) FROM predictions WHERE resolved_at > extract(epoch from now()) - 604800 GROUP BY outcome"
    )
    weekly_outcomes = {r[0]: r[1] for r in cur.fetchall()}
    return {
        "open_predictions": open_predictions,
        "resolved_24h": resolved_24h,
        "weekly_outcomes": weekly_outcomes,
    }


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
    _cycle_diag = {}
    # Dedup: Telegram morning card only once per CT day — but always run prediction cycle
    # (redeploys used to return [] here and skipped inserts until next calendar day).
    _ct_tz = _pytz.timezone("America/Chicago")
    _today_ct = _dt.datetime.now(_ct_tz).strftime("%Y-%m-%d")
    _skip_telegram = False
    try:
        with db_conn() as _dc2:
            _cur_d = _dc2.cursor()
            _cur_d.execute("SELECT val FROM ghost_state WHERE key='last_morning_card_date'")
            _row = _cur_d.fetchone()
            if _row and _row[0] == _today_ct:
                _skip_telegram = True
                LOGGER.info(
                    "Morning card already sent today (" + _today_ct + ") — will run prediction cycle, skip duplicate Telegram"
                )
    except Exception as _de:
        LOGGER.warning("Dedup check failed: "+str(_de)[:60])
    picks, _cycle_diag = run_prediction_cycle(with_diag=True)
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
    if _skip_telegram:
        LOGGER.info("Morning card: Telegram skipped (same CT day); cycle returned %s saved picks", len(picks or []))
        return picks
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
                        _cd = _cycle_diag if isinstance(_cycle_diag, dict) else {}
                        _top = (_cd.get("top_reason_label") or "unknown").strip()
                        _sum = (_cd.get("skip_summary") or "").strip()
                        _reg = (_cd.get("regime") or "").strip()
                        _cf = _cd.get("confidence_floor")
                        _sc = _cd.get("symbols_scanned")
                        _cand = _cd.get("candidates")
                        _saved = _cd.get("saved")
                        _dd = _cd.get("dedup_blocked")
                        _detail = (
                            f"scanned={_sc}, candidates={_cand}, saved={_saved}, dedup={_dd}. "
                            f"Top: {_top}"
                            + (f". Counts: {_sum}" if _sum else "")
                            + (f". Regime: {_reg}" if _reg else "")
                            + (
                                f". Conf floor {float(_cf)*100:.1f}%"
                                if isinstance(_cf, (int, float))
                                else ""
                            )
                        )
                        _send(
                            "Ghost Protocol v2 — No new picks inserted today.\n"
                            "Reason: " + _detail
                        )
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


def _build_train_symbol_list():
    """Training symbol universe = WOLF + portfolio holdings (WOLF-only mode)."""
    from core.prediction import STOCK_SYMBOLS
    syms = [(s.strip().upper(), "stock") for s in STOCK_SYMBOLS if s.strip()]
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT symbol FROM user_portfolio")
            for (sym,) in cur.fetchall():
                k = (str(sym or "").strip().upper(), "stock")
                if k[0] and k not in syms:
                    syms.append(k)
    except Exception as e:
        LOGGER.warning("Coverage symbol build failed: %s", str(e)[:80])
    return syms


def _coverage_maintenance_job():
    """
    Keep model coverage above a floor.
    If loaded model count is too low, run a rate-limited retrain pass.
    """
    global _COVERAGE_RETRAIN_RUNNING
    if os.getenv("AUTO_COVERAGE_RETRAIN_ENABLED", "1").strip() not in ("1", "true", "TRUE", "yes", "on"):
        return
    if _COVERAGE_RETRAIN_RUNNING:
        LOGGER.info("Coverage maintenance: retrain already running, skip")
        return

    min_models = max(1, int(os.getenv("MODEL_COVERAGE_MIN_MODELS", "3")))
    cooldown_s = max(900, int(os.getenv("COVERAGE_RETRAIN_COOLDOWN_SEC", "21600")))
    boot_grace_s = max(0, int(os.getenv("COVERAGE_BOOT_GRACE_SEC", "600")))
    low_yield_ratio = max(0.0, min(1.0, float(os.getenv("COVERAGE_LOW_YIELD_RATIO", "0.25"))))
    low_yield_backoff_s = max(3600, int(os.getenv("COVERAGE_LOW_YIELD_BACKOFF_SEC", "43200")))
    now = int(time.time())
    _lock_acquired = False
    if (time.time() - _APP_BOOT_TS) < boot_grace_s:
        LOGGER.info("Coverage maintenance: boot grace active, defer (%ss)", int(boot_grace_s - (time.time() - _APP_BOOT_TS)))
        return

    last_ts = 0
    low_yield_until_ts = 0
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
            cur.execute("SELECT val FROM ghost_state WHERE key='last_coverage_retrain_ts'")
            row = cur.fetchone()
            last_ts = int(row[0]) if row and row[0] else 0
            cur.execute("SELECT val FROM ghost_state WHERE key='last_coverage_low_yield_until_ts'")
            row2 = cur.fetchone()
            low_yield_until_ts = int(row2[0]) if row2 and row2[0] else 0
    except Exception as e:
        LOGGER.warning("Coverage maintenance state read failed: %s", str(e)[:80])

    if low_yield_until_ts and now < low_yield_until_ts:
        LOGGER.info("Coverage maintenance: low-yield backoff active (%ss left)", low_yield_until_ts - now)
        return

    if last_ts and now - last_ts < cooldown_s:
        LOGGER.info("Coverage maintenance: cooldown active (%ss left)", cooldown_s - (now - last_ts))
        return

    try:
        from core.signal_engine import get_model_status, train_and_validate
        st = get_model_status() or {}
        loaded = int(st.get("models", 0)) if st.get("trained") else 0
        if loaded >= min_models:
            LOGGER.info("Coverage maintenance: loaded models %s >= floor %s", loaded, min_models)
            return

        syms = _build_train_symbol_list()
        if not syms:
            LOGGER.warning("Coverage maintenance: empty symbol universe, skip retrain")
            return

        if not _RETRAIN_JOB_LOCK.acquire(blocking=False):
            LOGGER.info("Coverage maintenance: retrain lock busy, skip this run")
            return
        _lock_acquired = True
        _COVERAGE_RETRAIN_RUNNING = True
        LOGGER.warning(
            "Coverage maintenance: loaded models %s below floor %s, retraining %s symbols",
            loaded, min_models, len(syms)
        )
        _, acc_ratio, _ok = train_and_validate(syms)
        trained = int(round(acc_ratio * len(syms))) if syms else 0
        failed = len(syms) - trained
        try:
            purged = _auto_purge_bad_models()
            pv = _purge_v3_stale_or_weak()
            LOGGER.info("Coverage maintenance purge: legacy=%s v3=%s", purged, pv)
        except Exception as e:
            LOGGER.warning("Coverage maintenance purge failed: %s", str(e)[:80])
        _bump_cockpit_db_cache()
        LOGGER.info(
            "Coverage maintenance retrain complete: %s trained, %s failed (acc_ratio=%.3f)",
            trained, failed, float(acc_ratio or 0.0)
        )
        if float(acc_ratio or 0.0) < low_yield_ratio:
            try:
                with db_conn() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "INSERT INTO ghost_state(key,val) VALUES('last_coverage_low_yield_until_ts',%s) "
                        "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                        (str(int(time.time()) + low_yield_backoff_s),),
                    )
                LOGGER.warning(
                    "Coverage maintenance: low-yield retrain (acc_ratio=%.3f < %.3f), backoff %ss",
                    float(acc_ratio or 0.0), low_yield_ratio, low_yield_backoff_s
                )
            except Exception as e:
                LOGGER.warning("Coverage maintenance low-yield backoff write failed: %s", str(e)[:80])
    except Exception as e:
        LOGGER.warning("Coverage maintenance retrain failed: %s", str(e)[:120])
    finally:
        if _lock_acquired:
            try:
                _RETRAIN_JOB_LOCK.release()
            except Exception:
                pass
        _COVERAGE_RETRAIN_RUNNING = False
        if _lock_acquired:
            try:
                with db_conn() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "INSERT INTO ghost_state(key,val) VALUES('last_coverage_retrain_ts',%s) "
                        "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                        (str(int(time.time())),),
                    )
            except Exception:
                pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    LOGGER.info("Ghost Protocol v2 starting...")
    if os.getenv("GHOST_TEST_MODE", "0").strip().lower() in ("1", "true", "yes", "on"):
        LOGGER.info("Ghost Protocol v2 test mode startup: skipping DB init and schedulers")
        yield
        return
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

    # PR #26: auto-purge ghost/test rows from user_portfolio on every boot.
    # The /admin "Purge Ghost Portfolio" button (PR #23) was never run —
    # the ZZE2E* probe-ticker rows persisted. Self-healing: deletes rows
    # matching the ghost patterns on each startup so they can't pollute
    # the investor portfolio totals. Legit WOLF (and any deliberately-added
    # non-ghost symbol) is untouched.
    try:
        with db_conn() as _pc:
            _pcur = _pc.cursor()
            _pcur.execute("SELECT id, symbol FROM user_portfolio")
            _prows = _pcur.fetchall()
            _purged_ids = []
            for _rid, _sym in _prows:
                _up = str(_sym or "").strip().upper()
                if any(_up.startswith(p) or _up == p for p in _GHOST_PORTFOLIO_PATTERNS):
                    _pcur.execute("DELETE FROM user_portfolio WHERE id=%s", (int(_rid),))
                    _purged_ids.append(_rid)
            if _purged_ids:
                LOGGER.info("Boot portfolio purge: removed %s ghost rows %s",
                            len(_purged_ids), _purged_ids[:10])
    except Exception as _ppe:
        LOGGER.warning("Boot portfolio purge failed: " + str(_ppe)[:80])

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
    # Coverage maintenance: if too few loadable v3 models, run rate-limited retrain.
    scheduler.register(
        "coverage_maintenance",
        _coverage_maintenance_job,
        interval_s=max(900, int(os.getenv("COVERAGE_CHECK_INTERVAL_SEC", "3600"))),
    )
    # Weekly model retrain — keeps models fresh as market conditions change
    from core.signal_engine import train_and_validate as _tv
    def _weekly_retrain():
        _lock_acquired = False
        try:
            if not _RETRAIN_JOB_LOCK.acquire(blocking=False):
                LOGGER.info("Weekly retrain skipped: retrain lock busy")
                return
            _lock_acquired = True
            min_interval_s = max(3600, int(os.getenv("WEEKLY_RETRAIN_MIN_INTERVAL_SEC", "604800")))
            now_ts = int(time.time())
            last_ts = 0
            try:
                with db_conn() as _wc:
                    _wcur = _wc.cursor()
                    _wcur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
                    _wcur.execute("SELECT val FROM ghost_state WHERE key='last_weekly_retrain_ts'")
                    _wr = _wcur.fetchone()
                    last_ts = int(_wr[0]) if _wr and _wr[0] else 0
            except Exception as _wse:
                LOGGER.warning("Weekly retrain state read failed: %s", str(_wse)[:80])
            if last_ts and (now_ts - last_ts) < min_interval_s:
                LOGGER.info(
                    "Weekly retrain skipped: last run %ss ago (<%ss)",
                    now_ts - last_ts, min_interval_s
                )
                return
            try:
                with db_conn() as _wc2:
                    _wcur2 = _wc2.cursor()
                    _wcur2.execute(
                        "INSERT INTO ghost_state(key,val) VALUES('last_weekly_retrain_ts',%s) "
                        "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                        (str(now_ts),),
                    )
            except Exception as _wse2:
                LOGGER.warning("Weekly retrain state write failed: %s", str(_wse2)[:80])
            from core.prediction import STOCK_SYMBOLS
            syms = [(s.strip(), "stock") for s in STOCK_SYMBOLS if s.strip()]
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
        finally:
            if _lock_acquired:
                try:
                    _RETRAIN_JOB_LOCK.release()
                except Exception:
                    pass
    scheduler.register("weekly_retrain", _weekly_retrain, interval_s=604800)
    scheduler.start()
    # Ghost v3: auto-train on startup if no model in DB
    def _startup_train():
        _lock_acquired = False
        try:
            from core.signal_engine import train_and_validate
            import os
            if not _has_any_v3_model():
                if not _RETRAIN_JOB_LOCK.acquire(blocking=False):
                    LOGGER.info("Startup training skipped: retrain lock busy")
                    return
                _lock_acquired = True
                LOGGER.info("No v3.2 TP/SL model found — training on startup...")
                stocks = [(s.strip(),"stock") for s in os.getenv("STOCK_SYMBOLS","WOLF").split(",") if s.strip()] or [("WOLF","stock")]
                try:
                    from core.db import db_conn as _dbc
                    with _dbc() as _c:
                        _curp = _c.cursor()
                        _curp.execute("SELECT DISTINCT symbol FROM user_portfolio")
                        for (sym,) in _curp.fetchall():
                            _entry = (str(sym or "").strip().upper(), "stock")
                            if _entry[0] and _entry not in stocks:
                                stocks.append(_entry)
                except Exception:
                    pass
                m, acc, passed = train_and_validate(stocks)
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
        finally:
            if _lock_acquired:
                try:
                    _RETRAIN_JOB_LOCK.release()
                except Exception:
                    pass
    import threading as _th
    _th.Thread(target=_startup_train, daemon=True).start()
    LOGGER.info("Ghost Protocol v2 ready.")
    yield
    scheduler.stop()

APP = FastAPI(title="Ghost Protocol v2", version="2.0.0", lifespan=lifespan)
APP.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Mount portfolio router — WOLF position tracking, price refresh, ghost predictions
from core.portfolio_routes import portfolio_router
from core.stats_direction import compute_stats_by_direction
APP.include_router(portfolio_router)

# Phase 4: WOLF Intel endpoints
try:
    from api.wolf_endpoints import router as wolf_router
    APP.include_router(wolf_router)
    LOGGER.info("[INIT] WOLF Intel endpoints loaded")
except Exception as _we:
    LOGGER.warning(f"[INIT] wolf_endpoints unavailable: {_we}")



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
            # Legacy table may not exist on newer deployments; skip quietly if absent.
            cur.execute("SELECT to_regclass('public.ghost_models')")
            reg = cur.fetchone()
            if not reg or not reg[0]:
                return 0
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
async def delete_model(x_cron_secret: str = Header(None), non_wolf_only: bool = False):
    """Delete v3 models from ghost_v3_model.

    Default mode: delete models with accuracy < V3_MIN_HOLDOUT_ACC (cleanup
    of weak models below the deploy gate).

    non_wolf_only=true mode: delete every model whose symbol is not WOLF,
    regardless of accuracy. Use to clean up stale rows from the pre-WOLF
    crypto / multi-stock era that v3_status already filters out at read
    time (per PR #7 WOLF-only hardening) but still occupy DB rows.
    """
    if not _cron_ok(x_cron_secret, strict=True):
        raise HTTPException(status_code=403, detail="Forbidden")
    import json as _j
    from core.db import db_conn
    deleted = []
    kept = []
    ACCURACY_FLOOR = float(os.getenv("V3_MIN_HOLDOUT_ACC", "0.55"))
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM ghost_v3_model WHERE key LIKE 'meta_%'")
            rows = cur.fetchall()
            for key, val in rows:
                sym = key.replace("meta_", "")
                if non_wolf_only:
                    if str(sym).upper() == "WOLF":
                        kept.append(f"{sym}(WOLF)")
                        continue
                    cur.execute("DELETE FROM ghost_v3_model WHERE key IN (%s, %s)",
                               (f"model_{sym}", f"meta_{sym}"))
                    deleted.append(f"{sym}(non-WOLF)")
                    continue
                try:
                    meta = _j.loads(val)
                    acc = meta.get("accuracy", 0)
                    if acc < ACCURACY_FLOOR:
                        cur.execute("DELETE FROM ghost_v3_model WHERE key IN (%s, %s)",
                                   (f"model_{sym}", f"meta_{sym}"))
                        deleted.append(f"{sym}(acc={round(acc*100,1)}%)")
                    else:
                        kept.append(f"{sym}(acc={round(acc*100,1)}%)")
                except Exception:
                    pass
        return {"ok": True, "mode": "non_wolf_only" if non_wolf_only else "low_accuracy",
                "deleted": deleted, "kept": kept}
    except Exception as e:
        return {"ok": False, "error": str(e)}



_GHOST_PORTFOLIO_PATTERNS = ("ZZE2E", "STOCK GHOST", "GHOST", "ZZ", "TEST")


@APP.post("/api/admin/purge-ghost-portfolio")
async def purge_ghost_portfolio(x_cron_secret: str = Header(None), dry_run: bool = False):
    """Hard-delete ghost / test rows from user_portfolio.

    Targets symbols matching one of _GHOST_PORTFOLIO_PATTERNS (case-
    insensitive prefix or exact match). Common pollutants:
      - 'ZZE2E*' — yfinance probe tickers (PR #13/14 left visible by mistake)
      - 'STOCK GHOST', 'GHOST*' — test rows
      - 'ZZ*', 'TEST*' — manual test entries

    dry_run=true: report what would be deleted without deleting.
    """
    if not _cron_ok(x_cron_secret, strict=True):
        raise HTTPException(status_code=403, detail="Forbidden")
    deleted = []
    would_delete = []
    kept_count = 0
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, symbol FROM user_portfolio")
            rows = cur.fetchall()
            for rid, sym in rows:
                up = (str(sym or "").strip().upper())
                hit = any(up.startswith(p) or up == p for p in _GHOST_PORTFOLIO_PATTERNS)
                if not hit:
                    kept_count += 1
                    continue
                if dry_run:
                    would_delete.append({"id": int(rid), "symbol": sym})
                else:
                    cur.execute("DELETE FROM user_portfolio WHERE id=%s", (int(rid),))
                    deleted.append({"id": int(rid), "symbol": sym})
        return {
            "ok": True,
            "dry_run": dry_run,
            "patterns": list(_GHOST_PORTFOLIO_PATTERNS),
            "deleted": deleted,
            "would_delete": would_delete,
            "kept": kept_count,
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@APP.post("/api/admin/fix-stock-expiry")
async def fix_stock_expiry(x_cron_secret: str = Header(None)):
    """Fix stock picks that were created before the weekend-expiry fix and expire before market open."""
    if not _cron_ok(x_cron_secret, strict=True):
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


@APP.post("/api/dedup-picks", include_in_schema=False)
def dedup_picks(x_cron_secret: str = Header(None)):
    """Expire duplicate open picks per symbol (keep highest confidence). Requires CRON_SECRET header."""
    if not _cron_ok(x_cron_secret, strict=True):
        raise HTTPException(status_code=403, detail="Forbidden")
    now = int(time.time())
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, symbol, confidence FROM predictions WHERE outcome IS NULL AND expires_at > %s "
                "ORDER BY symbol, confidence DESC",
                (now,),
            )
            rows = cur.fetchall()
            seen = {}
            to_expire = []
            for pid, sym, conf in rows:
                if sym not in seen:
                    seen[sym] = pid
                else:
                    to_expire.append(pid)
            if to_expire:
                cur.execute(
                    "UPDATE predictions SET outcome='EXPIRED', resolved_at=%s WHERE id = ANY(%s)",
                    (now, to_expire),
                )
        return {"ok": True, "expired": len(to_expire), "kept": len(seen)}
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
    feeds = {"alpaca_stock": False, "yfinance": False, "summary": "0/2 feeds responding"}
    try:
        feeds = check_feeds()
        feeds_ok = sum(1 for k,v in feeds.items() if k != "summary" and v)
        if feeds_ok < 2:
            warnings.append(feeds.get("summary", "<2 feeds responding"))
    except Exception as _fe:
        LOGGER.warning("health.check_feeds failed: " + str(_fe)[:120])

    # 3. Prediction freshness vs cycle freshness
    freshness_min = None
    cycle_freshness_min = None
    cycle_last_saved = None
    cycle_last_scanned = None
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT predicted_at FROM predictions WHERE predicted_at IS NOT NULL ORDER BY predicted_at DESC LIMIT 1")
            row = cur.fetchone()
            if row and row[0]:
                freshness_min = int((_t.time() - float(row[0])) / 60)
            cur.execute("SELECT val FROM ghost_state WHERE key='last_prediction_cycle_ts'")
            cyc = cur.fetchone()
            if cyc and cyc[0]:
                cycle_freshness_min = int((_t.time() - float(cyc[0])) / 60)
            cur.execute("SELECT val FROM ghost_state WHERE key='last_prediction_cycle_saved'")
            cyc_saved = cur.fetchone()
            if cyc_saved and cyc_saved[0] is not None:
                cycle_last_saved = int(cyc_saved[0])
            cur.execute("SELECT val FROM ghost_state WHERE key='last_prediction_cycle_scanned'")
            cyc_scan = cur.fetchone()
            if cyc_scan and cyc_scan[0] is not None:
                cycle_last_scanned = int(cyc_scan[0])

        cycle_stale_min = max(60, int(os.getenv("PREDICTION_CYCLE_STALE_MIN", "2160")))  # default 36h
        if cycle_freshness_min is None:
            warnings.append("Prediction cycle heartbeat missing")
        elif cycle_freshness_min > cycle_stale_min:
            issues.append("Prediction cycle stale: " + str(cycle_freshness_min) + "m")

        # No-pick periods are normal when gates block trades; do not hard-fail if cycle is alive.
        if freshness_min and freshness_min > 2880:
            if cycle_freshness_min is not None and cycle_freshness_min <= cycle_stale_min:
                warnings.append("No picks inserted recently: " + str(freshness_min) + "m (cycle alive)")
            else:
                issues.append("Predictions stale: " + str(freshness_min) + "m")
    except Exception as _pe:
        LOGGER.warning("health.prediction_freshness_block failed: " + str(_pe)[:120])

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
        total_syms = len([s for s in os.getenv("STOCK_SYMBOLS","WOLF").split(",") if s.strip()]) or 1
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
            except Exception as _de:
                LOGGER.warning("health.dedup_expire_update failed: " + str(_de)[:120])
    except Exception as _oe:
        LOGGER.warning("health.open_picks_block failed: " + str(_oe)[:120])

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
    except Exception as _se:
        LOGGER.warning("health.scheduler_status failed: " + str(_se)[:120])

    score = max(0, min(100, 100 - len(issues)*20 - len(warnings)*5))
    status_str = "healthy" if score >= 80 and not issues else "degraded" if score >= 50 else "critical"
    return {
        "status": status_str, "score": score, "db": db_ok,
        "telegram_configured": tg_ok, "predictions_freshness_min": freshness_min,
        "prediction_cycle_freshness_min": cycle_freshness_min,
        "last_prediction_cycle_saved": cycle_last_saved,
        "last_prediction_cycle_scanned": cycle_last_scanned,
        "open_picks": open_picks, "dedup_blocked": dedup_blocked,
        "last_morning_card_min": last_card_min, "confidence_floor": conf_floor,
        "price_feeds": feeds, "tasks": tasks, "issues": issues, "warnings": warnings,
    }

@APP.get("/api/health")
def api_health():
    """Alias for health endpoint used by external monitors."""
    return health()


@APP.post("/api/health/audit")
def health_audit(x_cron_secret: str = Header(default=""), auto_fix: bool = True):
    """
    Deep reliability audit with persistent findings and optional auto-fix hooks.

    Returns structured PASS/FAIL records for each check:
    status, location, evidence, impact, auto_fix, fix_result.
    """
    if not _cron_ok(x_cron_secret):
        raise HTTPException(status_code=403)

    import asyncio as _asyncio
    from core.health_audit import run_health_audit

    stage = "init"
    try:
        stage = "health"
        h = health()

        stage = "diagnostics"
        d = {"score": 0, "checks_passed": 0, "warnings": 0, "errors": 1, "details": {"errors": [{"check": "diagnostics.fallback", "detail": "diagnostics fallback used"}]}}
        try:
            # Avoid creating an un-awaited coroutine if we are already inside a running loop.
            _loop_running = False
            try:
                _asyncio.get_running_loop()
                _loop_running = True
            except RuntimeError:
                _loop_running = False
            if _loop_running:
                d = {
                    "score": 0,
                    "checks_passed": 0,
                    "warnings": 0,
                    "errors": 1,
                    "details": {"errors": [{"check": "diagnostics.loop", "detail": "running loop detected; fallback diagnostics used"}]},
                }
            else:
                d = _asyncio.run(diagnostics())
        except Exception as _de:
            d = {
                "score": 0,
                "checks_passed": 0,
                "warnings": 0,
                "errors": 1,
                "details": {"errors": [{"check": "diagnostics.error", "detail": str(_de)[:160]}]},
            }

        stage = "stats"
        try:
            with db_conn() as conn:
                cur = conn.cursor()
                s = _compute_get_stats(cur)
        except Exception as _se:
            s = {
                "ok": False,
                "wins": 0,
                "losses": 0,
                "total": 0,
                "open_positions": 0,
                "error": "stats_unavailable: " + str(_se)[:120],
            }

        stage = "cockpit"
        try:
            c = cockpit_context()
            if isinstance(c, JSONResponse):
                c = {"ok": False, "error": "cockpit_context returned JSONResponse error"}
        except Exception as _ce:
            c = {"ok": False, "error": "cockpit_context_failed: " + str(_ce)[:120]}

        stage = "audit"
        report = run_health_audit(
            app=APP,
            db_conn=db_conn,
            health_payload=h,
            diagnostics_payload=d,
            stats_payload=s,
            cockpit_payload=c,
            auto_fix=bool(auto_fix),
        )
        return {"ok": True, "audit": report}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200], "stage": stage}, status_code=500)


@APP.get("/api/health/audit/history")
def health_audit_history(limit: int = 20):
    """Persistent audit run history for recurrence analysis."""
    lim = max(1, min(200, int(limit)))
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS health_audit_runs (
                    id SERIAL PRIMARY KEY,
                    run_ts BIGINT NOT NULL,
                    status TEXT NOT NULL,
                    coverage_pct FLOAT NOT NULL,
                    unresolved_count INT NOT NULL,
                    resolved_count INT NOT NULL,
                    payload JSONB NOT NULL
                )
                """
            )
            cur.execute(
                """
                SELECT id, run_ts, status, coverage_pct, unresolved_count, resolved_count
                FROM health_audit_runs
                ORDER BY id DESC
                LIMIT %s
                """,
                (lim,),
            )
            rows = cur.fetchall()
        out = [
            {
                "id": int(r[0]),
                "run_ts": int(r[1]),
                "status": r[2],
                "coverage_pct": float(r[3]),
                "unresolved_count": int(r[4]),
                "resolved_count": int(r[5]),
            }
            for r in rows
        ]
        return {"ok": True, "runs": out}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@APP.get("/api/regime", include_in_schema=False)
def api_regime():
    """WOLF-only mode: regime gate is a no-op. Endpoint retained for back-compat."""
    return {"ok": True, "block_crypto_buys": False, "reduce_size": False, "reason": "", "btc_24h_pct": 0.0}


@APP.get("/api/objective")
def api_objective():
    """Progress telemetry toward configured prediction win-rate objective."""
    try:
        from core.prediction import get_objective_status
        return {"ok": True, **get_objective_status()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


@APP.get("/api/objective/report")
def api_objective_report(days: int = 14):
    """Daily objective trend report for the last N days."""
    try:
        from core.prediction import get_objective_daily_report
        return {"ok": True, **get_objective_daily_report(days=days)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


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
        "asset_type": r.get("asset_type","stock"),
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
    if not _cron_ok(x_cron_secret):
        raise HTTPException(status_code=403)
    from core.prediction import run_prediction_cycle
    picks = run_prediction_cycle()
    return {"ok": True, "picks_generated": len(picks), "picks": picks}

@APP.post("/api/morning-card")
def trigger_morning_card(x_cron_secret: str = Header(default="")):
    """Run prediction cycle AND send Telegram card. Use for cron-job.org trigger."""
    if not _cron_ok(x_cron_secret):
        raise HTTPException(status_code=403)
    picks = _morning_card_job()
    return {"ok": True, "picks_generated": len(picks)}

@APP.post("/api/reconcile")
def trigger_reconcile(x_cron_secret: str = Header(default="")):
    if not _cron_ok(x_cron_secret):
        raise HTTPException(status_code=403)
    from core.prediction import reconcile_outcomes
    count = reconcile_outcomes()
    return {"ok": True, "resolved": count}

@APP.post("/api/wolf/signal-alert/check")
def wolf_signal_alert_check(x_cron_secret: str = Header(default="")):
    """Scan recent WOLF picks for unalerted high-confidence signals; fire Telegram.

    Throttling:
      - Confidence floor: 0.80 (only high-conviction signals alert)
      - Per-pick dedup: each prediction id only alerts once (wolf_signal_alerts table)
      - Daily cap: max 2 alerts per UTC day

    Designed to be called from a cron after /api/run-predictions or
    /api/morning-card. Safe to call repeatedly — dedup prevents duplicates.
    """
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
                    _send(body)
                except Exception as _se:
                    errors.append(f"id={pid} telegram: {str(_se)[:80]}")
                    continue
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


@APP.post("/api/cron/signal-check")
def cron_signal_check(x_cron_secret: str = Header(default="")):
    """Cron-triggered Telegram signal-alert sweep.

    Thin wrapper around wolf_signal_alert_check that also records the
    cron invocation in ghost_state for ops visibility. Wire this to your
    Railway cron schedule (cron-job.org / Railway scheduled jobs) alongside
    the existing prediction cycle — typical cadence: every 5-15 minutes
    during market hours. Throttling and dedup live inside the underlying
    check, so calling more frequently than needed is safe.
    """
    if not _cron_ok(x_cron_secret):
        raise HTTPException(status_code=403)
    ran_at = int(time.time())
    alert_result = wolf_signal_alert_check(x_cron_secret=x_cron_secret)
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
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


@APP.get("/api/diag/data-sources")
def diag_data_sources(x_cron_secret: str = Header(default=""), symbol: str = "WOLF", period: str = "1y"):
    """Probe each OHLCV data source independently and report results.

    Lets you see in-browser exactly which sources return bars and which
    fail, without grep'ing training logs. Mirrors the chain order in
    core/signal_engine._fetch_ohlcv (Alpaca SIP → IEX → Polygon → yfinance
    → Stooq).

    Each entry includes: ok, bar count, first/last timestamp on success,
    error string on failure, and request latency in ms.
    """
    if not _cron_ok(x_cron_secret):
        raise HTTPException(status_code=403)

    try:
        from core.signal_engine import (
            _try_polygon_ohlcv,
            _try_yfinance_ohlcv,
            _try_stooq_ohlcv,
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": "import failed: " + str(e)[:200]}, status_code=500)

    results = []

    def _probe(name, fn):
        t0 = time.time()
        try:
            rows = fn()
            elapsed_ms = int((time.time() - t0) * 1000)
            if rows:
                results.append({
                    "source": name,
                    "ok": True,
                    "bars": len(rows),
                    "first_ts": rows[0].get("ts"),
                    "last_ts": rows[-1].get("ts"),
                    "elapsed_ms": elapsed_ms,
                })
            else:
                results.append({
                    "source": name,
                    "ok": False,
                    "bars": 0,
                    "error": "returned no data (see Railway logs for per-branch detail)",
                    "elapsed_ms": elapsed_ms,
                })
        except Exception as exc:
            elapsed_ms = int((time.time() - t0) * 1000)
            results.append({
                "source": name,
                "ok": False,
                "bars": 0,
                "error": str(exc)[:300],
                "elapsed_ms": elapsed_ms,
            })

    _probe("polygon", lambda: _try_polygon_ohlcv(symbol, period))
    _probe("yfinance", lambda: _try_yfinance_ohlcv(symbol, period))
    _probe("stooq", lambda: _try_stooq_ohlcv(symbol, period))

    working = [r["source"] for r in results if r["ok"]]
    broken = [r["source"] for r in results if not r["ok"]]
    return {
        "ok": True,
        "symbol": symbol,
        "period": period,
        "results": results,
        "summary": {"working": working, "broken": broken, "total_working": len(working)},
        "note": "Alpaca SIP/IEX are nested inside _fetch_ohlcv and not directly probed; check Railway logs for those.",
    }


@APP.get("/api/telegram/status")
def telegram_status():
    """Telegram delivery visibility for the cockpit.

    Public read (matches /api/v3/status convention). Returns:
      - configured: whether Telegram env vars are set
      - last_cron_ts / last_cron_sent: from ghost_state (PR #8 signal-alert)
      - recent_alerts: last 5 rows from wolf_signal_alerts table
    """
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
            cur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
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


@APP.get("/api/wolf/gate-status")
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
            lp["regime"] = _scores.get("regime")
            lp["binding_confidence_threshold"] = round(binding_conf, 3)
            lp["bootstrap_min_conf"] = round(boot_conf, 3)
            # up_prob needed to clear the binding threshold, inverting
            # conf = clamp(accuracy + (up_prob - min_p) * 4, 0.75, 0.95):
            if acc is not None and min_p is not None:
                needed = min_p + (binding_conf - acc) / 4.0
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


@APP.get("/api/wolf/gate-history")
def wolf_gate_history(limit: int = 50):
    """Rolling per-cycle gate-outcome history (PR #29).

    Each prediction cycle records {ts, scanned, candidates, saved,
    dedup_blocked, would_fire, top_skip, skip_counts} to
    ghost_state.gate_outcome_history (last 50 cycles). This lets the
    operator review whether any recent cycle cleared the gates — and
    which gate was binding when none did — without watching the live
    monitor. Newest first. Read-only.
    """
    try:
        import json as _j
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
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
        for h in recent:
            ts_skip = h.get("top_skip")
            if ts_skip:
                binding[ts_skip] = binding.get(ts_skip, 0) + 1
        return {
            "ok": True,
            "count": len(recent),
            "fired_count": fired,
            "binding_gates": binding,
            "history": recent,
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


# v3.2 era marker — predictions with id >= this are Ghost's high-conviction
# v3.2-engine picks. Used across the codebase (core.stats_direction, core.prediction)
# to exclude ~223k legacy v1 rows from credibility stats.
_V32_ERA_MIN_ID = 223438


def _coerce_json(v):
    """psycopg2 may hand back JSONB as dict already, or as text. Normalise to obj."""
    if v is None:
        return {}
    if isinstance(v, (dict, list)):
        return v
    try:
        import json as _j
        return _j.loads(v)
    except Exception:
        return {}


@APP.get("/api/wolf/pick-journal")
def wolf_pick_journal(limit: int = 50, offset: int = 0, symbol: str = "WOLF"):
    """Pick journal — the credibility ledger (blueprint module 7).

    Every historical v3.2-era pick with full audit trail: confidence, the
    specialist score vector + regime-at-issuance (predictions.scores), entry/
    target/stop, resolution, exit, P&L. Plus aggregate honesty metrics computed
    over ALL resolved picks (not just the page): win rate with a 95% Wilson CI,
    expectancy, Brier score, and the pre-registered falsification verdict
    (core.prediction.FALSIFICATION_THRESHOLD, blueprint §10). Paginated, newest
    first. Public, read-only — this is the auditable record the 80% claim rests on.
    """
    import math as _m
    from core.prediction import FALSIFICATION_THRESHOLD
    try:
        lim = max(1, min(200, int(limit)))
        off = max(0, int(offset))
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM predictions WHERE symbol=%s AND id >= %s",
                (symbol, _V32_ERA_MIN_ID))
            total = cur.fetchone()[0]
            cur.execute(
                "SELECT id,symbol,direction,confidence,entry_price,target_price,stop_price,"
                "predicted_at,expires_at,resolved_at,outcome,exit_price,pnl_pct,features,scores "
                "FROM predictions WHERE symbol=%s AND id >= %s "
                "ORDER BY predicted_at DESC NULLS LAST, id DESC LIMIT %s OFFSET %s",
                (symbol, _V32_ERA_MIN_ID, lim, off))
            rows = cur.fetchall()
            cur.execute(
                "SELECT confidence,outcome,pnl_pct FROM predictions "
                "WHERE symbol=%s AND id >= %s AND outcome IS NOT NULL",
                (symbol, _V32_ERA_MIN_ID))
            resolved = cur.fetchall()

        picks = []
        for r in rows:
            (pid, sym, direction, conf, entry, target, stop, pred_at, exp_at,
             res_at, outcome, exit_p, pnl, feats, scrs) = r
            picks.append({
                "id": pid, "symbol": sym, "direction": direction,
                "confidence": float(conf) if conf is not None else None,
                "entry_price": entry, "target_price": target, "stop_price": stop,
                "predicted_at": pred_at, "expires_at": exp_at, "resolved_at": res_at,
                "outcome": outcome, "exit_price": exit_p,
                "pnl_pct": float(pnl) if pnl is not None else None,
                "features": _coerce_json(feats), "scores": _coerce_json(scrs),
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
            "symbol": symbol,
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


@APP.get("/api/wolf/kill-status")
def wolf_kill_status():
    """Live kill-condition dashboard (audit §2). Evaluates the env-tunable
    safety thresholds (win rate / Brier / consecutive losses / expectancy) over
    the rolling resolved-pick history and returns per-condition current-vs-
    threshold with a green/red/insufficient flag. Read-only — does not enforce.
    """
    try:
        from core.prediction import evaluate_kill_conditions, engine_pause_state
        out = evaluate_kill_conditions()
        if isinstance(out, dict):
            out["engine_pause"] = engine_pause_state()
        return out
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@APP.post("/api/admin/resume-engine")
def admin_resume_engine(x_cron_secret: str = Header(default="")):
    """Clear a kill-condition pause and resume firing (audit §2 enforcement).
    Manual recovery for pause/degrade/halt trips that do not auto-resume."""
    if not _cron_ok(x_cron_secret, strict=True):
        raise HTTPException(status_code=403)
    try:
        from core.prediction import resume_engine
        return resume_engine()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@APP.post("/api/test-alert")
def test_alert():
    """Send test message to Telegram to verify connection."""
    from core.telegram import send_test
    ok = send_test()
    return {"ok": ok, "message": "Test alert sent to Telegram + Discord"}

@APP.post("/api/retrain")
def retrain(x_cron_secret: str = Header(default="")):
    """Train XGBoost on ghost_prediction_outcomes. Inline - no import needed."""
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

@APP.get("/api/price/{symbol}")
def get_price_endpoint(symbol: str, asset_type: str = "stock"):
    """WOLF-only mode: asset_type is ignored, always returns stock price."""
    from core.prices import get_price
    price = get_price(symbol)
    return {"ok": price is not None, "symbol": symbol, "price": price}

@APP.post("/api/migrate-outcomes")
def migrate_outcomes(x_cron_secret: str = Header(default="")):
    """INSERT from ghost_prediction_outcomes (13k rows) into predictions."""
    if not _cron_ok(x_cron_secret):
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
                    'stock'
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
    """
    BUY-only WIN/LOSS in the same v3.2 window as /api/stats post_v32
    (V3_STATS_START_TS or min tp_sl_daily trained_at).
    """
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

@APP.get("/api/stats/confidence-buckets")
def get_stats_confidence_buckets():
    """Realized WIN/LOSS per confidence bucket since the v3.2 cutover.

    Public read-only — same convention as /api/stats and /api/stats/v32.
    Diagnostic for confidence calibration: if a high bucket wins at chance
    rate, confidence carries no signal and the engine needs recalibration.
    """
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


@APP.get("/api/stats")
def get_stats():
    """Overall accuracy stats across all sources."""
    with db_conn() as conn:
        return _compute_get_stats(conn.cursor())

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
    """Delete broken predictions with absurd entry/target combos.

    Filter: entry_price > 50 AND target_price < 1 (impossible legitimate trade).
    """
    if not _cron_ok(x_cron_secret):
        raise HTTPException(status_code=403)
    with db_conn() as conn:
        cur = conn.cursor()
        # Count first
        # Filter rationale: legitimate predictions have target within ~20% of entry.
        # Only impossible/garbage rows have entry > $50 with target < $1 (order-of-magnitude mismatch).
        cur.execute("SELECT COUNT(*) FROM predictions WHERE entry_price > 50 AND target_price < 1 AND predicted_at IS NOT NULL")
        garbage_count = cur.fetchone()[0]
        # Delete predictions with impossible entry/target combo
        # Filter rationale: legitimate predictions have target within ~20% of entry.
        # Only impossible/garbage rows have entry > $50 with target < $1 (order-of-magnitude mismatch).
        cur.execute("DELETE FROM predictions WHERE entry_price > 50 AND target_price < 1 AND predicted_at IS NOT NULL")
        deleted = cur.rowcount
        # Recount clean predictions
        cur.execute("SELECT outcome, COUNT(*) FROM predictions WHERE outcome IN ('WIN','LOSS') AND predicted_at IS NOT NULL GROUP BY outcome")
        counts = {r[0]: r[1] for r in cur.fetchall()}
    return {"ok": True, "deleted": deleted, "remaining": counts}

@APP.post("/api/watchdog")
def run_watchdog(x_cron_secret: str = Header(default="")):
    """Check open picks vs live prices. Send Telegram alert if target or stop hit."""
    if not _cron_ok(x_cron_secret):
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
            price = get_price(symbol, asset_type or "stock")
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
                    send_position_alert(symbol, direction, hit, entry, price, round(pnl, 2), conf or 0)
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

@APP.get("/cockpit", include_in_schema=False)
def cockpit():
    import os as _os
    _path = _os.path.join(_os.path.dirname(__file__), "cockpit.html")
    with open(_path, encoding="utf-8") as _f:
        return HTMLResponse(_f.read())


# ────────────────────────────────────────────────────────────────
# /admin — cookie-login operator console (PR #28)
# ────────────────────────────────────────────────────────────────
# Replaced HTTP Basic Auth (PR #23) which rendered blank on production —
# browsers/edge proxies mishandle the 401 Basic challenge. Cookie login is
# a plain HTML form → no browser auth dialog, no proxy quirks. The cookie
# is an HMAC-signed {expiry}.{sig} token so it can't be forged client-side.
_ADMIN_COOKIE = "gp_admin"
_ADMIN_TTL_S = 28800  # 8 hours


def _admin_mint_token(ttl_s: int = _ADMIN_TTL_S) -> str:
    secret = os.environ.get("CRON_SECRET", "")
    exp = str(int(time.time()) + ttl_s)
    sig = hmac.new(secret.encode("utf-8"), exp.encode("utf-8"), "sha256").hexdigest()
    return exp + "." + sig


def _admin_token_valid(token: str) -> bool:
    """True if the cookie token is a non-expired, correctly-signed value.

    Dev mode (no CRON_SECRET) always returns True — mirrors _cron_ok
    strict=False semantics.
    """
    secret = os.environ.get("CRON_SECRET", "")
    if not secret:
        return True
    if not token or "." not in token:
        return False
    try:
        exp_str, sig = token.rsplit(".", 1)
        if int(exp_str) < int(time.time()):
            return False
        expected = hmac.new(secret.encode("utf-8"), exp_str.encode("utf-8"), "sha256").hexdigest()
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False


_ADMIN_LOGIN_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Ghost Protocol — Admin Login</title>
<style>*{margin:0;padding:0;box-sizing:border-box}body{background:#0a0a0a;color:#fff;
font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',sans-serif;min-height:100vh;
display:flex;align-items:center;justify-content:center}
.box{background:#111;border:1px solid #1e1e1e;border-radius:14px;padding:32px;width:340px;max-width:90vw}
.logo{font-size:16px;font-weight:800;letter-spacing:2px;margin-bottom:6px}.logo span{color:#ff3b3b}
.sub{font-size:12px;color:#666;margin-bottom:20px}
input{width:100%;background:#0a0a0a;border:1px solid #2a2a2a;color:#fff;padding:11px 12px;
border-radius:8px;font-size:14px;font-family:ui-monospace,Menlo,monospace;margin-bottom:12px}
button{width:100%;background:#ff3b3b;color:#fff;border:none;padding:11px;border-radius:8px;
font-size:13px;font-weight:700;letter-spacing:.5px;cursor:pointer}button:hover{background:#e03333}
.err{color:#ff3b3b;font-size:12px;min-height:16px;margin-top:10px}</style></head><body>
<div class="box"><div class="logo">&#128123; GHOST <span>ADMIN</span></div>
<div class="sub">Enter the cron secret to access the operator console.</div>
<input type="password" id="secret" placeholder="CRON_SECRET" autocomplete="off" autofocus
onkeydown="if(event.key==='Enter')doLogin()">
<button onclick="doLogin()">Sign in</button><div class="err" id="err"></div></div>
<script>
async function doLogin(){
  var s=document.getElementById('secret').value||'';
  var e=document.getElementById('err');e.textContent='Signing in...';
  try{
    var r=await fetch('/admin/login',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({secret:s})});
    if(r.ok){location.reload();}
    else{e.textContent='Wrong secret. Try again.';}
  }catch(_){e.textContent='Network error.';}
}
</script></body></html>"""


@APP.get("/admin", include_in_schema=False)
def admin_page(request: Request):
    """Serve admin.html when the cookie is valid; else the login page."""
    token = request.cookies.get(_ADMIN_COOKIE, "")
    if not _admin_token_valid(token):
        return HTMLResponse(_ADMIN_LOGIN_HTML)
    import os as _os
    _path = _os.path.join(_os.path.dirname(__file__), "admin.html")
    with open(_path, encoding="utf-8") as _f:
        return HTMLResponse(_f.read())


@APP.post("/admin/login", include_in_schema=False)
async def admin_login(request: Request):
    """Validate the posted secret against CRON_SECRET; set the signed cookie.

    JSON body {"secret": "..."} (no python-multipart dependency). On success
    sets an HttpOnly, SameSite=Lax cookie valid for 8h and returns {ok:true}.
    """
    expected = os.environ.get("CRON_SECRET", "")
    provided = ""
    try:
        body = await request.json()
        provided = str(body.get("secret", "") or "")
    except Exception:
        provided = ""
    if expected and not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        return JSONResponse({"ok": False, "error": "invalid secret"}, status_code=401)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        _ADMIN_COOKIE, _admin_mint_token(),
        max_age=_ADMIN_TTL_S, httponly=True, samesite="lax",
        secure=os.getenv("ADMIN_COOKIE_SECURE", "1").strip() in ("1", "true", "yes", "on"),
    )
    return resp


@APP.post("/admin/logout", include_in_schema=False)
def admin_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_ADMIN_COOKIE)
    return resp


if os.path.exists("static"):
    APP.mount("/static", StaticFiles(directory="static"), name="static")

# ════════════════════════════════════════════════════════════
# GHOST v3 ENDPOINTS — Backtested signal engine
# ════════════════════════════════════════════════════════════

@APP.get("/api/v3/status")
def v3_status():
    """Model status — accuracy, edge over random, top features.

    WOLF-only hardening (PR #8): even if ghost_v3_model has stale rows for
    other symbols (e.g. BCH/SOL/UNI from the pre-WOLF crypto era), this
    endpoint never exposes them. Filtering at the wrapper keeps the source
    function reusable for one-off diagnostics that may need full coverage.
    """
    from core.signal_engine import get_model_status
    st = get_model_status() or {}
    syms = st.get("symbols") or {}
    if syms:
        wolf_only = {k: v for k, v in syms.items() if str(k).upper() == "WOLF"}
        st["symbols"] = wolf_only
        if st.get("trained"):
            st["models"] = len(wolf_only)
            if not wolf_only:
                st["trained"] = False
                st["reason"] = "No WOLF model trained — run /api/v3/train"
    return st


@APP.get("/api/coverage")
def coverage_status():
    """Coverage maintenance status for monitoring/ops."""
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
            cur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
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


def _cockpit_cached_db_payload():
    """
    Stats + direction + activity in one DB connection; v3 JSON cached with them.
    Health and regime stay fresh per request. TTL: COCKPIT_CONTEXT_CACHE_SEC (0 = off).
    """
    ttl = float(os.getenv("COCKPIT_CONTEXT_CACHE_SEC", "8"))
    now = time.time()
    if (
        ttl > 0
        and _COCKPIT_DB_CACHE["stats"] is not None
        and (now - _COCKPIT_DB_CACHE["t"]) < ttl
    ):
        return (
            _COCKPIT_DB_CACHE["stats"],
            _COCKPIT_DB_CACHE["direction"],
            _COCKPIT_DB_CACHE["v3"],
            _COCKPIT_DB_CACHE["activity"],
        )
    with db_conn() as conn:
        cur = conn.cursor()
        stats = _compute_get_stats(cur)
        direction = compute_stats_by_direction(cur)
        activity = _cockpit_activity_on_cursor(cur)
    v3 = v3_status()
    if ttl > 0:
        _COCKPIT_DB_CACHE["t"] = now
        _COCKPIT_DB_CACHE["stats"] = stats
        _COCKPIT_DB_CACHE["direction"] = direction
        _COCKPIT_DB_CACHE["v3"] = v3
        _COCKPIT_DB_CACHE["activity"] = activity
    return stats, direction, v3, activity


@APP.get("/api/cockpit/context", include_in_schema=False)
def cockpit_context():
    """Single fetch for /cockpit: health, stats, direction, regime, v3, activity summary."""
    try:
        stats, direction, v3, activity = _cockpit_cached_db_payload()
        # WOLF-only mode: regime gate is a no-op.
        regime = {"ok": True, "block_crypto_buys": False, "reduce_size": False, "reason": "", "btc_24h_pct": 0.0}
        return {
            "ok": True,
            "health": health(),
            "stats": stats,
            "direction": direction,
            "regime": regime,
            "v3": v3,
            "activity": activity,
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:120]}, status_code=500)


@APP.post("/api/v3/train")
def _v3_train_collect_symbols() -> list:
    """Collect symbols for v3 training, filtered to WOLF only.

    WOLF-only hardening (matches the pattern in _compute_get_stats and
    v3_status): even if STOCK_SYMBOLS env is dirty with non-WOLF rows
    (e.g. TSLA/META/AMZN/T) or user_portfolio has stale non-WOLF
    positions, training only ever runs on WOLF. Without this filter,
    the user observed 0/13 symbols passing because all 12 non-WOLF
    symbols also lacked Alpaca data, wasting compute and noise-logging.
    """
    stocks = [(s.strip(), "stock") for s in os.getenv("STOCK_SYMBOLS", "WOLF").split(",") if s.strip()] or [("WOLF", "stock")]
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT symbol FROM user_portfolio")
            for (sym,) in cur.fetchall():
                entry = (str(sym or "").strip().upper(), "stock")
                if entry[0] and entry not in stocks:
                    stocks.append(entry)
    except Exception:
        pass
    return [s for s in stocks if s[0].upper() == "WOLF"] or [("WOLF", "stock")]


def _record_v3_train_state(**fields) -> None:
    """Write v3_train phase markers into ghost_state for /api/v3/train/last.

    Fields are keyed as last_v3_train_<name>; each call upserts only the
    provided fields so partial updates work across the train phases.
    """
    if not fields:
        return
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
            for name, value in fields.items():
                cur.execute(
                    "INSERT INTO ghost_state(key,val) VALUES(%s,%s) "
                    "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                    (f"last_v3_train_{name}", "" if value is None else str(value)),
                )
    except Exception as _e:
        LOGGER.warning("v3_train state write failed: " + str(_e)[:120])


def v3_train(x_cron_secret: str = Header(default=""), force: bool = False):
    """
    Train v3 XGBoost model on 1yr historical data (WOLF-only).
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
    LOGGER.info(f"[v3_train] PR14_DIAG ENDPOINT_INVOKED force={force}")
    if not _cron_ok(x_cron_secret):
        return JSONResponse({"ok":False,"error":"Forbidden"}, status_code=403)
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
    threading.Thread(target=_train, daemon=True).start()
    # PR #19: response now includes _pr_version so the operator can verify
    # from a single curl whether the deployed code is fresh or stale. If the
    # response is missing this field, Railway is serving a pre-PR-#19 version.
    return {"ok": True, "message": "Training started in background. Check /api/v3/train/last in 3-5 minutes.",
            "force": force, "started_at": started_at, "_pr_version": 19}


# PR #19 deploy-version constant. Bump on every "did Railway pick up
# the new code?" PR so /api/_version reveals the truth in one curl.
_RUNNING_PR_VERSION = 34


@APP.get("/api/_version")
def deploy_version():
    """Return the running code version + Railway-injected git/deploy IDs.

    Lets the operator verify from a single curl whether the deployed
    container is running the expected commit. No auth required —
    nothing sensitive in the response.
    """
    return {
        "ok": True,
        "_pr_version": _RUNNING_PR_VERSION,
        "git_sha": os.getenv("RAILWAY_GIT_COMMIT_SHA", "unset"),
        "deploy_id": os.getenv("RAILWAY_DEPLOYMENT_ID", "unset"),
        "deploy_version_env": os.getenv("DEPLOY_VERSION", "unset"),
        "ts": int(time.time()),
        "endpoints_present": {
            "v3_train_force_param": True,    # PR #18
            "v3_train_last": True,            # PR #18
            "v3_train_sync": True,            # PR #19
            "diag_data_sources": True,        # PR #17
            "wolf_signal_alert_check": True,  # PR #8
        },
    }


@APP.post("/api/v3/train/sync")
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
    LOGGER.info(f"[v3_train_sync] PR19_DIAG ENDPOINT_INVOKED force={force}")
    if not _cron_ok(x_cron_secret):
        return JSONResponse({"ok": False, "error": "Forbidden"}, status_code=403)
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


@APP.get("/api/v3/train/last")
def v3_train_last():
    """Return the most recent v3_train invocation result from ghost_state.

    Public read-only — same convention as /api/v3/status. Returns a flat
    dict of the last_v3_train_* fields so the cockpit can render a
    "Last training result" panel without needing the cron secret.
    """
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
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
        return {"ok": True, "last": out or None}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@APP.post("/api/v3/backtest")
def v3_backtest(x_cron_secret: str = Header(default=""), symbol: str = "WOLF", asset_type: str = "stock"):
    """
    Historical samples for v3 training: TP/SL WIN before stop within N daily bars
    (same rules as live reconcile / core.vol_targets).
    """
    if not _cron_ok(x_cron_secret):
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
