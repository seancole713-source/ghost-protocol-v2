import os, time, logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from core.db import db_conn, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
LOGGER = logging.getLogger("ghost")
CRON_SECRET = os.getenv("CRON_SECRET", "")

def _morning_card_job():
    """Run prediction cycle and send morning Telegram card."""
    from core.prediction import run_prediction_cycle
    from core.telegram import send_morning_card
    from core.db import db_conn
    picks = run_prediction_cycle()
    # Get week stats
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cutoff = int(time.time()) - 7*86400
            cur.execute(
                "SELECT outcome, pnl_pct FROM predictions WHERE resolved_at > %s AND outcome IN ('WIN','LOSS')",
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
            all_wins = sum(1 for r in all_rows if r[0] == "WIN")
            alltime_wr = round(all_wins/len(all_rows)*100,1) if all_rows else 0
    except:
        wins, losses, pnl, alltime_wr = 0, 0, 0.0, 0
    week_stats = {"wins": wins, "losses": losses, "pnl_usd": pnl, "alltime_wr": alltime_wr}
    if picks:
        send_morning_card(picks, week_stats)
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
    from core import scheduler
    from core.prediction import reconcile_outcomes
    from core.news import run_news_cycle
    scheduler.register("morning_card", _morning_card_job, interval_s=86400)
    scheduler.register("weekly_summary", _weekly_summary_job, interval_s=3600)
    scheduler.register("reconcile", reconcile_outcomes, interval_s=900)
    scheduler.register("news", run_news_cycle, interval_s=1800)
    scheduler.start()
    LOGGER.info("Ghost Protocol v2 ready ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ 3 tasks running")
    yield
    scheduler.stop()

APP = FastAPI(title="Ghost Protocol v2", version="2.0.0", lifespan=lifespan)
APP.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@APP.get("/health")
def health():
    from core.prices import check_feeds
    from core import scheduler
    try:
        with db_conn() as conn: conn.cursor().execute("SELECT 1")
        db_ok = True
    except: db_ok = False
    freshness_min = None
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            try:
                cur.execute("SELECT predicted_at FROM predictions WHERE predicted_at IS NOT NULL ORDER BY predicted_at DESC LIMIT 1")
            except:
                conn.rollback()
                cur.execute("SELECT run_at FROM predictions ORDER BY run_at DESC LIMIT 1")
            row = cur.fetchone()
            if row and row[0]: freshness_min = int((time.time() - float(row[0])) / 60)
    except: pass
    feeds = check_feeds()
    tasks = scheduler.status()
    issues = []
    if not db_ok: issues.append("DB connection failed")
    if freshness_min and freshness_min > 90: issues.append("Predictions stale: " + str(freshness_min) + "m")
    feeds_ok = sum(1 for k,v in feeds.items() if k != "summary" and v)
    if feeds_ok < 2: issues.append(feeds["summary"])
    score = max(0, 100 - len(issues) * 15)
    return {"status": "healthy" if not issues else "degraded", "score": score,
            "db": db_ok, "predictions_freshness_min": freshness_min,
            "price_feeds": feeds, "tasks": tasks, "issues": issues}

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
    return {
        "id": r.get("id"),
        "symbol": r.get("symbol",""),
        "direction": r.get("direction",""),
        "confidence": r.get("confidence") or r.get("confidence_score") or 0,
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
        total_pnl = sum(r["pnl_pct"] or 0 for r in resolved)
        return {"ok": True, "trades": resolved, "total": len(resolved), "wins": wins,
                "losses": len(resolved)-wins,
                "win_rate_pct": round(wins/len(resolved)*100,1) if resolved else 0,
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

@APP.get("/api/stats")
def get_stats():
    """Overall accuracy stats across all sources."""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT outcome, COUNT(*) FROM predictions WHERE outcome IN ('WIN','LOSS') GROUP BY outcome")
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
        cur.execute("SELECT COUNT(*) FROM predictions WHERE entry_price < 1.0 AND entry_price > 0 AND predicted_at IS NOT NULL")
        garbage_count = cur.fetchone()[0]
        # Delete predictions where entry_price was $0.50 (the placeholder confidence value)
        cur.execute("DELETE FROM predictions WHERE entry_price < 1.0 AND entry_price > 0 AND predicted_at IS NOT NULL")
        deleted = cur.rowcount
        # Recount clean predictions
        cur.execute("SELECT outcome, COUNT(*) FROM predictions WHERE outcome IN ('WIN','LOSS') AND predicted_at IS NOT NULL GROUP BY outcome")
        counts = {r[0]: r[1] for r in cur.fetchall()}
    return {"ok": True, "deleted": deleted, "remaining": counts}

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