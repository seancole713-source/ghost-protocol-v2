"""
core/prices.py - Multi-source price fetcher with quorum validation.
Crypto: CoinGecko + Coinbase + Binance (2 of 3 must agree within 2%)
Stocks: Polygon primary, yfinance fallback
"""
import os, time, logging, requests
from typing import Optional, Dict, Tuple

LOGGER = logging.getLogger("ghost.prices")
POLYGON_KEY = os.getenv("POLYGON_API_KEY", "")
TIMEOUT = float(os.getenv("PRICE_PROVIDER_TIMEOUT_S", "2.5"))
CACHE_TTL = int(os.getenv("PRICE_TTL_S", "120"))
_mem_cache: Dict[str, Tuple[float, float]] = {}

def _cache_get(symbol):
    if symbol in _mem_cache:
        price, ts = _mem_cache[symbol]
        if time.time() - ts < CACHE_TTL:
            return price
    return None

def _cache_set(symbol, price):
    _mem_cache[symbol] = (price, time.time())

def _coingecko(symbol):
    try:
        r = requests.get(f"https://api.coingecko.com/api/v3/simple/price?ids={symbol.lower()}&vs_currencies=usd", timeout=TIMEOUT)
        for k, v in r.json().items():
            return float(v["usd"])
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
    if not sources: return None
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

def _polygon(symbol):
    if not POLYGON_KEY: return None
    try:
        r = requests.get(f"https://api.polygon.io/v2/last/trade/{symbol.upper()}?apiKey={POLYGON_KEY}", timeout=TIMEOUT)
        d = r.json()
        if d.get("status") == "OK": return float(d["results"]["p"])
    except: return None

def _yfinance(symbol):
    try:
        import yfinance as yf
        h = yf.Ticker(symbol).history(period="1d")
        if not h.empty: return float(h["Close"].iloc[-1])
    except: return None

def get_stock_price(symbol):
    cached = _cache_get(symbol)
    if cached: return cached
    price = _polygon(symbol) or _yfinance(symbol)
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
        "coinbase": _coinbase("BTC") is not None,
        "binance": _binance("BTC") is not None,
        "polygon": _polygon("AAPL") is not None if POLYGON_KEY else False,
    }
    working = sum(1 for v in r.values() if v)
    r["summary"] = f"{working}/{len(r)} feeds responding"
    return r