"""
api/wolf_endpoints.py — WOLF command center API
================================================
All endpoints mounted under /api/wolf/* and consumed by cockpit.html.

Endpoints
  GET /api/wolf/context          — existing WolfContext payload (unchanged)
  GET /api/wolf/price            — real-time price + change + market status
  GET /api/wolf/price-history    — OHLC bars for 1d/1w/1m ranges
  GET /api/wolf/predictions      — historical picks for the prediction-vs-reality chart
  GET /api/wolf/stats            — open/high/low/volume + market cap / PE / EPS / 52w / earnings date
  GET /api/wolf/earnings         — quarterly EPS estimate vs actual + revenue
  GET /api/wolf/analyst          — price targets + recommendation distribution + latest rating
  GET /api/wolf/news             — WOLF-relevant news with category tags

All endpoints follow the graceful-degradation pattern: on any failure they
return HTTP 200 with `{"ok": False, "error": "..."}` plus an empty-default
payload, so the frontend can render placeholders instead of crashing.

Caching is in-process with per-endpoint TTLs. Process restart clears cache.
"""

from __future__ import annotations

import time
import math
import logging
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter
from fastapi.responses import JSONResponse

LOGGER = logging.getLogger("ghost.wolf_endpoints")
router = APIRouter(prefix="/api/wolf", tags=["wolf"])

WOLF_SYMBOL = "WOLF"


# ────────────────────────────────────────────────────────────────
# TTL cache (single in-process dict, keyed by endpoint name)
# ────────────────────────────────────────────────────────────────
_CACHE: Dict[str, Tuple[float, Any]] = {}


def _cache_get(key: str, ttl_s: float):
    hit = _CACHE.get(key)
    if not hit:
        return None
    ts, payload = hit
    if (time.time() - ts) < ttl_s:
        return payload
    return None


def _cache_set(key: str, payload: Any) -> None:
    _CACHE[key] = (time.time(), payload)


def _ok(payload: dict) -> dict:
    out = {"ok": True}
    out.update(payload)
    return out


def _err(msg: str, **extra) -> dict:
    out = {"ok": False, "error": (msg or "")[:200]}
    out.update(extra)
    return out


# ────────────────────────────────────────────────────────────────
# Market status helper (US equities: Mon-Fri 9:30-16:00 ET)
# ────────────────────────────────────────────────────────────────
def _market_status() -> str:
    import datetime as _dt
    try:
        from zoneinfo import ZoneInfo
        now_et = _dt.datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        # Fallback: assume UTC-5 (EST), close enough for status badge
        now_et = _dt.datetime.utcnow() - _dt.timedelta(hours=5)
    if now_et.weekday() >= 5:
        return "Market Closed"
    hm = now_et.hour * 60 + now_et.minute
    if 9 * 60 + 30 <= hm < 16 * 60:
        return "Market Open"
    if 16 * 60 <= hm < 20 * 60:
        return "After Hours"
    if 4 * 60 <= hm < 9 * 60 + 30:
        return "Pre-Market"
    return "Market Closed"


# ────────────────────────────────────────────────────────────────
# /api/wolf/context — preserved from prior implementation
# ────────────────────────────────────────────────────────────────
@router.get("/context")
async def get_wolf_context_endpoint(direction: str = "UP"):
    """Latest WolfContext for the WOLF Intel panel (15-min cache inside module)."""
    try:
        from core.wolf_context import get_wolf_context
        ctx = get_wolf_context(direction=direction.upper())

        def _to_dict(obj):
            if hasattr(obj, "__dataclass_fields__"):
                return {k: _to_dict(v) for k, v in asdict(obj).items()}
            if isinstance(obj, list):
                return [_to_dict(i) for i in obj]
            return obj

        payload = _to_dict(ctx)
        payload["ok"] = True
        return JSONResponse(content=payload)
    except Exception as exc:
        return JSONResponse(status_code=200, content=_err(str(exc)))


# ────────────────────────────────────────────────────────────────
# /api/wolf/price — real-time price + change + market status
# ────────────────────────────────────────────────────────────────
@router.get("/price")
async def get_wolf_price():
    """Live WOLF price, day change, after-hours quote, and market-status badge.

    Source: core/prices.get_stock_price (Alpaca → yfinance fallback) for the
    spot price; yfinance fast_info for previous_close + post-market price.
    """
    cached = _cache_get("price", 60)
    if cached:
        return JSONResponse(content=cached)

    try:
        from core.prices import get_stock_price
        spot = get_stock_price(WOLF_SYMBOL)
    except Exception as e:
        spot = None
        LOGGER.warning("price fetch failed: " + str(e)[:120])

    prev_close = None
    after_hours = None
    after_hours_change = None
    try:
        import yfinance as yf
        tk = yf.Ticker(WOLF_SYMBOL)
        fi = tk.fast_info
        prev_close = getattr(fi, "previous_close", None) or getattr(fi, "previousClose", None)
        post = getattr(fi, "post_market_price", None) or getattr(fi, "postMarketPrice", None)
        if post and prev_close and float(post) > 0:
            after_hours = float(post)
            spot_for_diff = float(spot) if spot else float(prev_close)
            after_hours_change = round(after_hours - spot_for_diff, 4)
    except Exception:
        pass

    if not spot:
        payload = _err("Price feed unavailable", symbol=WOLF_SYMBOL, price=None,
                       change=None, change_pct=None, after_hours=None,
                       after_hours_change=None, market_status=_market_status(),
                       ts=int(time.time()))
        return JSONResponse(content=payload)

    spot = float(spot)
    change = None
    change_pct = None
    if prev_close and float(prev_close) > 0:
        change = round(spot - float(prev_close), 4)
        change_pct = round((spot - float(prev_close)) / float(prev_close) * 100, 2)

    payload = _ok({
        "symbol": WOLF_SYMBOL,
        "price": round(spot, 4),
        "previous_close": round(float(prev_close), 4) if prev_close else None,
        "change": change,
        "change_pct": change_pct,
        "after_hours": round(after_hours, 4) if after_hours is not None else None,
        "after_hours_change": after_hours_change,
        "market_status": _market_status(),
        "ts": int(time.time()),
    })
    _cache_set("price", payload)
    return JSONResponse(content=payload)


# ────────────────────────────────────────────────────────────────
# /api/wolf/price-history — OHLC bars for chart overlay
# ────────────────────────────────────────────────────────────────
_HISTORY_PARAMS = {
    "1d": ("1d", "5m"),
    "1w": ("5d", "30m"),
    "1m": ("1mo", "1d"),
}


@router.get("/price-history")
async def get_wolf_price_history(range: str = "1d"):
    """Time-series WOLF price for the prediction-vs-reality chart overlay.

    Ranges:
      1d → 5-minute bars (intraday)
      1w → 30-minute bars (5 trading days)
      1m → daily bars
    """
    rng = (range or "1d").lower()
    if rng not in _HISTORY_PARAMS:
        rng = "1d"
    cache_key = "price-history:" + rng
    ttl = 60 if rng == "1d" else 300
    cached = _cache_get(cache_key, ttl)
    if cached:
        return JSONResponse(content=cached)

    period, interval = _HISTORY_PARAMS[rng]
    points: List[Dict[str, float]] = []
    err = None
    try:
        import yfinance as yf
        tk = yf.Ticker(WOLF_SYMBOL)
        h = tk.history(period=period, interval=interval)
        if not h.empty:
            for ix, row in h.iterrows():
                try:
                    ts = int(ix.timestamp())
                    price = float(row["Close"])
                    if math.isnan(price):
                        continue
                    points.append({"ts": ts, "price": round(price, 4)})
                except Exception:
                    continue
    except Exception as e:
        err = str(e)[:200]

    payload = (_err(err, range=rng, points=points) if (err and not points)
               else _ok({"range": rng, "points": points}))
    _cache_set(cache_key, payload)
    return JSONResponse(content=payload)


# ────────────────────────────────────────────────────────────────
# /api/wolf/predictions — historical picks for chart overlay
# ────────────────────────────────────────────────────────────────
@router.get("/predictions")
async def get_wolf_predictions(days: int = 30, limit: int = 100):
    """Past WOLF picks for the prediction-vs-reality chart.

    Each row is one prediction with its target/stop band, direction, and
    realised outcome (or NULL if still open). Frontend overlays the band on
    the live price line and colours by outcome once resolved.
    """
    cached = _cache_get("predictions", 60)
    if cached:
        return JSONResponse(content=cached)

    try:
        from core.db import db_conn
        days = max(1, min(int(days or 30), 365))
        limit = max(1, min(int(limit or 100), 500))
        cutoff = int(time.time()) - days * 86400
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, predicted_at, expires_at, resolved_at, direction, confidence,
                       entry_price, target_price, stop_price, outcome, pnl_pct
                FROM predictions
                WHERE symbol = %s
                  AND predicted_at IS NOT NULL
                  AND predicted_at >= %s
                ORDER BY predicted_at DESC
                LIMIT %s
                """,
                (WOLF_SYMBOL, cutoff, limit),
            )
            rows = cur.fetchall()
        preds = []
        for r in rows:
            try:
                direction = r[4]
                entry = float(r[6]) if r[6] is not None else None
                target = float(r[7]) if r[7] is not None else None
                stop = float(r[8]) if r[8] is not None else None
                # buy_target/sell_target = the actionable price labels for chart bands.
                # BUY pick: buy_target = entry (where to go long), sell_target = target (take profit).
                # SELL pick: buy_target = target (where to cover), sell_target = entry (where to short).
                if direction in ("UP", "BUY"):
                    buy_target, sell_target = entry, target
                elif direction in ("DOWN", "SELL"):
                    buy_target, sell_target = target, entry
                else:
                    buy_target = sell_target = None
                preds.append({
                    "id": int(r[0]),
                    "predicted_at": int(r[1]) if r[1] is not None else None,
                    "expires_at": int(r[2]) if r[2] is not None else None,
                    "resolved_at": int(r[3]) if r[3] is not None else None,
                    "direction": direction,
                    "confidence": float(r[5]) if r[5] is not None else None,
                    "entry_price": entry,
                    "target_price": target,
                    "stop_price": stop,
                    "buy_target": buy_target,
                    "sell_target": sell_target,
                    "outcome": r[9],
                    "pnl_pct": float(r[10]) if r[10] is not None else None,
                })
            except Exception:
                continue
        payload = _ok({"symbol": WOLF_SYMBOL, "days": days, "predictions": preds})
    except Exception as e:
        payload = _err(str(e), symbol=WOLF_SYMBOL, predictions=[])

    _cache_set("predictions", payload)
    return JSONResponse(content=payload)


# ────────────────────────────────────────────────────────────────
# /api/wolf/stats — Yahoo Finance key stats grid
# ────────────────────────────────────────────────────────────────
@router.get("/stats")
async def get_wolf_stats():
    """Open/High/Low/Volume + Market Cap/PE/EPS/52w range/Earnings date.

    Pulls from yfinance .info + .fast_info. Earnings date falls back to
    Finviz (already scraped by core/wolf_context.py for the WOLF Intel tab).
    """
    cached = _cache_get("stats", 300)
    if cached:
        return JSONResponse(content=cached)

    out = {
        "symbol": WOLF_SYMBOL,
        "open": None, "high": None, "low": None,
        "volume": None, "avg_volume": None,
        "market_cap": None, "pe_ratio": None, "eps": None,
        "week52_low": None, "week52_high": None,
        "earnings_date": None,
    }
    err = None
    try:
        import yfinance as yf
        tk = yf.Ticker(WOLF_SYMBOL)
        fi = tk.fast_info
        out["open"] = _safe_float(getattr(fi, "open", None) or getattr(fi, "regular_market_open", None))
        out["high"] = _safe_float(getattr(fi, "day_high", None) or getattr(fi, "dayHigh", None))
        out["low"] = _safe_float(getattr(fi, "day_low", None) or getattr(fi, "dayLow", None))
        out["volume"] = _safe_int(getattr(fi, "last_volume", None) or getattr(fi, "lastVolume", None))
        out["market_cap"] = _safe_int(getattr(fi, "market_cap", None) or getattr(fi, "marketCap", None))
        out["week52_low"] = _safe_float(getattr(fi, "year_low", None) or getattr(fi, "yearLow", None))
        out["week52_high"] = _safe_float(getattr(fi, "year_high", None) or getattr(fi, "yearHigh", None))
        try:
            info = tk.info or {}
            out["avg_volume"] = _safe_int(info.get("averageVolume") or info.get("averageDailyVolume10Day"))
            out["pe_ratio"] = _safe_float(info.get("trailingPE") or info.get("forwardPE"))
            out["eps"] = _safe_float(info.get("trailingEps") or info.get("forwardEps"))
            ed = info.get("earningsTimestamp") or info.get("earningsTimestampStart")
            if ed:
                out["earnings_date"] = int(ed)
        except Exception:
            pass
    except Exception as e:
        err = str(e)[:200]

    if out["earnings_date"] is None:
        try:
            from core.wolf_context import get_wolf_context
            ctx = get_wolf_context(direction="UP")
            if ctx and ctx.earnings and ctx.earnings.date_str and ctx.earnings.date_str not in ("-", "N/A"):
                out["earnings_date_str"] = ctx.earnings.date_str
        except Exception:
            pass

    # Volume ratio + anomaly flag (post-fetch, doesn't fail the whole endpoint if missing)
    out["volume_ratio"] = None
    out["volume_alert"] = False
    try:
        if out["volume"] and out["avg_volume"] and out["avg_volume"] > 0:
            r = out["volume"] / out["avg_volume"]
            out["volume_ratio"] = round(r, 2)
            out["volume_alert"] = r >= 2.0
    except Exception:
        pass

    # Sector correlation — SiC / power semis peers
    out["sector_correlation"] = _fetch_sector_correlation()

    payload = _ok(out) if err is None else _err(err, **out)
    _cache_set("stats", payload)
    return JSONResponse(content=payload)


SECTOR_PEERS = ("ON", "NVTS", "AEHR", "POWI")


def _fetch_sector_correlation() -> Dict[str, Any]:
    """Day change % for SiC / power-semi peers + a divergence signal vs WOLF.

    Divergence signal fires when ≥ 2 peers are up > 1.5% and WOLF is < 0.5%
    (or vice versa). Cached separately for 5 min via the main /stats cache.
    """
    peers_out: List[Dict[str, Any]] = []
    wolf_chg = None
    try:
        import yfinance as yf
        for sym in (WOLF_SYMBOL,) + SECTOR_PEERS:
            try:
                tk = yf.Ticker(sym)
                fi = tk.fast_info
                cur = getattr(fi, "last_price", None) or getattr(fi, "lastPrice", None)
                prev = getattr(fi, "previous_close", None) or getattr(fi, "previousClose", None)
                chg = None
                if cur and prev and float(prev) > 0:
                    chg = round((float(cur) - float(prev)) / float(prev) * 100, 2)
                if sym == WOLF_SYMBOL:
                    wolf_chg = chg
                else:
                    peers_out.append({"symbol": sym, "change_pct": chg})
            except Exception:
                if sym != WOLF_SYMBOL:
                    peers_out.append({"symbol": sym, "change_pct": None})
    except Exception:
        pass

    signal = None
    if wolf_chg is not None:
        ups = sum(1 for p in peers_out if p["change_pct"] is not None and p["change_pct"] > 1.5)
        dns = sum(1 for p in peers_out if p["change_pct"] is not None and p["change_pct"] < -1.5)
        if ups >= 2 and wolf_chg < 0.5:
            signal = "wolf_lagging_up"
        elif dns >= 2 and wolf_chg > -0.5:
            signal = "wolf_holding_down"
    return {"wolf_change_pct": wolf_chg, "peers": peers_out, "signal": signal}


def _safe_float(v) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, 4)
    except Exception:
        return None


def _safe_int(v) -> Optional[int]:
    try:
        if v is None:
            return None
        return int(v)
    except Exception:
        return None


# ────────────────────────────────────────────────────────────────
# /api/wolf/earnings — quarterly EPS estimate vs actual + revenue
# ────────────────────────────────────────────────────────────────
@router.get("/earnings")
async def get_wolf_earnings():
    """Last 8 quarters of EPS (estimate, actual, beat) + revenue."""
    cached = _cache_get("earnings", 3600)
    if cached:
        return JSONResponse(content=cached)

    quarters: List[Dict[str, Any]] = []
    err = None
    try:
        import yfinance as yf
        tk = yf.Ticker(WOLF_SYMBOL)
        # EPS history (estimate + actual)
        try:
            eh = tk.earnings_history
            if eh is not None and not eh.empty:
                for ix, row in eh.iterrows():
                    try:
                        est = _safe_float(row.get("epsEstimate"))
                        act = _safe_float(row.get("epsActual"))
                        if est is None and act is None:
                            continue
                        beat = (act is not None and est is not None and act > est)
                        q_label = _quarter_label(ix)
                        quarters.append({
                            "quarter": q_label,
                            "ts": int(ix.timestamp()) if hasattr(ix, "timestamp") else None,
                            "estimate_eps": est,
                            "actual_eps": act,
                            "beat": bool(beat),
                            "revenue": None,
                            "earnings": None,
                        })
                    except Exception:
                        continue
        except Exception as _e:
            err = "earnings_history: " + str(_e)[:120]

        # Revenue + net income from income statement (quarterly)
        try:
            inc = tk.quarterly_income_stmt if hasattr(tk, "quarterly_income_stmt") else None
            if inc is None or (hasattr(inc, "empty") and inc.empty):
                inc = tk.quarterly_financials if hasattr(tk, "quarterly_financials") else None
            if inc is not None and hasattr(inc, "empty") and not inc.empty:
                cols = list(inc.columns)
                rev_row = None
                net_row = None
                for label in ("Total Revenue", "TotalRevenue", "Revenue"):
                    if label in inc.index:
                        rev_row = inc.loc[label]
                        break
                for label in ("Net Income", "NetIncome", "Net Income Common Stockholders"):
                    if label in inc.index:
                        net_row = inc.loc[label]
                        break
                rev_by_q = {}
                ern_by_q = {}
                for c in cols:
                    q_label = _quarter_label(c)
                    if rev_row is not None:
                        rev_by_q[q_label] = _safe_float(rev_row.get(c))
                    if net_row is not None:
                        ern_by_q[q_label] = _safe_float(net_row.get(c))
                # Merge into quarters list (by quarter label)
                seen = {q["quarter"]: q for q in quarters}
                for q_label, rev in rev_by_q.items():
                    if q_label in seen:
                        seen[q_label]["revenue"] = rev
                    else:
                        quarters.append({
                            "quarter": q_label, "ts": None,
                            "estimate_eps": None, "actual_eps": None, "beat": False,
                            "revenue": rev,
                            "earnings": ern_by_q.get(q_label),
                        })
                for q_label, ern in ern_by_q.items():
                    if q_label in seen and seen[q_label].get("earnings") is None:
                        seen[q_label]["earnings"] = ern
        except Exception as _e2:
            if not err:
                err = "income_stmt: " + str(_e2)[:120]

    except Exception as e:
        err = str(e)[:200]

    quarters.sort(key=lambda q: q.get("ts") or 0)
    quarters = quarters[-8:]

    payload = _ok({"symbol": WOLF_SYMBOL, "quarters": quarters}) if quarters else _err(
        err or "No earnings data", symbol=WOLF_SYMBOL, quarters=[]
    )
    _cache_set("earnings", payload)
    return JSONResponse(content=payload)


def _quarter_label(ts) -> str:
    """Format a timestamp/Index value as 'Q3 2025'."""
    try:
        if hasattr(ts, "to_pydatetime"):
            d = ts.to_pydatetime()
        elif hasattr(ts, "timestamp"):
            import datetime as _dt
            d = _dt.datetime.fromtimestamp(ts.timestamp())
        else:
            d = ts
        q = (d.month - 1) // 3 + 1
        return f"Q{q} {d.year}"
    except Exception:
        return str(ts)[:10]


# ────────────────────────────────────────────────────────────────
# /api/wolf/analyst — price targets + recommendation distribution
# ────────────────────────────────────────────────────────────────
@router.get("/analyst")
async def get_wolf_analyst():
    """Analyst price targets + recommendation distribution + latest rating."""
    cached = _cache_get("analyst", 3600)
    if cached:
        return JSONResponse(content=cached)

    out: Dict[str, Any] = {
        "symbol": WOLF_SYMBOL,
        "current_price": None,
        "price_target_low": None,
        "price_target_avg": None,
        "price_target_high": None,
        "analyst_count": None,
        "recommendations": {
            "strong_buy": 0, "buy": 0, "hold": 0, "underperform": 0, "sell": 0,
        },
        "latest_rating": None,
    }
    err = None
    try:
        import yfinance as yf
        tk = yf.Ticker(WOLF_SYMBOL)
        try:
            info = tk.info or {}
            out["current_price"] = _safe_float(info.get("currentPrice") or info.get("regularMarketPrice"))
            out["price_target_low"] = _safe_float(info.get("targetLowPrice"))
            out["price_target_avg"] = _safe_float(info.get("targetMeanPrice"))
            out["price_target_high"] = _safe_float(info.get("targetHighPrice"))
            out["analyst_count"] = _safe_int(info.get("numberOfAnalystOpinions"))
        except Exception as _e:
            err = "info: " + str(_e)[:120]

        # Recommendation distribution
        try:
            rec = tk.recommendations
            if rec is not None and hasattr(rec, "empty") and not rec.empty:
                # yfinance shape: columns 'period', 'strongBuy', 'buy', 'hold', 'sell', 'strongSell'
                latest = rec.iloc[0]
                out["recommendations"]["strong_buy"] = _safe_int(latest.get("strongBuy")) or 0
                out["recommendations"]["buy"] = _safe_int(latest.get("buy")) or 0
                out["recommendations"]["hold"] = _safe_int(latest.get("hold")) or 0
                out["recommendations"]["underperform"] = _safe_int(latest.get("sell")) or 0
                out["recommendations"]["sell"] = _safe_int(latest.get("strongSell")) or 0
        except Exception:
            pass

        # Latest upgrade/downgrade
        try:
            up = tk.upgrades_downgrades if hasattr(tk, "upgrades_downgrades") else None
            if up is not None and hasattr(up, "empty") and not up.empty:
                row = up.iloc[0]
                d = row.name if hasattr(row, "name") else None
                out["latest_rating"] = {
                    "date": int(d.timestamp()) if d is not None and hasattr(d, "timestamp") else None,
                    "analyst": str(row.get("Firm") or row.get("firm") or ""),
                    "rating": str(row.get("ToGrade") or row.get("toGrade") or ""),
                    "from_rating": str(row.get("FromGrade") or row.get("fromGrade") or ""),
                    "action": str(row.get("Action") or row.get("action") or ""),
                }
        except Exception:
            pass

    except Exception as e:
        err = str(e)[:200]

    has_targets = any([out["price_target_low"], out["price_target_avg"], out["price_target_high"]])
    rec_total = sum(out["recommendations"].values())
    if not has_targets and rec_total == 0:
        payload = _err(err or "No analyst data", **out)
    else:
        payload = _ok(out)
    _cache_set("analyst", payload)
    return JSONResponse(content=payload)


# ────────────────────────────────────────────────────────────────
# /api/wolf/news — WOLF-relevant news with category tags
# ────────────────────────────────────────────────────────────────
_EARNINGS_KEYWORDS = ("earnings", "eps", "quarterly", "q1 ", "q2 ", "q3 ", "q4 ",
                      "fiscal", "guidance", "outlook", "results")
_PRESS_KEYWORDS = ("press release", "announces", "announce", "appoints", "launches",
                   "introduces", "expands", "partnership", "contract")


@router.get("/news")
async def get_wolf_news(category: str = "all"):
    """WOLF-relevant news, optionally filtered by category.

    Categories: all | news | earnings | press
    """
    cached_key = "news:" + (category or "all").lower()
    cached = _cache_get(cached_key, 300)
    if cached:
        return JSONResponse(content=cached)

    cat = (category or "all").lower()
    if cat not in ("all", "news", "earnings", "press"):
        cat = "all"

    articles_out: List[Dict[str, Any]] = []
    err = None
    try:
        from core.news import get_recent_articles
        raw = get_recent_articles(50) or []
    except Exception as e:
        raw = []
        err = str(e)[:200]

    for a in raw:
        try:
            title = a.get("title") or a.get("headline") or ""
            syms = [str(s).upper() for s in (a.get("symbols") or [])]
            # Keep WOLF-mentioned items or generic market news
            wolf_match = (WOLF_SYMBOL in syms) or ("WOLFSPEED" in title.upper()) or (WOLF_SYMBOL in title.upper().split())
            if not wolf_match and syms and WOLF_SYMBOL not in syms:
                # Has symbol tags but none are WOLF → skip
                continue
            article_cat = _categorize(title)
            if cat != "all" and article_cat != cat:
                continue
            articles_out.append({
                "title": title,
                "source": a.get("source") or "News",
                "url": a.get("url") or a.get("link") or "",
                "published_at": _safe_int(a.get("published_at") or a.get("ts") or a.get("publishedAt")),
                "category": article_cat,
                "sentiment": _safe_float(a.get("sentiment")),
                "symbols": syms,
            })
        except Exception:
            continue

    # Augment with yfinance news (often catches press releases the cron misses)
    try:
        import yfinance as yf
        tk = yf.Ticker(WOLF_SYMBOL)
        for item in (tk.news or [])[:15]:
            try:
                title = item.get("title") or ""
                if not title:
                    continue
                article_cat = _categorize(title)
                if cat != "all" and article_cat != cat:
                    continue
                articles_out.append({
                    "title": title,
                    "source": item.get("publisher") or "Yahoo Finance",
                    "url": item.get("link") or "",
                    "published_at": _safe_int(item.get("providerPublishTime")),
                    "category": article_cat,
                    "sentiment": None,
                    "symbols": [WOLF_SYMBOL],
                })
            except Exception:
                continue
    except Exception:
        pass

    # Dedup by title (case-insensitive), keep first
    seen_titles = set()
    deduped = []
    for a in articles_out:
        key = (a["title"] or "").strip().lower()
        if not key or key in seen_titles:
            continue
        seen_titles.add(key)
        deduped.append(a)

    deduped.sort(key=lambda a: a.get("published_at") or 0, reverse=True)
    deduped = deduped[:30]

    payload = (_ok({"category": cat, "articles": deduped}) if deduped
               else _err(err or "No articles in window", category=cat, articles=[]))
    _cache_set(cached_key, payload)
    return JSONResponse(content=payload)


def _categorize(title: str) -> str:
    t = (title or "").lower()
    if any(k in t for k in _EARNINGS_KEYWORDS):
        return "earnings"
    if any(k in t for k in _PRESS_KEYWORDS):
        return "press"
    return "news"
