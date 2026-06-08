"""
core/prices.py - Stock price fetcher (WOLF-only mode).
Primary: Alpaca real-time trades. Fallback: yfinance (fast_info + history).
"""
import os, time, logging, requests
from typing import Dict, Tuple, Any

LOGGER = logging.getLogger("ghost.prices")
POLYGON_KEY = os.getenv("POLYGON_API_KEY", "")
TIMEOUT = float(os.getenv("PRICE_PROVIDER_TIMEOUT_S", "8.0"))
STOCK_CACHE_TTL = int(os.getenv("STOCK_PRICE_TTL_S", "60"))  # refresh every 60s during market hours
_mem_cache: Dict[str, Tuple[float, float]] = {}


def _cache_get(symbol):
    if symbol in _mem_cache:
        price, ts = _mem_cache[symbol]
        if time.time() - ts < STOCK_CACHE_TTL:
            return price
        del _mem_cache[symbol]
    return None


def _cache_set(symbol, price):
    _mem_cache[symbol] = (price, time.time())


def _alpaca(symbol):
    """Real-time stock price from Alpaca — free tier, works on Railway."""
    try:
        key = os.getenv("ALPACA_KEY_ID", "")
        secret = os.getenv("ALPACA_SECRET_KEY", "")
        if not key or not secret:
            return None
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{symbol.upper()}/trades/latest",
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            return float(r.json()["trade"]["p"])
    except Exception:
        pass
    return None


def _yfinance(symbol):
    try:
        import yfinance as yf
        tk = yf.Ticker(symbol)
        # Try live price first via fast_info attributes (market hours)
        try:
            fi = tk.fast_info
            live = getattr(fi, 'last_price', None) or getattr(fi, 'lastPrice', None)
            if live and float(live) > 0:
                return float(live)
        except Exception:
            pass
        # Fallback: latest close (pre/post market or closed)
        h = tk.history(period="2d")
        if not h.empty:
            return float(h["Close"].iloc[-1])
    except Exception:
        return None


def get_stock_price(symbol):
    cached = _cache_get(symbol)
    if cached:
        return cached
    # Alpaca = real-time, yfinance = prev-close fallback
    price = _alpaca(symbol) or _yfinance(symbol)
    if price:
        _cache_set(symbol, price)
    return price


def get_price(symbol, asset_type=None):
    """WOLF-only mode: always returns stock price. asset_type kept for backward compat, ignored."""
    return get_stock_price(symbol)


def get_extended_session(symbol: str) -> Dict[str, Any]:
    """Extended-hours context: prior close, live quote, gap %, and session label.

    Used during pre-market scans so Ghost can price gaps without waiting for RTH.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return {}
    live = get_stock_price(sym)
    prev_close = None
    pre_market = None
    post_market = None
    try:
        import yfinance as yf
        fi = yf.Ticker(sym).fast_info
        prev_close = getattr(fi, "previous_close", None) or getattr(fi, "previousClose", None)
        pre_market = getattr(fi, "pre_market_price", None) or getattr(fi, "preMarketPrice", None)
        post_market = getattr(fi, "post_market_price", None) or getattr(fi, "postMarketPrice", None)
    except Exception:
        pass
    try:
        if prev_close is not None:
            prev_close = float(prev_close)
    except Exception:
        prev_close = None
    session = "closed"
    session_price = live
    try:
        import datetime as _dt
        try:
            from zoneinfo import ZoneInfo
            now_et = _dt.datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            now_et = _dt.datetime.utcnow() - _dt.timedelta(hours=5)
        if now_et.weekday() < 5:
            hm = now_et.hour * 60 + now_et.minute
            if 9 * 60 + 30 <= hm < 16 * 60:
                session = "rth"
            elif 4 * 60 <= hm < 9 * 60 + 30:
                session = "premarket"
                if pre_market and float(pre_market) > 0:
                    session_price = float(pre_market)
            elif 16 * 60 <= hm < 20 * 60:
                session = "afterhours"
                if post_market and float(post_market) > 0:
                    session_price = float(post_market)
    except Exception:
        pass
    gap_pct = None
    gap_abs = None
    if prev_close and prev_close > 0 and session_price and float(session_price) > 0:
        gap_abs = round(float(session_price) - prev_close, 4)
        gap_pct = round(gap_abs / prev_close * 100, 3)
    return {
        "symbol": sym,
        "session": session,
        "live_price": round(float(live), 4) if live else None,
        "session_price": round(float(session_price), 4) if session_price else None,
        "previous_close": round(prev_close, 4) if prev_close else None,
        "gap_abs": gap_abs,
        "gap_pct": gap_pct,
        "pre_market_price": round(float(pre_market), 4) if pre_market else None,
        "post_market_price": round(float(post_market), 4) if post_market else None,
        "ts": int(time.time()),
    }


def get_vix():
    try:
        import yfinance as yf
        h = yf.Ticker("^VIX").history(period="1d")
        if not h.empty:
            return float(h["Close"].iloc[-1])
    except Exception:
        pass
    return None


def check_feeds():
    """Health check — can we price the target symbol right now?

    WOLF-only system: probe the actual target (WOLF), not a proxy ticker. The
    earlier AAPL probe measured generic "feed alive" but hid whether we can
    actually price WOLF — misleading for a single-ticker product. Reports each
    spot-price feed plus an overall `priceable` flag. Full multi-tier OHLCV
    coverage detail lives at /api/diag/data-sources. Override with
    HEALTH_PROBE_SYMBOL if you specifically want to test generic feed health.
    """
    probe = os.getenv("HEALTH_PROBE_SYMBOL", "WOLF")
    _al = _alpaca(probe) is not None
    _yf = _yfinance(probe) is not None
    priceable = bool(_al or _yf)
    r = {"alpaca_stock": _al, "yfinance": _yf, "probe_symbol": probe, "priceable": priceable}
    working = sum(1 for v in (_al, _yf) if v)
    r["summary"] = (f"{probe} priceable ({working}/2 feeds)" if priceable
                    else f"{probe} NOT priceable (0/2 feeds)")
    return r
