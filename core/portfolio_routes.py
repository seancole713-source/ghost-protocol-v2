"""core/portfolio_routes.py - Personal portfolio tracker."""
import os

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from core.db import db_conn
from core.stats_direction import compute_stats_by_direction
from core.prediction_filters import REAL_TRADE_WHERE
import time

portfolio_router = APIRouter()

# Synthetic/test rows that must never reach the investor portfolio view. e2e
# roundtrips create ZZE2E<ts>; ZZ/TEST/GHOST are manual probes. Real tickers
# never match these. Filtered at the API layer (defense-in-depth) so a stale DB
# row can't pollute totals even if the boot purge hasn't run.
_GHOST_SYMBOL_PATTERNS = ("ZZE2E", "STOCK GHOST", "GHOST", "ZZ", "TEST")


def _is_ghost_symbol(sym) -> bool:
    up = str(sym or "").strip().upper()
    return any(up.startswith(p) or up == p for p in _GHOST_SYMBOL_PATTERNS)


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS user_portfolio (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    asset_type TEXT DEFAULT 'stock',
    quantity FLOAT NOT NULL,
    buy_price FLOAT NOT NULL,
    buy_date TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    manual_price FLOAT DEFAULT NULL,
    created_at BIGINT DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT
)
"""

def build_portfolio_payload() -> dict:
    """Portfolio JSON (shared by HTTP route and MCP ghost_portfolio tool)."""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(_CREATE_TABLE); conn.commit()
        cur.execute("SELECT id,symbol,asset_type,quantity,buy_price,buy_date,notes,manual_price FROM user_portfolio ORDER BY id DESC")
        rows = cur.fetchall()
    positions = []
    for r in rows:
        sym, atype, qty, bp = r[1], r[2], float(r[3]), float(r[4])
        if _is_ghost_symbol(sym):
            continue  # never surface synthetic/test rows or count them in totals
        cost = round(qty * bp, 2)
        live = None
        manual_p = r[7] if len(r) > 7 else None  # manual_price column (index 7)
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
    # Cash App imports sometimes duplicate the same symbol — keep the largest lot.
    deduped = {}
    for p in positions:
        sym = p["symbol"]
        prev = deduped.get(sym)
        if prev is None or p["quantity"] > prev["quantity"]:
            deduped[sym] = p
    positions = list(deduped.values())
    tc = sum(p["cost_basis"] for p in positions)
    tv = sum(p["current_value"] for p in positions if p["current_value"])
    return {"ok":True,"positions":positions,"total_cost":round(tc,2),
        "total_value":round(tv,2) if tv else None,
        "total_gain_loss":round(tv-tc,2) if tv else None}


@portfolio_router.get("/api/portfolio")
def get_portfolio(request: Request):
    from mcp.security import require_portfolio_auth
    require_portfolio_auth(request)
    return build_portfolio_payload()

@portfolio_router.post("/api/portfolio")
async def add_portfolio(request: Request):
    from mcp.security import require_portfolio_auth
    require_portfolio_auth(request)
    d = await request.json()
    sym = str(d.get("symbol","")).upper().strip()
    atype = str(d.get("asset_type","stock"))
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
def set_manual_price(position_id: int, data: dict, request: Request):
    """Set manual price when live feed fails (e.g. WOLF)."""
    from mcp.security import require_portfolio_auth
    require_portfolio_auth(request)
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
def del_portfolio(position_id: int, request: Request):
    from mcp.security import require_portfolio_auth
    require_portfolio_auth(request)
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM user_portfolio WHERE id=%s",(position_id,))
        conn.commit()
    return {"ok":True}


def rebuild_cashapp_watchlist(*, live_prices: dict) -> dict:
    """Replace portfolio watchlist with one row per Cash App symbol.

    Uses live price as cost for ~$1 tracking rows (avoids false exit alerts from
    back-calculating buy_price off full Cash App dollar P&L). WOLF and AMC keep
    explicit sizing from Cash App screenshots.
    """
    from core.risk_discipline import _ghost_state_set

    specs = [
        # symbol, qty, buy_price — None qty/buy means ~$1 notional at live
        ("WOLF", 13.48, None),  # buy from +$344.78 G/L
        ("AMC", 440.490437, 4.65),
        ("SPCE", None, None),
        ("YMM", None, None),
        ("AI", None, None),
        ("FLNC", None, None),
        ("OPTU", None, None),
        ("OPK", None, None),
        ("ODD", None, None),
        ("NOK", None, None),
        ("SABR", None, None),
        ("TME", None, None),
        ("CLNE", None, None),
        ("IQ", None, None),
        ("LULU", None, None),
    ]
    wolf_gl = 344.78
    inserted = []
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(_CREATE_TABLE)
        cur.execute("DELETE FROM user_portfolio")
        conn.commit()
        for sym, qty, buy in specs:
            live = float(live_prices.get(sym) or 0)
            if live <= 0:
                continue
            if sym == "WOLF" and qty:
                bp = live - (wolf_gl / qty)
                if bp <= 0:
                    bp = live * 0.5
            elif qty and buy:
                bp = float(buy)
            else:
                qty = 1.0 / live
                bp = live
            cur.execute(
                "INSERT INTO user_portfolio (symbol,asset_type,quantity,buy_price,buy_date,notes) "
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                (sym, "stock", round(float(qty), 6), round(float(bp), 4), "2026-06-04", "Cash App watchlist"),
            )
            inserted.append({"id": cur.fetchone()[0], "symbol": sym, "quantity": qty, "buy_price": bp})
        conn.commit()
    import json
    from datetime import datetime
    import pytz

    tz = pytz.timezone(os.getenv("GHOST_TZ", "America/Chicago"))
    today = datetime.now(tz).strftime("%Y-%m-%d")
    _ghost_state_set("portfolio_exit_alerted", json.dumps({"_date": today}))
    return {"ok": True, "count": len(inserted), "positions": inserted}


@portfolio_router.post("/api/admin/rebuild-cashapp-watchlist", include_in_schema=False)
async def admin_rebuild_cashapp_watchlist(x_cron_secret: str = Header(default="")):
    import json
    import urllib.request

    import wolf_app

    if not wolf_app._cron_ok(x_cron_secret, strict=True):
        raise HTTPException(status_code=403, detail="Forbidden")
    UA = {"User-Agent": "Mozilla/5.0"}
    symbols = [
        "WOLF", "AMC", "SPCE", "YMM", "AI", "FLNC", "OPTU",
        "OPK", "ODD", "NOK", "SABR", "TME", "CLNE", "IQ", "LULU",
    ]
    prices = {}
    for sym in symbols:
        if sym == "WOLF":
            try:
                from api.wolf_endpoints import wolf_price_payload_sync

                prices[sym] = float(wolf_price_payload_sync().get("price") or 0)
            except Exception:
                pass
        if not prices.get(sym):
            try:
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=1d"
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=15) as r:
                    d = json.loads(r.read())
                meta = d["chart"]["result"][0]["meta"]
                prices[sym] = float(meta.get("regularMarketPrice") or meta.get("previousClose") or 0)
            except Exception:
                prices[sym] = 0
    return rebuild_cashapp_watchlist(live_prices=prices)

@portfolio_router.get("/")
def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/cockpit")

@portfolio_router.get("/api/v2/recent")
def v2_recent(symbol: str = "WOLF"):
    """Recent resolved trades. Security (audit): defaults to WOLF-only so the
    public investor view never leaks the full multi-symbol history. Pass
    ?symbol=ALL for the unfiltered cross-symbol listing (internal use)."""
    _all = str(symbol).strip().upper() in ("ALL", "*", "")
    with db_conn() as conn:
        cur = conn.cursor()
        if _all:
            cur.execute("""SELECT id,symbol,direction,confidence,entry_price,exit_price,pnl_pct,outcome,predicted_at,expires_at,asset_type
                FROM predictions WHERE outcome IS NOT NULL AND predicted_at IS NOT NULL
                  AND """
                + REAL_TRADE_WHERE
                + """
                ORDER BY expires_at DESC NULLS LAST LIMIT 50""")
        else:
            cur.execute("""SELECT id,symbol,direction,confidence,entry_price,exit_price,pnl_pct,outcome,predicted_at,expires_at,asset_type
                FROM predictions WHERE outcome IS NOT NULL AND predicted_at IS NOT NULL AND symbol=%s
                  AND """
                + REAL_TRADE_WHERE
                + """
                ORDER BY expires_at DESC NULLS LAST LIMIT 50""", (symbol.strip().upper(),))
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
        return compute_stats_by_direction(conn.cursor())


@portfolio_router.post("/api/admin/expire-picks", include_in_schema=False)
def force_expire_picks(x_cron_secret: str = Header(default="")):
    """Force-expire all open picks so fresh ones can be generated."""
    import os, time as _time
    if x_cron_secret != os.getenv("CRON_SECRET",""):
        return JSONResponse({"ok":False,"error":"Forbidden"}, status_code=403)
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE predictions SET outcome='EXPIRED', resolved_at=%s WHERE outcome IS NULL",
                (int(_time.time()),)
            )
            expired = cur.rowcount
        return {"ok": True, "expired": expired}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


def auto_refresh_portfolio_prices():
    """T19: Refresh all stock portfolio positions with latest price from yfinance."""
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, symbol, asset_type FROM user_portfolio")
            positions = cur.fetchall()
        updated = 0
        for pid, symbol, asset_type in positions:
            if (asset_type or "stock").lower() != "stock":
                continue  # WOLF-only: only refresh stocks; ignore non-stock rows
            try:
                from core.prices import get_stock_price
                latest = get_stock_price(symbol)
                if latest and latest > 0:
                    with db_conn() as conn:
                        conn.cursor().execute(
                            "UPDATE user_portfolio SET manual_price=%s WHERE id=%s",
                            (latest, pid)
                        )
                    updated += 1
            except Exception as _se:
                pass  # silent — stale price better than crash
        return updated
    except Exception as _e:
        return 0


@portfolio_router.post("/api/portfolio/refresh-prices")
def refresh_portfolio_prices(request: Request):
    """T19: Manually trigger portfolio price refresh."""
    from mcp.security import require_portfolio_auth
    require_portfolio_auth(request)
    updated = auto_refresh_portfolio_prices()
    return {"ok": True, "updated": updated}
