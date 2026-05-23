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

    # PR #25 fallback: if yfinance returned nothing for the key fundamentals,
    # try Polygon (we already have POLYGON_API_KEY set). Polygon's
    # /v3/reference/tickers/{sym} returns market_cap, total_employees, etc;
    # its /v2/aggs/.../prev gives day OHLC + volume.
    _polygon_filled = _try_polygon_stats_fallback(out)

    # PR #25: short interest from yfinance.info (item 5). Surfaces in the
    # cockpit Short Interest tile via /api/wolf/context, but having it on
    # /api/wolf/stats too gives the cockpit a redundant path.
    out["short_float"] = None
    out["short_days_to_cover"] = None
    try:
        import yfinance as yf
        info = yf.Ticker(WOLF_SYMBOL).info or {}
        sf = info.get("shortPercentOfFloat")
        if sf is not None:
            out["short_float"] = _safe_float(sf)  # already 0..1
        dtc = info.get("shortRatio")  # days-to-cover from yfinance
        if dtc is not None:
            out["short_days_to_cover"] = _safe_float(dtc)
    except Exception:
        pass

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

    # PR #23: never leak raw yfinance exception strings (they can contain
    # internal JSON key names like 'currentTradingPeriod' that show up
    # verbatim in the cockpit). Use a generic friendly message.
    if err is not None:
        err = "Stats unavailable"
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


def _try_polygon_stats_fallback(out: dict) -> bool:
    """PR #25 (item 4): when yfinance returns nothing, populate market_cap +
    volume + OHLC fields from Polygon. We already have POLYGON_API_KEY
    configured (PR #12 OHLCV fallback uses it).

    Mutates `out` in place. Returns True if at least one field was filled.
    """
    import os as _os
    api_key = _os.getenv("POLYGON_API_KEY", "")
    if not api_key:
        return False
    filled = False
    try:
        import requests as _req
        sym = WOLF_SYMBOL.upper()
        # Ticker reference (market cap, name, etc)
        if out.get("market_cap") is None:
            try:
                r = _req.get(
                    f"https://api.polygon.io/v3/reference/tickers/{sym}?apiKey={api_key}",
                    timeout=15,
                )
                if r.status_code == 200:
                    res = (r.json() or {}).get("results") or {}
                    mc = res.get("market_cap")
                    if mc:
                        out["market_cap"] = _safe_int(mc)
                        filled = True
            except Exception as _e:
                LOGGER.info(f"Polygon ref {sym}: {str(_e)[:80]}")
        # Previous day's OHLC + volume
        if any(out.get(k) is None for k in ("open", "high", "low", "volume")):
            try:
                r = _req.get(
                    f"https://api.polygon.io/v2/aggs/ticker/{sym}/prev?adjusted=true&apiKey={api_key}",
                    timeout=15,
                )
                if r.status_code == 200:
                    results = (r.json() or {}).get("results") or []
                    if results:
                        bar = results[0]
                        if out.get("open") is None:
                            out["open"] = _safe_float(bar.get("o"))
                            filled = True
                        if out.get("high") is None:
                            out["high"] = _safe_float(bar.get("h"))
                            filled = True
                        if out.get("low") is None:
                            out["low"] = _safe_float(bar.get("l"))
                            filled = True
                        if out.get("volume") is None:
                            out["volume"] = _safe_int(bar.get("v"))
                            filled = True
            except Exception as _e:
                LOGGER.info(f"Polygon prev {sym}: {str(_e)[:80]}")
        # 52-week range — derive from the last 365 daily bars
        if out.get("week52_low") is None or out.get("week52_high") is None:
            try:
                from datetime import datetime as _dt, timedelta as _td, timezone as _tz
                end = _dt.now(_tz.utc).date()
                start = end - _td(days=365)
                r = _req.get(
                    f"https://api.polygon.io/v2/aggs/ticker/{sym}/range/1/day/"
                    f"{start.isoformat()}/{end.isoformat()}"
                    f"?adjusted=true&sort=asc&limit=5000&apiKey={api_key}",
                    timeout=20,
                )
                if r.status_code == 200:
                    bars = (r.json() or {}).get("results") or []
                    closes = [b.get("c") for b in bars if b.get("c")]
                    highs = [b.get("h") for b in bars if b.get("h")]
                    lows = [b.get("l") for b in bars if b.get("l")]
                    if highs and out.get("week52_high") is None:
                        out["week52_high"] = _safe_float(max(highs))
                        filled = True
                    if lows and out.get("week52_low") is None:
                        out["week52_low"] = _safe_float(min(lows))
                        filled = True
                    # Volume — average over the window if avg_volume missing
                    if out.get("avg_volume") is None and bars:
                        vols = [b.get("v") for b in bars[-20:] if b.get("v")]
                        if vols:
                            out["avg_volume"] = _safe_int(sum(vols) / len(vols))
                            filled = True
            except Exception as _e:
                LOGGER.info(f"Polygon range {sym}: {str(_e)[:80]}")
        if filled:
            LOGGER.info(f"Polygon stats fallback {sym}: populated {[k for k in out if out.get(k) is not None]}")
    except Exception as e:
        LOGGER.warning(f"Polygon stats fallback {sym}: {str(e)[:120]}")
    return filled


def _safe_float(v) -> Optional[float]:
    # PR #23: explicitly reject dict / list — newer yfinance returns nested
    # objects (e.g. {"raw": 70.5, "fmt": "70.50"}) for some fields, which
    # would coerce to garbage via float() or leak the dict downstream.
    if v is None or isinstance(v, (dict, list, tuple, set)):
        return None
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, 4)
    except Exception:
        return None


def _safe_int(v) -> Optional[int]:
    if v is None or isinstance(v, (dict, list, tuple, set)):
        return None
    try:
        return int(v)
    except Exception:
        return None


def _scrub_payload_dict(d: dict, allowed_keys: set) -> dict:
    """Return a copy of d keeping ONLY allowed_keys and scalar values.

    Defensive scrub so unexpected yfinance nested objects (currentTradingPeriod
    etc.) can never leak as raw JSON keys into the UI. Any value that isn't
    str/int/float/bool/None is dropped.
    """
    out = {}
    for k in allowed_keys:
        v = d.get(k)
        if v is None or isinstance(v, (str, int, float, bool)):
            out[k] = v
        else:
            out[k] = None
    return out


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
    # PR #23: clean error string — never leak raw yfinance exception text
    # (e.g. JSON parse errors like "Expecting value: line 1 column 1 (char 0)").
    if not has_targets and rec_total == 0:
        clean_err = "Analyst data unavailable"
        payload = _err(clean_err, **out)
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
            # PR #26: REQUIRE a textual WOLF mention. The previous symbols-tag
            # shortcut (WOLF in syms) was the leak source — core/news fetches
            # Finnhub *company-news for WOLF* and tags EVERY returned article
            # with ["WOLF"], including market-roundup pieces that are actually
            # about IBM / Zoom / Ross Stores / Ralph Lauren. The tag is
            # therefore unreliable; only trust the article TEXT.
            title_upper = title.upper()
            body_text = (a.get("summary") or a.get("description") or "")
            blob_upper = (title + " " + body_text).upper()
            blob_words = set(blob_upper.replace(",", " ").replace(".", " ")
                             .replace(":", " ").replace(";", " ")
                             .replace("(", " ").replace(")", " ").split())
            wolf_match = (
                ("WOLFSPEED" in blob_upper)
                or (WOLF_SYMBOL in blob_words)
                or ("SIC" in blob_words)
                or ("SILICON CARBIDE" in blob_upper)
            )
            if not wolf_match:
                continue
            article_cat = _categorize(title)
            if cat != "all" and article_cat != cat:
                continue
            # PR #25: aggressively extract real publisher name — articles can
            # carry it under 'source', 'publisher', 'source_name', or 'name'.
            # Strip internal labels (finnhub*, gnews*, generic 'News').
            raw_source = (
                a.get("publisher")
                or a.get("source_name")
                or a.get("name")
                or a.get("source")
                or "News"
            )
            rs_lower = str(raw_source).lower()
            # Strip internal-only labels. If we have a URL, derive publisher
            # from hostname; otherwise fall back to the generic "News" placeholder.
            if rs_lower.startswith("finnhub") or rs_lower.startswith("gnews"):
                url = a.get("url") or a.get("link") or ""
                host = None
                if url:
                    try:
                        from urllib.parse import urlparse
                        host = urlparse(url).netloc.replace("www.", "")
                    except Exception:
                        host = None
                raw_source = host if host else "News"
            source = raw_source if raw_source else "News"
            articles_out.append({
                "title": title,
                "source": source,
                "url": a.get("url") or a.get("link") or "",
                "published_at": _safe_int(a.get("published_at") or a.get("ts") or a.get("publishedAt")),
                "category": article_cat,
                "sentiment": _safe_float(a.get("sentiment")),
                "symbols": syms,
            })
        except Exception:
            continue

    # Augment with yfinance news. PR #25: this used to bypass the WOLF
    # filter entirely — Yahoo's news endpoint returns related-ticker noise
    # (Zoom / IBM / Ross Stores / Ralph Lauren) tagged to a target ticker,
    # which leaked straight into the investor feed. Now: same word-boundary
    # check the main loop uses, applied to title + (yfinance summary if any).
    try:
        import yfinance as yf
        tk = yf.Ticker(WOLF_SYMBOL)
        for item in (tk.news or [])[:15]:
            try:
                title = item.get("title") or ""
                summary = item.get("summary") or ""
                if not title:
                    continue
                blob_upper = (title + " " + summary).upper()
                blob_words = set(blob_upper.replace(",", " ").replace(".", " ")
                                 .replace(":", " ").replace(";", " ")
                                 .replace("(", " ").replace(")", " ").split())
                wolf_match = (
                    "WOLFSPEED" in blob_upper
                    or WOLF_SYMBOL in blob_words
                    or "SIC" in blob_words            # silicon carbide ticker tag
                    or "SILICON CARBIDE" in blob_upper
                )
                if not wolf_match:
                    continue
                article_cat = _categorize(title)
                if cat != "all" and article_cat != cat:
                    continue
                # PR #25: use yfinance's `publisher` field as the real source
                # (e.g. "Seeking Alpha", "Benzinga", "Business Wire"); fall back
                # to "Yahoo Finance" not the generic "News" placeholder.
                publisher = item.get("publisher") or "Yahoo Finance"
                articles_out.append({
                    "title": title,
                    "source": publisher,
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


def _squeeze_risk_tag(short_float_pct, days_to_cover) -> str:
    """low/medium/high/extreme from short %-of-float and days-to-cover
    (mirrors core.wolf_context._build_short_data thresholds)."""
    sfp = short_float_pct or 0
    dtc = days_to_cover or 0
    if sfp >= 35 or dtc >= 5:
        return "extreme"
    if sfp >= 25 or dtc >= 3:
        return "high"
    if sfp >= 15 or dtc >= 2:
        return "medium"
    return "low"


def _short_trend(shares_short, prior):
    """Month-over-month short-interest trend, or None if either side is missing."""
    if shares_short is None or not prior:
        return None
    delta = shares_short - prior
    return {
        "delta": delta,
        "pct": round(delta / prior * 100, 1) if prior else None,
        "direction": "rising" if delta > 0 else "falling" if delta < 0 else "flat",
    }


@router.get("/short-interest")
async def get_wolf_short_interest():
    """Short interest + squeeze context (audit free-API wiring). Best-effort via
    yfinance .info: short % of float, days-to-cover (shortRatio), shares short and
    the prior-month trend, plus a low/medium/high/extreme squeeze-risk tag. Cached
    1h. `available` is False (and the cockpit hides the tile) when the feed has no
    short data."""
    cached = _cache_get("short-interest", 3600)
    if cached:
        return JSONResponse(content=cached)

    short_float_pct = days_to_cover = shares_short = shares_short_prior = None
    err = None
    try:
        import yfinance as yf
        info = yf.Ticker(WOLF_SYMBOL).info or {}
        sf = _safe_float(info.get("shortPercentOfFloat"))  # yfinance returns 0..1
        if sf is not None:
            short_float_pct = round(sf * 100, 2)
        days_to_cover = _safe_float(info.get("shortRatio"))
        shares_short = _safe_int(info.get("sharesShort"))
        shares_short_prior = _safe_int(info.get("sharesShortPriorMonth"))
    except Exception as e:
        err = str(e)[:200]

    risk = _squeeze_risk_tag(short_float_pct, days_to_cover)
    trend = _short_trend(shares_short, shares_short_prior)
    available = any(v is not None for v in (short_float_pct, days_to_cover, shares_short))
    payload = _ok({
        "symbol": WOLF_SYMBOL,
        "available": available,
        "short_float_pct": short_float_pct,
        "days_to_cover": days_to_cover,
        "shares_short": shares_short,
        "shares_short_prior_month": shares_short_prior,
        "trend": trend,
        "squeeze_risk": risk if available else None,
        "error": err,
    })
    _cache_set("short-interest", payload)
    return JSONResponse(content=payload)


# ────────────────────────────────────────────────────────────────
# /api/wolf/ghost-score — composite intelligence rating
# ────────────────────────────────────────────────────────────────
#
# Score = sum of five weighted components, each in [0, weight], total [0, 100].
# Higher = more bullish on WOLF.
#
#   Component         Weight   Computation
#   ───────────────   ──────   ─────────────────────────────────────────────
#   model_confidence    40     BUY pick → confidence*40
#                              SELL pick → (1-confidence)*40
#                              No pick → 20 (neutral midpoint)
#   volume_signal       20     min(20, volume_ratio * 10)
#                              At 2x avg volume (alert threshold) → 20
#                              At 1x → 10. No data → 10.
#   sector_alignment    15     'wolf_lagging_up' (peers up, WOLF flat) → 15
#                              'wolf_holding_down' (peers down, WOLF holds) → 12
#                              else → 7.5 (neutral)
#   price_momentum      15     (current - 5d_SMA) / 5d_SMA mapped:
#                              ≥+3% → 15, ≥+1% → 12, ±1% → 7.5,
#                              <-1% → 3, <-3% → 0. No data → 7.5.
#   freshness           10     Hours since latest predicted_at:
#                              0-2h → 10, 2-6h → 8, 6-12h → 6,
#                              12-24h → 4, 24-48h → 2, >48h → 0.
#
# Signal label (from spec):
#   80-100 STRONG_BUY · 60-79 BUY · 40-59 HOLD · 20-39 SELL · 0-19 STRONG_SELL
#
# The formula is transparent on purpose — weights are product-owner choices,
# not derived empirically. Every input is real (no mocks), but the user
# should treat the score as a summary indicator, not a validated signal.

_GHOST_WEIGHTS = {
    "model_confidence": 40,
    "volume_signal": 20,
    "sector_alignment": 15,
    "price_momentum": 15,
    "freshness": 10,
}


def _score_model(latest_pick: Optional[dict]) -> float:
    if not latest_pick:
        return 20.0  # neutral midpoint of [0, 40]
    conf = float(latest_pick.get("confidence") or 0.0)
    direction = (latest_pick.get("direction") or "").upper()
    if direction in ("UP", "BUY"):
        return round(conf * 40, 2)
    if direction in ("DOWN", "SELL"):
        return round((1 - conf) * 40, 2)
    return 20.0


def _score_volume(volume_ratio: Optional[float]) -> float:
    if volume_ratio is None:
        return 10.0
    return round(min(20.0, max(0.0, float(volume_ratio) * 10)), 2)


def _score_sector(sector: Optional[dict]) -> float:
    if not sector:
        return 7.5
    sig = sector.get("signal")
    if sig == "wolf_lagging_up":
        return 15.0
    if sig == "wolf_holding_down":
        return 12.0
    return 7.5


def _score_momentum(current: Optional[float], sma_5d: Optional[float]) -> float:
    if current is None or sma_5d is None or sma_5d <= 0:
        return 7.5
    delta = (current - sma_5d) / sma_5d
    if delta >= 0.03:
        return 15.0
    if delta >= 0.01:
        return 12.0
    if delta >= -0.01:
        return 7.5
    if delta >= -0.03:
        return 3.0
    return 0.0


def _score_freshness(activity_ts: Optional[int], now_ts: int) -> float:
    # activity_ts = most recent ENGINE ACTIVITY (last scan cycle), falling back
    # to the last pick. Keyed to scan, not pick: a selective engine is silent
    # most of the time by design, so scoring "hours since last pick" wrongly
    # zeroed freshness during long, healthy WATCHING stretches.
    if not activity_ts:
        return 0.0
    age_h = max(0.0, (now_ts - int(activity_ts)) / 3600.0)
    if age_h <= 2:
        return 10.0
    if age_h <= 6:
        return 8.0
    if age_h <= 12:
        return 6.0
    if age_h <= 24:
        return 4.0
    if age_h <= 48:
        return 2.0
    return 0.0


def _signal_label(score: float) -> str:
    if score >= 80:
        return "STRONG_BUY"
    if score >= 60:
        return "BUY"
    if score >= 40:
        return "HOLD"
    if score >= 20:
        return "SELL"
    return "STRONG_SELL"


def compute_ghost_score(latest_pick, volume_ratio, sector, current_price, sma_5d, now_ts,
                        last_scan_ts=None, regime=None):
    """Pure scoring function — all I/O lifted to the caller for testability.

    `regime` (audit §3) is the rule-based market-regime tag; its modifier scales
    the raw component sum so the score is downgraded in bearish regimes and
    modestly boosted in confirmed uptrends. raw_score is the pre-modifier value."""
    # Freshness reflects engine activity (last scan), falling back to last pick.
    activity_ts = last_scan_ts or (latest_pick.get("predicted_at") if latest_pick else None)
    components = {
        "model": _score_model(latest_pick),
        "volume": _score_volume(volume_ratio),
        "sector": _score_sector(sector),
        "momentum": _score_momentum(current_price, sma_5d),
        "freshness": _score_freshness(activity_ts, now_ts),
    }
    raw = max(0.0, min(100.0, sum(components.values())))
    modifier = float((regime or {}).get("modifier", 1.0))
    score = max(0.0, min(100.0, raw * modifier))
    return {
        "score": round(score, 1),
        "raw_score": round(raw, 1),
        "signal": _signal_label(score),
        "components": {k: round(v, 2) for k, v in components.items()},
        "weights": dict(_GHOST_WEIGHTS),
        "regime": regime,
    }


@router.get("/ghost-score")
async def get_ghost_score():
    """Composite 0-100 score reflecting overall WOLF bullishness.

    Cached for 60s. All inputs are real data: latest pick from the
    predictions table, volume ratio from yfinance, sector correlation
    from the SiC peers helper, and 5-day SMA from the price history.

    Frontend renders this as a gauge at the top of the cockpit.
    """
    cached = _cache_get("ghost-score", 60)
    if cached:
        return JSONResponse(content=cached)

    latest_pick = None
    volume_ratio = None
    current_price = None
    sma_5d = None
    sector = None
    errors: List[str] = []

    # 1. Latest WOLF pick (any status, ordered by predicted_at)
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, predicted_at, direction, confidence
                FROM predictions
                WHERE symbol = %s AND predicted_at IS NOT NULL
                ORDER BY predicted_at DESC
                LIMIT 1
                """,
                (WOLF_SYMBOL,),
            )
            row = cur.fetchone()
            if row:
                latest_pick = {
                    "id": int(row[0]),
                    "predicted_at": int(row[1]) if row[1] else None,
                    "direction": row[2],
                    "confidence": float(row[3]) if row[3] is not None else None,
                }
    except Exception as e:
        errors.append("latest_pick: " + str(e)[:80])

    # 2. Volume ratio + current price (yfinance fast_info)
    try:
        import yfinance as yf
        tk = yf.Ticker(WOLF_SYMBOL)
        fi = tk.fast_info
        current_price = _safe_float(getattr(fi, "last_price", None) or getattr(fi, "lastPrice", None))
        last_vol = _safe_int(getattr(fi, "last_volume", None) or getattr(fi, "lastVolume", None))
        try:
            info = tk.info or {}
            avg_vol = _safe_int(info.get("averageVolume") or info.get("averageDailyVolume10Day"))
        except Exception:
            avg_vol = None
        if last_vol and avg_vol and avg_vol > 0:
            volume_ratio = round(last_vol / avg_vol, 2)
    except Exception as e:
        errors.append("volume: " + str(e)[:80])

    # 3. Sector correlation (reuse existing helper)
    try:
        sector = _fetch_sector_correlation()
    except Exception as e:
        errors.append("sector: " + str(e)[:80])

    # 4. 5-day SMA from yfinance daily bars
    try:
        import yfinance as yf
        tk = yf.Ticker(WOLF_SYMBOL)
        h = tk.history(period="7d", interval="1d")
        if h is not None and not h.empty:
            closes = [float(c) for c in h["Close"].tolist() if c is not None and not (isinstance(c, float) and math.isnan(c))]
            tail = closes[-5:] if len(closes) >= 5 else closes
            if tail:
                sma_5d = round(sum(tail) / len(tail), 4)
            # Fall back to last close if we never got a live price above
            if current_price is None and closes:
                current_price = round(closes[-1], 4)
    except Exception as e:
        errors.append("momentum: " + str(e)[:80])

    # Engine activity (last scan cycle) for freshness — "is the engine alive",
    # not "did it fire". Recorded by run_prediction_cycle every cycle.
    last_scan_ts = None
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT val FROM ghost_state WHERE key='last_prediction_cycle_ts'")
            r = cur.fetchone()
            if r and r[0]:
                last_scan_ts = int(float(r[0]))
    except Exception as e:
        errors.append("scan_ts: " + str(e)[:80])

    now_ts = int(time.time())
    # Rule-based market regime (audit §3) — modifies the score + shown in cockpit.
    from core.regime import classify_regime
    regime = classify_regime(current_price, sma_5d, volume_ratio)
    scored = compute_ghost_score(latest_pick, volume_ratio, sector, current_price, sma_5d, now_ts,
                                 last_scan_ts=last_scan_ts, regime=regime)

    payload = _ok({
        "symbol": WOLF_SYMBOL,
        "updated_at": now_ts,
        "score": scored["score"],
        "raw_score": scored["raw_score"],
        "signal": scored["signal"],
        "regime": regime,
        "components": scored["components"],
        "weights": scored["weights"],
        "inputs": {
            "model_pick_id": latest_pick.get("id") if latest_pick else None,
            "model_direction": latest_pick.get("direction") if latest_pick else None,
            "model_confidence": latest_pick.get("confidence") if latest_pick else None,
            "predicted_at": latest_pick.get("predicted_at") if latest_pick else None,
            "last_scan_ts": last_scan_ts,
            "volume_ratio": volume_ratio,
            "current_price": current_price,
            "sma_5d": sma_5d,
            "sector_signal": sector.get("signal") if sector else None,
        },
        "errors": errors if errors else None,
    })
    _cache_set("ghost-score", payload)
    return JSONResponse(content=payload)
