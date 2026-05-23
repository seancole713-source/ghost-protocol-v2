"""
core/prices.py - Stock price fetcher (WOLF-only mode).
Primary: Alpaca real-time trades. Fallback: yfinance (fast_info + history).
"""
import os, time, logging, requests
from typing import Dict, Tuple

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
    """Health check - test stock price feeds.

    PR #24 fix: probe with a universally-available symbol (AAPL) instead
    of WOLF, since Alpaca's free/IEX tier doesn't carry post-restructure
    WOLF. The previous "1/2 feeds responding" status was misleading —
    Alpaca itself was working; it just didn't have WOLF. Probing with a
    well-covered ticker measures FEED HEALTH, not WOLF coverage. The
    WOLF-specific coverage check lives at /api/diag/data-sources.
    """
    probe = os.getenv("HEALTH_PROBE_SYMBOL", "AAPL")
    _al = _alpaca(probe) is not None
    _yf = _yfinance(probe) is not None
    r = {"alpaca_stock": _al, "yfinance": _yf, "probe_symbol": probe}
    working = sum(1 for v in (_al, _yf) if v)
    r["summary"] = f"{working}/2 feeds responding (probed with {probe})"
    return r
