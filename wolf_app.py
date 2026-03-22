"""
wolf_app.py - Ghost Protocol v2 entry point.
Clean FastAPI app. Procfile: web: uvicorn wolf_app:APP
"""
import os, time, logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from core.db import db_conn, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
LOGGER = logging.getLogger("ghost")
CRON_SECRET = os.getenv("CRON_SECRET", "")

@asynccontextmanager
async def lifespan(app: FastAPI):
    LOGGER.info("Ghost Protocol v2 starting...")
    init_db()
    from core import scheduler
    from core.prediction import run_prediction_cycle, reconcile_outcomes
    scheduler.register("prediction_cycle", run_prediction_cycle, interval_s=3600)
    scheduler.register("reconcile", reconcile_outcomes, interval_s=900)
    scheduler.start()
    LOGGER.info("Ghost Protocol v2 ready")
    yield
    scheduler.stop()
    LOGGER.info("Ghost Protocol v2 stopped")

APP = FastAPI(title="Ghost Protocol v2", version="2.0.0", lifespan=lifespan)

APP.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Health
@APP.get("/health")
def health():
    from core.prices import check_feeds
    from core import scheduler
    try:
        with db_conn() as conn:
            conn.cursor().execute("SELECT 1")
        db_ok = True
    except:
        db_ok = False
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT predicted_at FROM predictions ORDER BY predicted_at DESC LIMIT 1")
            row = cur.fetchone()
            freshness_min = int((time.time() - row[0]) / 60) if row else None
    except:
        freshness_min = None
    feeds = check_feeds()
    tasks = scheduler.status()
    issues = []
    if not db_ok: issues.append("DB connection failed")
    if freshness_min and freshness_min > 90: issues.append(f"Predictions stale: {freshness_min}m")
    feeds_ok = sum(1 for k,v in feeds.items() if k != "summary" and v)
    if feeds_ok < 2: issues.append(feeds["summary"])
    score = max(0, 100 - len(issues) * 15)
    return {"status": "healthy" if not issues else "degraded", "score": score, "db": db_ok,
            "predictions_freshness_min": freshness_min, "price_feeds": feeds,
            "tasks": tasks, "issues": issues}

# Picks
@APP.get("/api/picks")
def get_picks():
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, symbol, direction, confidence, entry_price,
                       target_price, stop_price, predicted_at, expires_at,
                       outcome, exit_price, pnl_pct, asset_type
                FROM predictions ORDER BY predicted_at DESC LIMIT 50
            """)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        active = [r for r in rows if r["outcome"] is None]
        resolved = [r for r in rows if r["outcome"] is not None]
        wins = sum(1 for r in resolved if r["outcome"] == "WIN")
        total = len(resolved)
        return {"ok": True, "active": active, "recent": resolved[:20],
                "accuracy_pct": round(wins/total*100, 1) if total else 0,
                "wins": wins, "losses": total - wins, "total": total}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

# Trigger predictions
@APP.post("/api/run-predictions")
def trigger_predictions(x_cron_secret: str = Header(default="")):
    if CRON_SECRET and x_cron_secret != CRON_SECRET:
        raise HTTPException(status_code=403, detail="Invalid cron secret")
    from core.prediction import run_prediction_cycle
    picks = run_prediction_cycle()
    return {"ok": True, "picks_generated": len(picks), "picks": picks}

# Reconcile outcomes
@APP.post("/api/reconcile")
def trigger_reconcile(x_cron_secret: str = Header(default="")):
    if CRON_SECRET and x_cron_secret != CRON_SECRET:
        raise HTTPException(status_code=403, detail="Invalid cron secret")
    from core.prediction import reconcile_outcomes
    count = reconcile_outcomes()
    return {"ok": True, "resolved": count}

# History
@APP.get("/api/history")
def get_history(limit: int = 200):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.symbol, p.direction, p.confidence, p.entry_price,
                   p.exit_price, p.outcome, p.pnl_pct, p.predicted_at,
                   p.resolved_at, p.asset_type,
                   pt.usd_in, pt.usd_out
            FROM predictions p
            LEFT JOIN paper_trades pt ON pt.prediction_id = p.id
            WHERE p.outcome IS NOT NULL
            ORDER BY p.resolved_at DESC LIMIT %s
        """, (limit,))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    wins = sum(1 for r in rows if r["outcome"] == "WIN")
    total_pnl = sum(r["pnl_pct"] or 0 for r in rows)
    return {"ok": True, "trades": rows, "total": len(rows), "wins": wins,
            "losses": len(rows) - wins,
            "win_rate_pct": round(wins/len(rows)*100, 1) if rows else 0,
            "total_pnl_pct": round(total_pnl, 2)}

# Price
@APP.get("/api/price/{symbol}")
def get_price_endpoint(symbol: str, asset_type: str = "crypto"):
    from core.prices import get_price
    price = get_price(symbol, asset_type)
    return {"ok": price is not None, "symbol": symbol, "price": price}

# Dashboard placeholder
@APP.get("/cockpit")
def cockpit():
    from fastapi.responses import HTMLResponse
    return HTMLResponse("<h1>Ghost Protocol v2</h1><p>Dashboard coming Week 4.</p><p><a href=/health>Health</a> | <a href=/api/picks>Picks</a> | <a href=/api/history>History</a></p>")

if os.path.exists("static"):
    APP.mount("/static", StaticFiles(directory="static"), name="static")