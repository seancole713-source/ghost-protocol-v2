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


def get_intraday_session(symbol: str) -> Dict[str, Any]:
    """Today's extended-session O/H/L + last trade via Alpaca (fallback yfinance).

    Used by the Daily Prediction live row — must track the consolidated tape, not
    stale daily-bar closes.
    """
    import datetime as _dt

    sym = (symbol or "").strip().upper()
    if not sym:
        return {}

    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
    except Exception:
        et = None

    now_et = _dt.datetime.now(et) if et else _dt.datetime.utcnow() - _dt.timedelta(hours=5)
    session_date = now_et.date()
    market_date = session_date.isoformat()

    hm = now_et.hour * 60 + now_et.minute
    if now_et.weekday() >= 5:
        session, session_label = "closed", "Closed"
    elif hm < 4 * 60:
        session, session_label = "closed", "Closed"
    elif hm < 9 * 60 + 30:
        session, session_label = "premarket", "Pre-market"
    elif hm < 16 * 60:
        session, session_label = "rth", "Market open"
    elif hm < 20 * 60:
        session, session_label = "afterhours", "After hours"
    else:
        session, session_label = "closed", "Closed"

    today_open = today_high = today_low = last_price = prev_close = None
    feed = None

    key = os.getenv("ALPACA_KEY_ID", "")
    secret = os.getenv("ALPACA_SECRET_KEY", "")
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret} if key and secret else None

    if headers:
        try:
            if et:
                day_start = _dt.datetime(
                    session_date.year, session_date.month, session_date.day, 4, 0, tzinfo=et
                ).astimezone(_dt.timezone.utc)
            else:
                day_start = _dt.datetime.utcnow().replace(hour=9, minute=0, second=0, microsecond=0)
            start_str = day_start.strftime("%Y-%m-%dT%H:%M:%SZ")
            end_str = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            for feed_name in ("sip", "iex"):
                url = (
                    f"https://data.alpaca.markets/v2/stocks/{sym}/bars"
                    f"?timeframe=5Min&start={start_str}&end={end_str}&limit=10000&feed={feed_name}"
                )
                r = requests.get(url, headers=headers, timeout=TIMEOUT)
                if r.status_code != 200:
                    continue
                bars = r.json().get("bars") or []
                if not bars:
                    continue
                opens = [float(b["o"]) for b in bars if b.get("o")]
                highs = [float(b["h"]) for b in bars if b.get("h")]
                lows = [float(b["l"]) for b in bars if b.get("l")]
                closes = [float(b["c"]) for b in bars if b.get("c")]
                if opens:
                    today_open = round(opens[0], 4)
                if highs:
                    today_high = round(max(highs), 4)
                if lows:
                    today_low = round(min(lows), 4)
                if closes:
                    last_price = round(closes[-1], 4)
                feed = f"alpaca_{feed_name}"
                break
            # Prior session close from last two daily bars
            d_end = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            d_start = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
            for feed_name in ("sip", "iex"):
                url = (
                    f"https://data.alpaca.markets/v2/stocks/{sym}/bars"
                    f"?timeframe=1Day&start={d_start}&end={d_end}&limit=5&feed={feed_name}"
                )
                r = requests.get(url, headers=headers, timeout=TIMEOUT)
                if r.status_code != 200:
                    continue
                dbars = r.json().get("bars") or []
                if len(dbars) >= 2:
                    prev_close = round(float(dbars[-2].get("c", 0)), 4)
                elif len(dbars) == 1:
                    prev_close = round(float(dbars[0].get("o", 0)), 4)
                if prev_close and prev_close > 0:
                    break
        except Exception as exc:
            LOGGER.debug("intraday alpaca %s: %s", sym, str(exc)[:80])

    if last_price is None:
        last_price = _alpaca(sym)
        if last_price:
            last_price = round(float(last_price), 4)
            feed = feed or "alpaca_trade"

    if today_open is None or today_high is None:
        try:
            import yfinance as yf
            h = yf.Ticker(sym).history(period="1d", interval="5m")
            if h is not None and not h.empty:
                today_open = round(float(h["Open"].iloc[0]), 4)
                today_high = round(float(h["High"].max()), 4)
                today_low = round(float(h["Low"].min()), 4)
                yf_last = round(float(h["Close"].iloc[-1]), 4)
                if not last_price:
                    last_price = yf_last
                feed = feed or "yfinance_5m"
            if prev_close is None:
                fi = yf.Ticker(sym).fast_info
                pc = getattr(fi, "previous_close", None) or getattr(fi, "previousClose", None)
                if pc:
                    prev_close = round(float(pc), 4)
        except Exception:
            pass

    chg_abs = chg_pct = None
    if last_price and prev_close and prev_close > 0:
        chg_abs = round(last_price - prev_close, 4)
        chg_pct = round(chg_abs / prev_close * 100, 3)

    return {
        "symbol": sym,
        "as_of_ts": int(time.time()),
        "session": session,
        "session_label": session_label,
        "market_date": market_date,
        "price": last_price,
        "previous_close": prev_close,
        "change_abs": chg_abs,
        "change_pct": chg_pct,
        "today_open": today_open,
        "today_high": today_high,
        "today_low": today_low,
        "feed": feed,
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
