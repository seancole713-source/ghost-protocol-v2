"""
core/prices.py - Stock price fetcher (WOLF-only mode).
Primary: Alpaca real-time trades. Fallback: yfinance (fast_info + history).
"""
import os, time, logging, requests
from typing import Dict, Tuple, Any, Optional

LOGGER = logging.getLogger("ghost.prices")
POLYGON_KEY = os.getenv("POLYGON_API_KEY", "")
TIMEOUT = float(os.getenv("PRICE_PROVIDER_TIMEOUT_S", "8.0"))
STOCK_CACHE_TTL = int(os.getenv("STOCK_PRICE_TTL_S", "60"))  # refresh every 60s during market hours
INTRADAY_QUOTE_TTL_S = int(os.getenv("INTRADAY_QUOTE_TTL_S", "900"))  # RTH O/H/L bars; trade overlay on cache hit
_mem_cache: Dict[str, Tuple[float, float]] = {}
_intraday_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

# US equity regular hours (America/New_York)
_RTH_OPEN_MIN = 9 * 60 + 30
_RTH_CLOSE_MIN = 16 * 60


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


def _bar_et_minutes(bar: Dict[str, Any], et) -> Optional[int]:
    """Minutes since midnight ET for an Alpaca bar timestamp (UTC ISO)."""
    import datetime as _dt

    raw = bar.get("t")
    if not raw:
        return None
    try:
        s = str(raw).replace("Z", "+00:00")
        ts = _dt.datetime.fromisoformat(s)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        if et:
            ts = ts.astimezone(et)
        return ts.hour * 60 + ts.minute
    except Exception:
        return None


def _ohlc_from_bars(
    bars: list,
    et,
    *,
    start_min: Optional[int] = None,
    end_min: Optional[int] = None,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Aggregate open/high/low from 5Min bars, optionally filtered to an ET window."""
    filtered = []
    for bar in bars or []:
        mins = _bar_et_minutes(bar, et)
        if mins is None:
            continue
        if start_min is not None and mins < start_min:
            continue
        if end_min is not None and mins >= end_min:
            continue
        filtered.append(bar)
    if not filtered:
        return None, None, None
    opens = [float(b["o"]) for b in filtered if b.get("o")]
    highs = [float(b["h"]) for b in filtered if b.get("h")]
    lows = [float(b["l"]) for b in filtered if b.get("l")]
    if not opens or not highs or not lows:
        return None, None, None
    return round(opens[0], 4), round(max(highs), 4), round(min(lows), 4)


def _rth_close_from_bars(
    bars: list,
    et,
    *,
    start_min: Optional[int] = None,
    end_min: Optional[int] = None,
) -> Optional[float]:
    """Last RTH 5Min bar close (~4:00 PM ET cash close)."""
    filtered = []
    for bar in bars or []:
        mins = _bar_et_minutes(bar, et)
        if mins is None:
            continue
        if start_min is not None and mins < start_min:
            continue
        if end_min is not None and mins >= end_min:
            continue
        if bar.get("c"):
            filtered.append(bar)
    if not filtered:
        return None
    return round(float(filtered[-1]["c"]), 4)


def _parse_bar_session_date(bar: dict, et) -> Optional[str]:
    """Bar timestamp as YYYY-MM-DD in America/New_York."""
    import datetime as _dt

    s = bar.get("t") or bar.get("timestamp") or ""
    if not s:
        return None
    try:
        ts = _dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        if et:
            ts = ts.astimezone(et)
        return ts.date().isoformat()
    except Exception:
        return str(s)[:10] if len(str(s)) >= 10 else None


def _today_ohlc_from_alpaca_daily(
    sym: str,
    headers: dict,
    session_date,
    et,
) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Fallback: today's cash-session O/H/L from 1Day bar when 5Min bars are empty."""
    import datetime as _dt

    try:
        end = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        start = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        want = session_date.isoformat() if hasattr(session_date, "isoformat") else str(session_date)[:10]
        for feed_name in ("sip", "iex"):
            url = (
                f"https://data.alpaca.markets/v2/stocks/{sym.upper()}/bars"
                f"?timeframe=1Day&start={start}&end={end}&limit=10&feed={feed_name}"
            )
            r = requests.get(url, headers=headers, timeout=TIMEOUT)
            if r.status_code != 200:
                continue
            for bar in reversed(r.json().get("bars") or []):
                if _parse_bar_session_date(bar, et) != want:
                    continue
                o = float(bar.get("o") or 0)
                h = float(bar.get("h") or 0)
                l = float(bar.get("l") or 0)
                c = float(bar.get("c") or 0)
                if o > 0 and h > 0:
                    return round(o, 4), round(h, 4), round(l, 4), round(c, 4) if c > 0 else None
    except Exception as exc:
        LOGGER.debug("intraday daily fallback %s: %s", sym, str(exc)[:80])
    return None, None, None, None


def get_intraday_session(symbol: str) -> Dict[str, Any]:
    """Today's O/H/L + last trade via Alpaca (fallback yfinance).

    RTH open/high/low use 9:30–16:00 ET bars so the live row matches Google Finance
    during and after the cash session. Latest trade always wins for ``price``.
    """
    import datetime as _dt

    sym = (symbol or "").strip().upper()
    if not sym:
        return {}

    cached = _intraday_cache.get(sym)
    if cached and (time.time() - cached[0]) < INTRADAY_QUOTE_TTL_S:
        out = dict(cached[1])
        # Price-only cache hits must not skip OHLC refresh when open/high/low are missing.
        if out.get("today_open") is not None and out.get("today_high") is not None:
            trade = _alpaca(sym)
            if trade:
                out["price"] = round(float(trade), 4)
                if out.get("previous_close") and out["previous_close"] > 0:
                    out["change_abs"] = round(out["price"] - out["previous_close"], 4)
                    out["change_pct"] = round(out["change_abs"] / out["previous_close"] * 100, 3)
            out["as_of_ts"] = int(time.time())
            return out

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
    elif hm < _RTH_OPEN_MIN:
        session, session_label = "premarket", "Pre-market"
    elif hm < _RTH_CLOSE_MIN:
        session, session_label = "rth", "Market open"
    elif hm < 20 * 60:
        session, session_label = "afterhours", "After hours"
    else:
        session, session_label = "closed", "Closed"

    today_open = today_high = today_low = last_price = prev_close = None
    rth_open = rth_high = rth_low = rth_close = None
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
            bars = []
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
                feed = f"alpaca_{feed_name}"
                break
            if bars:
                ext_open, ext_high, ext_low = _ohlc_from_bars(bars, et)
                rth_open, rth_high, rth_low = _ohlc_from_bars(
                    bars, et, start_min=_RTH_OPEN_MIN, end_min=_RTH_CLOSE_MIN,
                )
                rth_close = _rth_close_from_bars(
                    bars, et, start_min=_RTH_OPEN_MIN, end_min=_RTH_CLOSE_MIN,
                )
                use_rth = session in ("rth", "afterhours") or hm >= _RTH_CLOSE_MIN
                if use_rth and rth_open is not None:
                    today_open, today_high, today_low = rth_open, rth_high, rth_low
                else:
                    today_open, today_high, today_low = ext_open, ext_high, ext_low
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

    if (today_open is None or today_high is None) and headers:
        d_o, d_h, d_l, d_c = _today_ohlc_from_alpaca_daily(sym, headers, session_date, et)
        if d_o is not None:
            today_open, today_high, today_low = d_o, d_h, d_l
            if rth_open is None:
                rth_open, rth_high, rth_low = d_o, d_h, d_l
            if rth_close is None and d_c:
                rth_close = d_c
            feed = feed or "alpaca_1d"

    if today_open is None and rth_open is not None:
        today_open, today_high, today_low = rth_open, rth_high, rth_low

    trade = _alpaca(sym)
    if trade:
        last_price = round(float(trade), 4)
        feed = feed or "alpaca_trade"

    if today_open is None or today_high is None:
        try:
            import yfinance as yf
            h = yf.Ticker(sym).history(period="1d", interval="5m")
            if h is not None and not h.empty:
                # yfinance 5m during RTH — filter roughly to session hours
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

    out = {
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
        "rth_open": rth_open,
        "rth_high": rth_high,
        "rth_low": rth_low,
        "rth_close": rth_close,
        "feed": feed,
    }
    _intraday_cache[sym] = (time.time(), out)
    return dict(out)


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
