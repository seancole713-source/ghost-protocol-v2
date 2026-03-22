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
                cur.execute("SELECT predicted_at FROM predictions ORDER BY predicted_at DESC LIMIT 1")
            except Exception:
                conn.rollback()
                cur.execute("SELECT created_at FROM predictions ORDER BY created_at DESC LIMIT 1")
            row = cur.fetchone()
            if row: freshness_min = int((time.time() - row[0]) / 60)
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
    """Normalise a prediction row to v2 schema regardless of v1/v2 column names."""
    return {
        "id": r.get("id"),
        "symbol": r.get("symbol", ""),
        "direction": r.get("direction", ""),
        "confidence": r.get("confidence") or r.get("confidence_score") or 0,
        "entry_price": r.get("entry_price") or r.get("entry") or 0,
        "target_price": r.get("target_price") or r.get("target") or 0,
        "stop_price": r.get("stop_price") or r.get("stop") or 0,
        "predicted_at": r.get("predicted_at") or r.get("created_at") or 0,
        "expires_at": r.get("expires_at") or 0,
        "outcome": r.get("outcome") or r.get("result"),
        "exit_price": r.get("exit_price"),
        "pnl_pct": r.get("pnl_pct") or r.get("pnl"),
        "asset_type": r.get("asset_type", "crypto"),
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
        return {"ok": True, "trades": resolved, "total": len(resolved),
                "wins": wins, "losses": len(resolved)-wins,
                "win_rate_pct": round(wins/len(resolved)*100,1) if resolved else 0,
                "total_pnl_pct": round(total_pnl,2)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@APP.post("/api/run-predictions")
def trigger_predictions(x_cron_secret: str = Header(default="")):
    if CRON_SECRET and x_cron_secret != CRON_SECRET:
        raise HTTPException(status_code=403, detail="Invalid cron secret")
    from core.prediction import run_prediction_cycle
    picks = run_prediction_cycle()
    return {"ok": True, "picks_generated": len(picks), "picks": picks}

@APP.post("/api/reconcile")
def trigger_reconcile(x_cron_secret: str = Header(default="")):
    if CRON_SECRET and x_cron_secret != CRON_SECRET:
        raise HTTPException(status_code=403, detail="Invalid cron secret")
    from core.prediction import reconcile_outcomes
    count = reconcile_outcomes()
    return {"ok": True, "resolved": count}

@APP.get("/api/price/{symbol}")
def get_price_endpoint(symbol: str, asset_type: str = "crypto"):
    from core.prices import get_price
    price = get_price(symbol, asset_type)
    return {"ok": price is not None, "symbol": symbol, "price": price}

@APP.get("/cockpit")
def cockpit():
    html = "<h1>Ghost Protocol v2 - LIVE</h1><ul><li><a href=/health>/health</a></li><li><a href=/api/picks>/api/picks</a></li><li><a href=/api/history>/api/history</a></li><li><a href=/api/schema>/api/schema</a></li></ul>"
    return HTMLResponse(html)

if os.path.exists("static"):
    APP.mount("/static", StaticFiles(directory="static"), name="static")