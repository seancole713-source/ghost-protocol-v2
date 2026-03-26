"""core/portfolio_routes.py - Personal portfolio tracker."""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from core.db import db_conn
import time

portfolio_router = APIRouter()

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS user_portfolio (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    asset_type TEXT DEFAULT 'crypto',
    quantity FLOAT NOT NULL,
    buy_price FLOAT NOT NULL,
    buy_date TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    manual_price FLOAT DEFAULT NULL,
    created_at BIGINT DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT
)
"""

@portfolio_router.get("/api/portfolio")
def get_portfolio():
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(_CREATE_TABLE); conn.commit()
        cur.execute("SELECT id,symbol,asset_type,quantity,buy_price,buy_date,notes,manual_price FROM user_portfolio ORDER BY id DESC")
        rows = cur.fetchall()
    positions = []
    for r in rows:
        sym, atype, qty, bp = r[1], r[2], float(r[3]), float(r[4])
        cost = round(qty * bp, 2)
        live = None
        manual_p = r[8] if len(r) > 8 else None  # manual_price column
        try:
            from core.prices import get_price
            live = get_price(sym, atype)
        except Exception:
            pass
        if live is None and manual_p:
            live = manual_p  # fallback to manually set price
        val = round(qty * live, 2) if live else None
        gl = round(val - cost, 2) if val is not None else None
        glp = round(gl / cost * 100, 2) if gl is not None and cost > 0 else None
        sig = None
        try:
            with db_conn() as c2:
                c = c2.cursor()
                c.execute("SELECT direction,confidence FROM predictions WHERE symbol=%s AND outcome IS NULL AND expires_at>%s ORDER BY confidence DESC LIMIT 1",(sym,int(time.time())))
                row = c.fetchone()
                if row: sig={"direction":row[0],"confidence":round(float(row[1]),3)}
        except Exception:
            pass
        positions.append({"id":r[0],"symbol":sym,"asset_type":atype,"quantity":qty,"buy_price":bp,
            "cost_basis":cost,"live_price":round(live,6) if live else None,
            "current_value":val,"gain_loss":gl,"gain_loss_pct":glp,
            "buy_date":r[5],"notes":r[6],"ghost_signal":sig})
    tc = sum(p["cost_basis"] for p in positions)
    tv = sum(p["current_value"] for p in positions if p["current_value"])
    return {"ok":True,"positions":positions,"total_cost":round(tc,2),
        "total_value":round(tv,2) if tv else None,
        "total_gain_loss":round(tv-tc,2) if tv else None}

@portfolio_router.post("/api/portfolio")
async def add_portfolio(request: Request):
    d = await request.json()
    sym = str(d.get("symbol","")).upper().strip()
    atype = str(d.get("asset_type","crypto"))
    qty = float(d.get("quantity",0))
    bp = float(d.get("buy_price",0))
    if not sym or qty <= 0 or bp <= 0:
        return {"ok":False,"error":"symbol, quantity and buy_price required"}
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(_CREATE_TABLE); conn.commit()
        cur.execute("INSERT INTO user_portfolio (symbol,asset_type,quantity,buy_price,buy_date,notes) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (sym,atype,qty,bp,str(d.get("buy_date","")),str(d.get("notes",""))))
        new_id = cur.fetchone()[0]; conn.commit()
    return {"ok":True,"id":new_id,"symbol":sym}

@portfolio_router.post("/api/portfolio/{position_id}/price")
def set_manual_price(position_id: int, data: dict):
    """Set manual price when live feed fails (e.g. WOLF)."""
    try:
        price = float(data.get("price", 0))
        with db_conn() as conn:
            cur = conn.cursor()
            try:
                cur.execute("ALTER TABLE user_portfolio ADD COLUMN IF NOT EXISTS manual_price FLOAT DEFAULT NULL")
            except Exception:
                conn.rollback()
            cur.execute("UPDATE user_portfolio SET manual_price=%s WHERE id=%s", (price, position_id))
        return {"ok": True, "id": position_id, "manual_price": price}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@portfolio_router.delete("/api/portfolio/{position_id}")
def del_portfolio(position_id: int):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM user_portfolio WHERE id=%s",(position_id,))
        conn.commit()
    return {"ok":True}

@portfolio_router.get("/")
def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/cockpit")

@portfolio_router.get("/api/v2/recent")
def v2_recent():
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""SELECT id,symbol,direction,confidence,entry_price,exit_price,pnl_pct,outcome,predicted_at,expires_at,asset_type
            FROM predictions WHERE outcome IS NOT NULL AND predicted_at IS NOT NULL
            ORDER BY expires_at DESC NULLS LAST LIMIT 50""")
        rows = cur.fetchall()
    trades=[]; wins=losses=0
    for r in rows:
        o=r[7]; pnl=float(r[6] or 0)
        if o=="WIN": wins+=1
        elif o in ("LOSS","STOP","EXPIRED"): losses+=1
        trades.append({"id":r[0],"symbol":r[1],"direction":r[2],"confidence":r[3],
            "entry_price":float(r[4] or 0),"exit_price":float(r[5] or 0) if r[5] else None,
            "pnl_pct":round(pnl,3),"outcome":o,"predicted_at":r[8],"expires_at":r[9],"asset_type":r[10]})
    total=wins+losses
    return {"ok":True,"trades":trades,"total":total,"wins":wins,"losses":losses,
        "win_rate_pct":round(wins/total*100,1) if total else 0}

@portfolio_router.get("/api/stats/direction")
def stats_by_direction():
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT direction,
                   COUNT(*) as total,
                   SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
                   ROUND(AVG(CASE WHEN pnl_pct IS NOT NULL THEN pnl_pct ELSE 0 END)::numeric,2) as avg_pnl
            FROM predictions
            WHERE outcome IN ('WIN','LOSS','STOP','EXPIRED')
            GROUP BY direction
        """)
        rows = cur.fetchall()
    result = {}
    for r in rows:
        d = "BUY" if r[0] in ("UP","BUY") else "SELL"
        total = int(r[1]); wins = int(r[2])
        result[d] = {"total":total,"wins":wins,"losses":total-wins,
            "win_rate_pct":round(wins/total*100,1) if total else 0,
            "avg_pnl":float(r[3])}
    return {"ok":True,"by_direction":result}
