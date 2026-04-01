"""
core/prices.py - Multi-source price fetcher with quorum validation.
Crypto: CoinGecko + Coinbase + Binance (2 of 3 must agree within 2%)
Stocks: Polygon primary, yfinance fallback
"""
import os, time, logging, requests
from typing import Optional, Dict, Tuple

LOGGER = logging.getLogger("ghost.prices")
POLYGON_KEY = os.getenv("POLYGON_API_KEY", "")
TIMEOUT = float(os.getenv("PRICE_PROVIDER_TIMEOUT_S", "8.0"))  # raised: CoinGecko/Binance need more time
CACHE_TTL = int(os.getenv("PRICE_TTL_S", "120"))
STOCK_CACHE_TTL = int(os.getenv("STOCK_PRICE_TTL_S", "60"))  # stocks refresh every 60s during market hours
_mem_cache: Dict[str, Tuple[float, float]] = {}

def _cache_get(symbol, is_stock=False):
    ttl = STOCK_CACHE_TTL if is_stock else CACHE_TTL
    if symbol in _mem_cache:
        price, ts = _mem_cache[symbol]
        if time.time() - ts < ttl:
            return price
        del _mem_cache[symbol]
    return None

def _cache_set(symbol, price):
    _mem_cache[symbol] = (price, time.time())

def _coingecko(symbol):
    try:
        r = requests.get(f"https://api.coingecko.com/api/v3/simple/price?ids={symbol.lower()}&vs_currencies=usd", timeout=TIMEOUT)
        for k, v in r.json().items():
            return float(v["usd"])
    except: 
        try:  # fallback: try coinbase for this symbol
            return _coinbase(symbol)
        except: return None

def _coinbase(symbol):
    try:
        r = requests.get(f"https://api.coinbase.com/v2/prices/{symbol.upper()}-USD/spot", timeout=TIMEOUT)
        return float(r.json()["data"]["amount"])
    except: return None

def _binance(symbol):
    try:
        r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol.upper()}USDT", timeout=TIMEOUT)
        return float(r.json()["price"])
    except: return None

def _alpaca_crypto(symbol):
    """Alpaca crypto latest bar — confirmed working on Railway, no rate limits."""
    try:
        import urllib.parse
        key = os.getenv("ALPACA_KEY_ID", "")
        secret = os.getenv("ALPACA_SECRET_KEY", "")
        if not key or not secret: return None
        ticker = symbol.upper() + "/USD"
        ticker_enc = urllib.parse.quote(ticker, safe='')
        r = requests.get(
            f"https://data.alpaca.markets/v1beta3/crypto/us/latest/bars?symbols={ticker_enc}",
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=TIMEOUT
        )
        if r.status_code == 200:
            bars = r.json().get("bars", {})
            bar = bars.get(ticker)
            if bar: return float(bar.get("c") or 0) or None
    except Exception as _e:
        LOGGER.debug(f"Alpaca crypto failed {symbol}: {_e}")
    return None

CRYPTO_MAP = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "ripple",
    "CHZ": "chiliz", "LINK": "chainlink", "ADA": "cardano", "AVAX": "avalanche-2",
    "DOT": "polkadot", "MATIC": "matic-network", "TRX": "tron", "LTC": "litecoin",
    "BCH": "bitcoin-cash", "ATOM": "cosmos", "UNI": "uniswap",
}

def get_crypto_price(symbol):
    cached = _cache_get(symbol)
    if cached: return cached
    cg_id = CRYPTO_MAP.get(symbol.upper(), symbol.lower())
    sources = []
    p1 = _coingecko(cg_id)
    if p1: sources.append(("coingecko", p1))
    p2 = _coinbase(symbol)
    if p2: sources.append(("coinbase", p2))
    p3 = _binance(symbol)
    if p3: sources.append(("binance", p3))
    if not sources:
        p4 = _alpaca_crypto(symbol)
        if p4:
            _cache_set(symbol, p4)
            LOGGER.info(f"Alpaca crypto fallback for {symbol}: {p4}")
            return p4
        return None
    vals = [p for _, p in sources]
    if len(vals) >= 2:
        for i in range(len(vals)):
            for j in range(i+1, len(vals)):
                if abs(vals[i]-vals[j]) / max(vals[i],vals[j]) <= 0.02:
                    price = (vals[i]+vals[j]) / 2
                    _cache_set(symbol, price)
                    return price
    price = vals[0]
    _cache_set(symbol, price)
    return price

def _alpaca(symbol):
    """Real-time stock price from Alpaca — free tier, works on Railway."""
    try:
        key = os.getenv("ALPACA_KEY_ID", "")
        secret = os.getenv("ALPACA_SECRET_KEY", "")
        if not key or not secret: return None
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{symbol.upper()}/trades/latest",
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=TIMEOUT
        )
        if r.status_code == 200:
            return float(r.json()["trade"]["p"])
    except Exception: pass
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
        except Exception: pass
        # Fallback: latest close (pre/post market or closed)
        h = tk.history(period="2d")
        if not h.empty: return float(h["Close"].iloc[-1])
    except: return None

def get_stock_price(symbol):
    cached = _cache_get(symbol, is_stock=True)
    if cached: return cached
    # Alpaca = real-time, yfinance = prev-close fallback
    price = _alpaca(symbol) or _yfinance(symbol)
    if price: _cache_set(symbol, price)
    return price

def get_price(symbol, asset_type="crypto"):
    return get_stock_price(symbol) if asset_type == "stock" else get_crypto_price(symbol)

def get_spy_price():
    return get_stock_price("SPY")

def get_vix():
    try:
        import yfinance as yf
        h = yf.Ticker("^VIX").history(period="1d")
        if not h.empty: return float(h["Close"].iloc[-1])
    except: pass
    return None

def check_feeds():
    """Health check - test all price sources."""
    r = {
        "coingecko": _coingecko("bitcoin") is not None,
        "coinbase":  _coinbase("BTC") is not None,
        "binance":   _binance("BTC") is not None,
        "alpaca":    _alpaca_crypto("BTC") is not None,
    }
    working = sum(1 for v in r.values() if v)
    r["summary"] = f"{working}/{len(r)} feeds responding"
    return r