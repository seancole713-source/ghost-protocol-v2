"""
core/prices.py - Stock price fetcher (WOLF-only mode).
Primary: Alpaca real-time trades. Fallback: yfinance (fast_info + history).

P0-2 (audit): yfinance circuit breaker prevents wasted calls during persistent
JSON-parse failures overnight. P1-4: staleness flag on cached prices.
"""
import os, time, logging, requests, threading
from core.quiet import note_suppressed
from typing import Dict, Tuple, Any, Optional

from core.yfinance_client import ungated_ticker as _ungated_yf_ticker  # noqa: E402
LOGGER = logging.getLogger("ghost.prices")
POLYGON_KEY = os.getenv("POLYGON_API_KEY", "")
TIMEOUT = float(os.getenv("PRICE_PROVIDER_TIMEOUT_S", "8.0"))
STOCK_CACHE_TTL = int(os.getenv("STOCK_PRICE_TTL_S", "60"))  # refresh every 60s during market hours
INTRADAY_QUOTE_TTL_S = int(os.getenv("INTRADAY_QUOTE_TTL_S", "900"))  # RTH O/H/L bars; trade overlay on cache hit
# P2-6: force-refresh intraday OHLC when the live trade breaks this far OUTSIDE
# the cached high/low range (a breakout means cached OHLC is provably stale).
INTRADAY_MOVE_REFRESH_PCT = float(os.getenv("INTRADAY_MOVE_REFRESH_PCT", "2.0"))


def _intraday_breakout_pct(trade, cached_high, cached_low) -> float:
    """Percent the live trade sits OUTSIDE the cached [low, high] range.

    Returns 0.0 while the trade is inside the range. Distance from the high or
    low while inside the range is normal — on any big red day the price sits
    far below the session high all day — and must not bust the cache. The
    original P2-6 check measured that distance, which self-triggered on every
    call for any symbol with a >2x-threshold intraday range, deleted the cache
    each poll, and hammered Alpaca into its rate-limit breaker (50 calls/60s).
    """
    try:
        t = float(trade)
        hi = float(cached_high)
        lo = float(cached_low)
    except (TypeError, ValueError):
        return 0.0
    if t <= 0 or hi <= 0 or lo <= 0:
        return 0.0
    if t > hi:
        return (t - hi) / hi * 100.0
    if t < lo:
        return (lo - t) / lo * 100.0
    return 0.0
_mem_cache: Dict[str, Tuple[float, float]] = {}
_intraday_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
# Free-tier Alpaca keys are never SIP-entitled: after the first 403, skip SIP
# for a while instead of burning one guaranteed-403 call per request.
_SIP_FORBIDDEN = {"until": 0.0}


def _alpaca_bar_feeds() -> tuple:
    """Feed priority for Alpaca bar fetches — drops SIP while it's known 403."""
    if time.time() < _SIP_FORBIDDEN["until"]:
        return ("iex",)
    return ("sip", "iex")


def _note_alpaca_feed_status(feed_name: str, status_code: int) -> None:
    if feed_name == "sip" and status_code == 403:
        _SIP_FORBIDDEN["until"] = time.time() + 6 * 3600
# Persistent prev_close cache — survives market close when all feeds are down.
# Each entry is (session_date_iso, close): the value is the close OF THAT
# SESSION, and a read accepts it only when that session is the one a previous
# close must come from right now (_expected_prev_session). The old contract was
# (write_time, close) with a 24h TTL: on 2026-09-25 at 03:19 CDT GLND showed a
# previous close of 2.90 -- the 9/23 close written the previous morning, still
# inside 24h -- while the 9/24 close was ~4, so its premarket gap read ~38% high.
# Backed by ghost_state table for restart survival.
_prev_close_cache: Dict[str, Tuple[str, float]] = {}

# Price sanity guard: reject phantom quotes (wrong security / stale feed) that
# diverge wildly from an independent source. Catches the MU/SNDK class of bug
# where Alpaca IEX serves a foreign-listed twin at ~10x the real price. The
# cross-check is bounded by a per-symbol TTL so it never hammers yfinance.
PRICE_SANITY_DIVERGENCE_PCT = float(os.getenv("PRICE_SANITY_DIVERGENCE_PCT", "50.0"))
# Outside regular trading hours the reference yfinance gives (last_price, or
# previous_close) is the PRIOR regular-session close, and a premarket print is
# supposed to diverge from it -- that is the gap. The 50% band rejected real
# 9/24 gappers (PFSA +74.6%, APUS +169%), fell back to the stale close and
# reported a 0% gap. Outside RTH, or against a reference taken outside RTH,
# only a ratio beyond this bound is treated as a phantom: the ~10x wrong-
# security class (MU/SNDK) is still caught, a real +60%..+600% gap is not.
PRICE_SANITY_EXTENDED_MAX_RATIO = float(os.getenv("PRICE_SANITY_EXTENDED_MAX_RATIO", "8.0"))
PRICE_SANITY_CROSS_CHECK_TTL_S = int(os.getenv("PRICE_SANITY_CROSS_CHECK_TTL_S", "900"))
# Fail-closed mode: when no independent reference is available (yfinance down
# or breaker open), reject the candidate instead of trusting a single feed.
# Default off (fail-open) so a yfinance outage never starves pricing; enable
# for strict phantom protection on names with a known-bad feed.
PRICE_SANITY_FAIL_CLOSED = os.getenv("PRICE_SANITY_FAIL_CLOSED", "0").strip().lower() in ("1", "true", "yes", "on")
# symbol -> (fetched_at, reference, fetched_in_rth)
_cross_check_cache: Dict[str, Tuple[Any, ...]] = {}
# Cross-check call budget. Every symbol priced in one pass got its reference in
# the same minute, so all references expired together 15 min later and the next
# pricing pass (portfolio refresh, risk discipline, paper wallet ...) re-fetched
# them all at once -- a burst that tripped the shared yfinance rate-limit
# circuit ("CB yfinance: 15 calls in 60s -- rate-limit circuit OPEN for 600s")
# every 15 minutes and blocked price-critical yfinance fallbacks with it. At
# most this many cross-checks run per rolling minute; beyond that a reference
# up to PRICE_SANITY_STALE_REF_GRACE_S old is reused, else the candidate is
# handled exactly as when yfinance is unavailable (fail-open / fail-closed).
PRICE_SANITY_CROSS_CHECK_MAX_PER_MIN = int(os.getenv("PRICE_SANITY_CROSS_CHECK_MAX_PER_MIN", "6"))
PRICE_SANITY_STALE_REF_GRACE_S = int(
    os.getenv("PRICE_SANITY_STALE_REF_GRACE_S", str(2 * PRICE_SANITY_CROSS_CHECK_TTL_S))
)
_cross_check_calls: list = []
_cross_check_lock = threading.Lock()


def _cross_check_budget_ok(now: float) -> bool:
    """Take one cross-check slot for this rolling minute, if any is left."""
    limit = PRICE_SANITY_CROSS_CHECK_MAX_PER_MIN
    if limit <= 0:
        return True
    with _cross_check_lock:
        cutoff = now - 60.0
        _cross_check_calls[:] = [t for t in _cross_check_calls if t > cutoff]
        if len(_cross_check_calls) >= limit:
            return False
        _cross_check_calls.append(now)
        return True

def _parse_iso_date(value):
    """``date`` for a YYYY-MM-DD string, else None."""
    import datetime as _dt
    try:
        return _dt.date.fromisoformat(str(value)[:10]) if isinstance(value, str) else None
    except ValueError:
        return None


def _expected_prev_session(now=None):
    """The trading session whose close is "the previous close" right now.

    On a trading day D (premarket, RTH, after hours, or overnight in Central
    time) it is the session before D; on a weekend or holiday it is the session
    before the most recent one -- the same convention as yfinance's
    ``previous_close`` and Alpaca's snapshot ``prevDailyBar``. Weekends and NYSE
    holidays come from core.market_hours via core.daily_bar_contract.
    """
    from core.daily_bar_contract import previous_session
    from core.market_hours import is_market_holiday

    now = now or _now_ct()
    day = now.date()
    if is_market_holiday(day):
        day = previous_session(day)
    return previous_session(day)


def _bar_close_for_session(bars, expected) -> Optional[float]:
    """Close of the daily bar dated ``expected``; None when no bar is.

    A bar for any other session is never substituted: a stale close would be
    served as the previous close and inflate or erase the gap.
    """
    from core.daily_bar_contract import bar_session_date

    for bar in bars or []:
        if not isinstance(bar, dict) or bar_session_date(bar.get("t")) != expected:
            continue
        try:
            close = float(bar.get("c") or 0)
        except (TypeError, ValueError):
            continue
        if close > 0:
            return round(close, 4)
    return None


def _snapshot_prev_close(snapshot, expected) -> Optional[float]:
    """Previous close from an Alpaca snapshot, verified by bar date.

    Normally ``prevDailyBar`` is the previous session. Early premarket the
    snapshot lags: no bar exists for today yet, so ``dailyBar`` IS the previous
    session and ``prevDailyBar`` is the one before it (the GLND 2.90 read).
    Whichever of the two is dated ``expected`` wins; neither -> None.
    """
    snap = snapshot or {}
    return _bar_close_for_session(
        [snap.get("dailyBar"), snap.get("prevDailyBar")], expected,
    )


def _alpaca_daily_close(symbol, expected, headers) -> Optional[float]:
    """Close of the ``expected`` session from Alpaca 1Day bars.

    Newest first with a start date: an ascending ``limit=5`` over a 10-day
    window returned the OLDEST bars in the window, so ``bars[-2]`` was about a
    week old (U17).
    """
    import datetime as _dt

    if not headers:
        return None
    try:
        start = _dt.datetime(expected.year, expected.month, expected.day, tzinfo=_dt.timezone.utc)
        start -= _dt.timedelta(days=7)
        start_s = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_s = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for feed_name in _alpaca_bar_feeds():
            r = requests.get(
                f"https://data.alpaca.markets/v2/stocks/{str(symbol).upper()}/bars",
                headers=headers,
                params={
                    "timeframe": "1Day", "start": start_s, "end": end_s,
                    "limit": 10, "sort": "desc", "feed": feed_name,
                },
                timeout=TIMEOUT,
            )
            if r.status_code != 200:
                _note_alpaca_feed_status(feed_name, r.status_code)
                continue
            close = _bar_close_for_session((r.json() or {}).get("bars") or [], expected)
            if close:
                return close
    except Exception as exc:
        LOGGER.debug("alpaca daily close %s: %s", symbol, str(exc)[:80])
    return None


def _prev_close_cache_get(symbol, expected) -> Optional[float]:
    """Cached close only when it is the ``expected`` session's close."""
    entry = _prev_close_cache.get(symbol)
    if not entry:
        return None
    try:
        session, val = entry
        val = float(val)
    except (TypeError, ValueError):
        return None
    if _parse_iso_date(session) != expected or val <= 0:
        return None
    return val


def _prev_close_cache_put(symbol, expected, value) -> None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return
    if value <= 0:
        return
    entry = (expected.isoformat(), round(value, 4))
    if _prev_close_cache.get(symbol) != entry:
        _prev_close_cache[symbol] = entry
        _save_prev_close_cache()


def _load_prev_close_cache():
    """Load persisted prev_close values from ghost_state on module init."""
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT val FROM ghost_state WHERE key='prev_close_cache'")
            row = cur.fetchone()
            if row and row[0]:
                import json
                data = json.loads(row[0])
                for sym, entry in data.items():
                    # Legacy (write_time, close) rows carry no session date and
                    # cannot be validated: drop them, never guess their session.
                    try:
                        session, val = entry
                    except (TypeError, ValueError):
                        continue
                    if _parse_iso_date(session) is None:
                        continue
                    try:
                        val = float(val)
                    except (TypeError, ValueError):
                        continue
                    if val > 0:
                        _prev_close_cache[sym] = (str(session), val)
    except Exception:
        note_suppressed()
def _save_prev_close_cache():
    """Persist prev_close cache to ghost_state so it survives restarts."""
    try:
        from core.db import db_conn
        import json
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO ghost_state (key, val) VALUES ('prev_close_cache', %s) "
                "ON CONFLICT (key) DO UPDATE SET val = EXCLUDED.val",
                (json.dumps(_prev_close_cache),),
            )
    except Exception:
        note_suppressed()  # Load persisted cache on module init
_load_prev_close_cache()

# P0-2: circuit breaker for yfinance (wired in _yfinance below)
from core.circuit_breaker import _yfinance_cb, _alpaca_cb

# US equity regular hours — Central Time (see core.market_hours)
from core.market_hours import (
    AFTERHOURS_END_MIN,
    PREMARKET_START_MIN,
    RTH_CLOSE_MIN,
    RTH_OPEN_MIN,
    SESSION_TZ,
    is_us_after_hours,
    is_us_premarket,
    is_us_rth,
    _now_ct,
)


def _cache_get(symbol):
    if symbol in _mem_cache:
        price, ts = _mem_cache[symbol]
        if time.time() - ts < STOCK_CACHE_TTL:
            return price
        del _mem_cache[symbol]
    return None


def _cache_set(symbol, price):
    _mem_cache[symbol] = (price, time.time())


def _in_rth_now() -> bool:
    """True during the regular cash session; True on a clock error (strict)."""
    try:
        return bool(is_us_rth(_now_ct()))
    except Exception:
        return True


def _reject_phantom(symbol, price):
    """Reject phantom quotes (wrong security / stale feed) before they corrupt
    features, entries, and models.

    A single feed returning a ~10x-off price (e.g. Alpaca IEX serving a
    foreign-listed twin for MU/SNDK) must not enter the cache. We cross-check
    against an independent source (yfinance) and reject when the candidate
    diverges by more than PRICE_SANITY_DIVERGENCE_PCT. The cross-check is
    bounded by a per-symbol TTL and gated by the yfinance circuit breaker, so
    it is cheap in the common case and fail-open when yfinance is down.

    The tight band applies only in RTH against a reference also taken in RTH.
    Outside RTH -- or at the open, against a reference taken in premarket --
    the reference is the prior regular-session close and a real premarket
    gap diverges from it by design, so only a ratio beyond
    PRICE_SANITY_EXTENDED_MAX_RATIO (the ~10x phantom class) is rejected.

    Returns (price_or_None, rejected_bool).
    """
    if not price:
        return None, False
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None, False
    if p <= 0:
        return None, False

    in_rth = _in_rth_now()
    # Bound cross-check frequency: reuse a recent reference price per symbol.
    ref = None
    ref_in_rth = True  # legacy 2-tuple entries: treat as a live RTH reference
    now = time.time()
    cc = _cross_check_cache.get(symbol)
    if cc and now - cc[0] < PRICE_SANITY_CROSS_CHECK_TTL_S:
        ref = cc[1]
        ref_in_rth = bool(cc[2]) if len(cc) > 2 else True
    elif not _cross_check_budget_ok(now):
        # Over this minute's cross-check budget: a slightly older reference
        # still catches a ~10x phantom; with none, fall through to the same
        # handling as a yfinance outage.
        if cc and now - cc[0] < PRICE_SANITY_STALE_REF_GRACE_S:
            ref = cc[1]
            ref_in_rth = bool(cc[2]) if len(cc) > 2 else True
    else:
        try:
            from core.circuit_breaker import _yfinance_cb
            if _yfinance_cb.allow():
                # allow() above took this call's breaker slot: construct the
                # Ticker without the process-wide gate counting it again.
                fi = _ungated_yf_ticker(symbol).fast_info
                ref = getattr(fi, "last_price", None) or getattr(fi, "previous_close", None)
                if ref and float(ref) > 0:
                    ref = float(ref)
                    ref_in_rth = in_rth
                    _cross_check_cache[symbol] = (time.time(), ref, in_rth)
                    _yfinance_cb.record_success()
        except Exception:
            note_suppressed()

    if not ref or float(ref) <= 0:
        if PRICE_SANITY_FAIL_CLOSED:
            LOGGER.warning(
                "price sanity %s: no independent reference, fail-closed — rejecting %.2f",
                symbol, p,
            )
            return None, True
        return p, False  # fail-open: no independent reference available
    ref = float(ref)
    if in_rth and ref_in_rth:
        phantom = abs(p - ref) / ref * 100.0 > PRICE_SANITY_DIVERGENCE_PCT
    else:
        phantom = max(p / ref, ref / p) > PRICE_SANITY_EXTENDED_MAX_RATIO
    if phantom:
        LOGGER.warning(
            "price sanity %s: rejecting phantom %.2f (independent ref %.2f, %s)",
            symbol, p, ref, "rth" if in_rth and ref_in_rth else "extended-hours ratio",
        )
        return None, True
    return p, False


def _timestamp_to_epoch(raw) -> Optional[int]:
    """Normalize provider timestamps to Unix seconds without inventing a time."""
    if raw is None:
        return None
    try:
        if hasattr(raw, "timestamp"):
            return int(raw.timestamp())
        if isinstance(raw, (int, float)):
            value = float(raw)
            if value > 1e15:
                value /= 1e9
            elif value > 1e12:
                value /= 1e3
            return int(value)
        import datetime as _dt

        parsed = _dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        return int(parsed.timestamp())
    except Exception:
        return None


def _alpaca_trade_quote(symbol) -> Tuple[Optional[float], Optional[int]]:
    """Return Alpaca's latest trade price and provider observation timestamp."""
    if not _alpaca_cb.allow():
        return None, None
    try:
        key = os.getenv("ALPACA_KEY_ID", "")
        secret = os.getenv("ALPACA_SECRET_KEY", "")
        if not key or not secret:
            return None, None
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{symbol.upper()}/trades/latest",
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            trade = r.json()["trade"]
            _alpaca_cb.record_success()
            return float(trade["p"]), _timestamp_to_epoch(trade.get("t"))
        if r.status_code >= 500 or r.status_code == 429:
            _alpaca_cb.record_failure()
    except Exception:
        _alpaca_cb.record_failure()
    return None, None


def _alpaca_prev_close(symbol, expected=None) -> Optional[float]:
    """The previous session's close from Alpaca, verified by bar date.

    Used when yfinance is unavailable and nothing is cached: without it the
    extended-session quote discarded a good live Alpaca price. On 2026-09-24,
    with the yfinance breaker open, GRAL, GLND, GRML, SKYQ and P all returned
    "no_price_available" while Alpaca had live trades for every one.

    The snapshot's ``prevDailyBar`` alone was one session stale in early
    premarket (no bar for today yet): the bar dated ``expected`` is used, and
    when neither snapshot bar is, date-filtered 1Day bars are.
    """
    expected = expected or _expected_prev_session()
    if not _alpaca_cb.allow():
        return None
    key = os.getenv("ALPACA_KEY_ID", "")
    secret = os.getenv("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        return None
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    try:
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{symbol.upper()}/snapshot",
            headers=headers,
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            _alpaca_cb.record_success()
            close = _snapshot_prev_close(r.json() or {}, expected)
            if close:
                return close
        elif r.status_code >= 500 or r.status_code == 429:
            _alpaca_cb.record_failure()
            return None
    except Exception:
        _alpaca_cb.record_failure()
        return None
    return _alpaca_daily_close(symbol, expected, headers)


def _alpaca(symbol):
    """Real-time Alpaca price; retained as a float-only compatibility API."""
    price, _price_as_of_ts = _alpaca_trade_quote(symbol)
    return price


def _yfinance_quote(symbol) -> Tuple[Optional[float], Optional[int]]:
    """Return Yahoo price with a genuine observation timestamp when available.

    ``fast_info.last_price`` does not expose trustworthy market time, so it is
    returned with ``None``. The history fallback preserves its bar index.
    """
    if not _yfinance_cb.allow():
        return None, None
    try:
        import yfinance as yf
        tk = _ungated_yf_ticker(symbol)
        try:
            fi = tk.fast_info
            live = getattr(fi, 'last_price', None) or getattr(fi, 'lastPrice', None)
            if live and float(live) > 0:
                _yfinance_cb.record_success()
                return float(live), None
        except Exception:
            note_suppressed()
        h = tk.history(period="2d")
        if not h.empty:
            _yfinance_cb.record_success()
            return float(h["Close"].iloc[-1]), _timestamp_to_epoch(h.index[-1])
        return None, None
    except Exception as e:
        es = str(e)
        if "429" in es or "Too Many Requests" in es or "rate limit" in es.lower():
            LOGGER.warning(f"yfinance {symbol}: RATE LIMITED (429) — counting as breaker failure")
            _yfinance_cb.record_failure()
        elif "connection" in es.lower() or "timeout" in es.lower() or "timed out" in es.lower():
            LOGGER.warning(f"yfinance {symbol}: connection/timeout — counting as breaker failure: {e}")
            _yfinance_cb.record_failure()
        elif "Expecting value" in es or "JSON" in es or "json" in es.lower() or "parse" in es.lower():
            LOGGER.warning(f"yfinance {symbol}: JSON parse error (empty response) — counting as breaker failure: {e}")
            _yfinance_cb.record_failure()
        else:
            LOGGER.debug(f"yfinance {symbol}: non-critical error: {e}")
        return None, None


def _yfinance(symbol):
    """Yahoo price compatibility wrapper."""
    price, _observed_at = _yfinance_quote(symbol)
    return price


def _polygon_spot(symbol):
    """Previous close from Polygon.io — used ONLY for prev_close fallback.

    WARNING: This calls the /prev endpoint which returns YESTERDAY'S close,
    not a live spot price. Do NOT use this for live pricing — it is only
    suitable as a prev_close source when all other feeds are down.
    """
    if not POLYGON_KEY:
        return None
    try:
        r = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/{symbol.upper()}/prev",
            params={"adjusted": "true", "apiKey": POLYGON_KEY},
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            data = r.json()
            results = data.get("results", [])
            if results and results[0].get("c"):
                return float(results[0]["c"])
    except Exception:
        note_suppressed()
    return None


def _iex_trade_quote(symbol) -> Tuple[Optional[float], Optional[int]]:
    """Return Alpaca IEX trade price and provider observation timestamp."""
    try:
        key = os.getenv("ALPACA_KEY_ID", "")
        secret = os.getenv("ALPACA_SECRET_KEY", "")
        if not key or not secret:
            return None, None
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{symbol.upper()}/trades/latest",
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            params={"feed": "iex"},
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            trade = r.json()["trade"]
            return float(trade["p"]), _timestamp_to_epoch(trade.get("t"))
    except Exception:
        note_suppressed()
    return None, None


def _iex_spot(symbol):
    """Alpaca IEX float-only compatibility wrapper."""
    price, _observed_at = _iex_trade_quote(symbol)
    return price


def get_stock_price(symbol, *, with_staleness: bool = False):
    """Return live price from the spot chain: Alpaca → yfinance → Alpaca IEX.
    When with_staleness=True, returns (price, stale_flag).

    Polygon is intentionally NOT in this chain: its /prev endpoint returns
    yesterday's close, not a live price, and serving it as spot silently
    corrupts entries, drift, and TP/SL checks. It remains available as an
    explicit prev_close source only (see _polygon_spot / get_prev_close).
    Stooq is deprecated (JS challenge, always None)."""
    cached = _cache_get(symbol)
    if cached:
        return (cached, False) if with_staleness else cached
    price = _alpaca(symbol)
    price, _rej = _reject_phantom(symbol, price)
    if not price:
        price = _yfinance(symbol)
    if not price:
        price = _iex_spot(symbol)
        price, _rej = _reject_phantom(symbol, price)
    if price:
        _cache_set(symbol, price)
        return (price, False) if with_staleness else price
    # All providers failed — serve stale cache if available
    if symbol in _mem_cache:
        stale_price, _ts = _mem_cache[symbol]
        LOGGER.debug("price %s: all providers failed, serving stale cache", symbol)
        return (stale_price, True) if with_staleness else stale_price
    return (None, True) if with_staleness else None


def get_price(symbol, asset_type=None):
    """WOLF-only mode: always returns stock price. asset_type kept for backward compat, ignored."""
    return get_stock_price(symbol)


def _session_start_epoch(now_ct, session: str) -> Optional[int]:
    """Epoch second at which the CURRENT session began, or None when closed.

    A price stamped before this moment belongs to an earlier session and must
    never be priced against a previous close as if it were this session's move.
    """
    starts = {
        "premarket": PREMARKET_START_MIN,
        "rth": RTH_OPEN_MIN,
    }
    if session == "afterhours":
        try:
            from core.market_hours import _rth_close_for
            start_min = _rth_close_for(now_ct)
        except Exception:
            start_min = RTH_CLOSE_MIN
    else:
        start_min = starts.get(session)
    if start_min is None:
        return None
    try:
        if now_ct.tzinfo is None:
            # A naive clock is exchange-local by contract (_now_ct); without this
            # .timestamp() would silently read it in the HOST's timezone.
            from zoneinfo import ZoneInfo
            now_ct = now_ct.replace(tzinfo=ZoneInfo(SESSION_TZ))
        start = now_ct.replace(
            hour=start_min // 60, minute=start_min % 60, second=0, microsecond=0,
        )
        return int(start.timestamp())
    except Exception:
        return None


def get_extended_session(symbol: str) -> Dict[str, Any]:
    """Extended-hours context: prior close, live quote, gap %, and session label.

    Used during pre-market scans so Ghost can price gaps without waiting for RTH.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return {}
    live_quote, price_as_of_ts = _alpaca_trade_quote(sym)
    phantom_rejected = False
    if live_quote is not None:
        live_quote, phantom_rejected = _reject_phantom(sym, live_quote)
    if live_quote is None:
        # A rejected (or absent) print's timestamp belongs to THAT print. The
        # fallback below is untimed and must never inherit it -- that is how a
        # stale close was labelled "current_session" with a 0% gap.
        price_as_of_ts = None
    live = live_quote if live_quote is not None else get_stock_price(sym)
    prev_close = None
    prev_close_session = None
    pre_market = None
    post_market = None
    try:
        expected_prev = _expected_prev_session()
    except Exception:
        expected_prev = None
    from core.circuit_breaker import _yfinance_cb
    try:
        if not _yfinance_cb.allow():
            # yfinance breaker open — fall back to the persistent prev_close
            # cache, accepted only for the previous session's close.
            if expected_prev is not None:
                prev_close = _prev_close_cache_get(sym, expected_prev)
                if prev_close is None:
                    prev_close = _alpaca_prev_close(sym)
                    if prev_close:
                        _prev_close_cache_put(sym, expected_prev, prev_close)
                if prev_close is not None:
                    prev_close_session = expected_prev.isoformat()
            if prev_close is None and live_quote is None:
                return {}
        else:
            import yfinance as yf
            fi = _ungated_yf_ticker(sym).fast_info
            prev_close = getattr(fi, "previous_close", None) or getattr(fi, "previousClose", None)
            pre_market = getattr(fi, "pre_market_price", None) or getattr(fi, "preMarketPrice", None)
            post_market = getattr(fi, "post_market_price", None) or getattr(fi, "postMarketPrice", None)
            _yfinance_cb.record_success()
    except Exception:
        _yfinance_cb.record_failure()
    try:
        if prev_close is not None:
            prev_close = float(prev_close)
    except Exception:
        prev_close = None
    session = "closed"
    session_price = live
    session_price_as_of_ts = price_as_of_ts
    session_price_source = "alpaca_trade" if live_quote is not None else None
    now_ct = None
    try:
        now_ct = _now_ct()
        if now_ct.weekday() < 5:
            if is_us_rth(now_ct):
                session = "rth"
            elif is_us_premarket(now_ct):
                session = "premarket"
                if pre_market and float(pre_market) > 0:
                    session_price = float(pre_market)
                    session_price_as_of_ts = None
                    session_price_source = "yfinance_fast_info"
            elif is_us_after_hours(now_ct):
                session = "afterhours"
                if post_market and float(post_market) > 0:
                    session_price = float(post_market)
                    session_price_as_of_ts = None
                    session_price_source = "yfinance_fast_info"
    except Exception:
        note_suppressed()
    # ONE BASIS OR NONE -- the rule PR #194 applied to the discovery screener,
    # applied here to the main quote path. Caught live 2026-09-22 premarket:
    # WOLF, SHOP and BB had no Alpaca print yet that morning, so `live` was
    # Monday's 16:00 ET closing trade, 17.4h old. It was labelled "premarket"
    # and priced against the previous close to give a "gap" of +16.9%, +7.1%
    # and +7.4% -- which were Monday's completed moves, presented as Tuesday's
    # premarket gaps. The number is still returned; it simply may not be
    # called this session's gap.
    session_start_ts = _session_start_epoch(now_ct, session) if now_ct else None
    now_ts = int(time.time())
    session_price_age_s = None
    session_price_basis = "no_price"
    if session_price and float(session_price) > 0:
        if phantom_rejected and session_price_source is None:
            # The live print was rejected and this is the untimed fallback
            # (usually the prior close itself): it says nothing about how this
            # session is moving, so it carries no gap.
            session_price_basis = "phantom_rejected"
        elif session_price_as_of_ts is None:
            session_price_basis = "unverified_time"
        else:
            session_price_age_s = max(0, now_ts - int(session_price_as_of_ts))
            if session_start_ts is not None and int(session_price_as_of_ts) < session_start_ts:
                session_price_basis = "prior_session_trade"
            else:
                session_price_basis = "current_session"
    gap_pct = None
    gap_abs = None
    if (
        session_price_basis not in ("prior_session_trade", "phantom_rejected")
        and prev_close and prev_close > 0
        and session_price and float(session_price) > 0
    ):
        gap_abs = round(float(session_price) - prev_close, 4)
        gap_pct = round(gap_abs / prev_close * 100, 3)
    return {
        "symbol": sym,
        "session": session,
        "live_price": round(float(live), 4) if live else None,
        "session_price": round(float(session_price), 4) if session_price else None,
        "previous_close": round(prev_close, 4) if prev_close else None,
        # Session date the previous close was verified against (Alpaca bar
        # date / dated cache); None when the source carries no date (yfinance).
        "previous_close_session": prev_close_session if prev_close else None,
        "gap_abs": gap_abs,
        "gap_pct": gap_pct,
        "pre_market_price": round(float(pre_market), 4) if pre_market else None,
        "post_market_price": round(float(post_market), 4) if post_market else None,
        "price_as_of_ts": session_price_as_of_ts,
        "price_source": session_price_source,
        "session_start_ts": session_start_ts,
        "session_price_age_s": session_price_age_s,
        # current_session | prior_session_trade | unverified_time |
        # phantom_rejected | no_price. A prior_session_trade carries NO gap:
        # the last print predates this session, so there is no observation of
        # how this session is moving. phantom_rejected: the live print failed
        # the sanity guard and session_price is an untimed fallback -- no gap.
        "phantom_rejected": bool(phantom_rejected),
        "session_price_basis": session_price_basis,
        "requested_at_ts": now_ts,
        "ts": now_ts,
    }


def _bar_et_minutes(bar: Dict[str, Any], tz) -> Optional[int]:
    """Minutes since midnight Central for an Alpaca bar timestamp (UTC ISO)."""
    import datetime as _dt

    raw = bar.get("t")
    if not raw:
        return None
    try:
        s = str(raw).replace("Z", "+00:00")
        ts = _dt.datetime.fromisoformat(s)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        if tz:
            ts = ts.astimezone(tz)
        return ts.hour * 60 + ts.minute
    except Exception:
        return None


def _ohlc_from_bars(
    bars: list,
    tz,
    *,
    start_min: Optional[int] = None,
    end_min: Optional[int] = None,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Aggregate open/high/low from 5Min bars, optionally filtered to a Central-time window."""
    filtered = []
    for bar in bars or []:
        mins = _bar_et_minutes(bar, tz)
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
    tz,
    *,
    start_min: Optional[int] = None,
    end_min: Optional[int] = None,
) -> Optional[float]:
    """Last RTH 5Min bar close (~3:00 PM CT cash close)."""
    filtered = []
    for bar in bars or []:
        mins = _bar_et_minutes(bar, tz)
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


def _parse_bar_session_date(bar: dict, tz) -> Optional[str]:
    """Bar timestamp as YYYY-MM-DD in America/Chicago."""
    import datetime as _dt

    s = bar.get("t") or bar.get("timestamp") or ""
    if not s:
        return None
    try:
        ts = _dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        if tz:
            ts = ts.astimezone(tz)
        return ts.date().isoformat()
    except Exception:
        return str(s)[:10] if len(str(s)) >= 10 else None


def _today_ohlc_from_alpaca_daily(
    sym: str,
    headers: dict,
    session_date,
    tz,
) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Fallback: today's cash-session O/H/L from 1Day bar when 5Min bars are empty."""
    import datetime as _dt

    try:
        end = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        start = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        want = session_date.isoformat() if hasattr(session_date, "isoformat") else str(session_date)[:10]
        for feed_name in _alpaca_bar_feeds():
            url = (
                f"https://data.alpaca.markets/v2/stocks/{sym.upper()}/bars"
                f"?timeframe=1Day&start={start}&end={end}&limit=10&feed={feed_name}"
            )
            r = requests.get(url, headers=headers, timeout=TIMEOUT)
            if r.status_code != 200:
                _note_alpaca_feed_status(feed_name, r.status_code)
                continue
            for bar in reversed(r.json().get("bars") or []):
                if _parse_bar_session_date(bar, tz) != want:
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

    RTH open/high/low use 8:30–15:00 CT bars so the live row matches Google Finance
    during and after the cash session. Latest trade always wins for ``price``.
    """
    import datetime as _dt

    sym = (symbol or "").strip().upper()
    if not sym:
        return {}

    cached = _intraday_cache.get(sym)
    if cached and cached[1].get("market_date") not in (None, _now_ct().date().isoformat()):
        cached = None  # yesterday's row: its previous close is a session stale
    if cached and (time.time() - cached[0]) < INTRADAY_QUOTE_TTL_S:
        out = dict(cached[1])
        # Always refresh price + change on cache hit when we can get a live trade.
        trade, trade_ts = _alpaca_trade_quote(sym)
        if trade:
            trade, _rej = _reject_phantom(sym, trade)
        if trade:
            out["price"] = round(float(trade), 4)
            out["price_as_of_ts"] = trade_ts
        # Compute change_pct whenever we have both price and prev_close,
        # even if the live trade fetch failed (breaker may be open).
        if out.get("price") and out.get("previous_close") and out["previous_close"] > 0:
            out["change_abs"] = round(out["price"] - out["previous_close"], 4)
            out["change_pct"] = round(out["change_abs"] / out["previous_close"] * 100, 3)
        # prev_close may be null in cache if yfinance was down on first fetch.
        # Try Polygon/Stooq as non-yfinance fallbacks.
        if not out.get("previous_close"):
            pc = _polygon_spot(sym)
            if pc:
                out["previous_close"] = round(float(pc), 4)
                if out.get("price") and out["price"] > 0:
                    out["change_abs"] = round(out["price"] - out["previous_close"], 4)
                    out["change_pct"] = round(out["change_abs"] / out["previous_close"] * 100, 3)
        # OHLC-specific refresh: only when we already have open/high/low cached.
        if out.get("today_open") is not None and out.get("today_high") is not None:
            # P2-6: force-refresh OHLC if live price moved significantly from cached values
            cached_high = out.get("today_high")
            cached_low = out.get("today_low")
            if trade and cached_high and cached_low:
                breakout = _intraday_breakout_pct(trade, cached_high, cached_low)
                if breakout >= INTRADAY_MOVE_REFRESH_PCT:
                    LOGGER.info("intraday %s: live trade broke %.1f%% outside cached OHLC range, force-refreshing",
                                sym, breakout)
                    del _intraday_cache[sym]  # force full refresh below
                    cached = None
            if cached is not None:
                # P1-4: staleness flag — cached data is within TTL but not live-refreshed
                cache_age_s = int(time.time() - cached[0])
                out["data_stale"] = cache_age_s > (INTRADAY_QUOTE_TTL_S / 2)
                out["cache_age_s"] = cache_age_s
                out["requested_at_ts"] = int(time.time())
                return out

    try:
        from zoneinfo import ZoneInfo
        ct = ZoneInfo(SESSION_TZ)
    except Exception:
        ct = None

    now_ct = _dt.datetime.now(ct) if ct else _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) - _dt.timedelta(hours=6)
    session_date = now_ct.date()
    market_date = session_date.isoformat()

    hm = now_ct.hour * 60 + now_ct.minute
    if now_ct.weekday() >= 5:
        session, session_label = "closed", "Closed"
    elif hm < PREMARKET_START_MIN:
        session, session_label = "closed", "Closed"
    elif hm < RTH_OPEN_MIN:
        session, session_label = "premarket", "Pre-market"
    elif hm < RTH_CLOSE_MIN:
        session, session_label = "rth", "Market open"
    elif hm < AFTERHOURS_END_MIN:
        session, session_label = "afterhours", "After hours"
    else:
        session, session_label = "closed", "Closed"

    today_open = today_high = today_low = last_price = prev_close = None
    prev_close_verified = prev_close_from_cache = False
    expected_prev = _expected_prev_session(now_ct)
    price_as_of_ts = None
    rth_open = rth_high = rth_low = rth_close = None
    feed = None

    key = os.getenv("ALPACA_KEY_ID", "")
    secret = os.getenv("ALPACA_SECRET_KEY", "")
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret} if key and secret else None

    if headers:
        try:
            if ct:
                day_start = _dt.datetime(
                    session_date.year, session_date.month, session_date.day,
                    PREMARKET_START_MIN // 60, PREMARKET_START_MIN % 60, tzinfo=ct,
                ).astimezone(_dt.timezone.utc)
            else:
                day_start = _dt.datetime.now(_dt.timezone.utc).replace(hour=8, minute=0, second=0, microsecond=0, tzinfo=None)
            start_str = day_start.strftime("%Y-%m-%dT%H:%M:%SZ")
            end_str = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            bars = []
            for feed_name in _alpaca_bar_feeds():
                url = (
                    f"https://data.alpaca.markets/v2/stocks/{sym}/bars"
                    f"?timeframe=5Min&start={start_str}&end={end_str}&limit=10000&feed={feed_name}"
                )
                r = requests.get(url, headers=headers, timeout=TIMEOUT)
                if r.status_code != 200:
                    _note_alpaca_feed_status(feed_name, r.status_code)
                    continue
                bars = r.json().get("bars") or []
                if not bars:
                    continue
                feed = f"alpaca_{feed_name}"
                break
            if bars:
                ext_open, ext_high, ext_low = _ohlc_from_bars(bars, ct)
                rth_open, rth_high, rth_low = _ohlc_from_bars(
                    bars, ct, start_min=RTH_OPEN_MIN, end_min=RTH_CLOSE_MIN,
                )
                rth_close = _rth_close_from_bars(
                    bars, ct, start_min=RTH_OPEN_MIN, end_min=RTH_CLOSE_MIN,
                )
                use_rth = session in ("rth", "afterhours") or hm >= RTH_CLOSE_MIN
                if use_rth and rth_open is not None:
                    today_open, today_high, today_low = rth_open, rth_high, rth_low
                else:
                    today_open, today_high, today_low = ext_open, ext_high, ext_low
            # Previous close = the close of the expected previous session,
            # matched by bar date on newest-first 1Day bars (U17: ascending
            # limit=5 returned the oldest bars in the window, and bars[-2]
            # was two sessions back whenever today had no bar yet).
            prev_close = _alpaca_daily_close(sym, expected_prev, headers)
            if prev_close:
                prev_close_verified = True
            else:
                prev_close = _prev_close_cache_get(sym, expected_prev)
                if prev_close:
                    prev_close_from_cache = True
            # Free-tier Alpaca may return 401 on 1Day bars. Last resort: the
            # first 5-min bar's open. It is NOT the previous session's close
            # (it is this morning's first print), so it is never cached as one.
            if prev_close is None and bars:
                first_bar = bars[0]
                o = float(first_bar.get("o", 0))
                if o > 0:
                    prev_close = round(o, 4)
        except Exception as exc:
            LOGGER.debug("intraday alpaca %s: %s", sym, str(exc)[:80])

    if (today_open is None or today_high is None) and headers:
        d_o, d_h, d_l, d_c = _today_ohlc_from_alpaca_daily(sym, headers, session_date, ct)
        if d_o is not None:
            today_open, today_high, today_low = d_o, d_h, d_l
            if rth_open is None:
                rth_open, rth_high, rth_low = d_o, d_h, d_l
            if rth_close is None and d_c:
                rth_close = d_c
            feed = feed or "alpaca_1d"

    if today_open is None and rth_open is not None:
        today_open, today_high, today_low = rth_open, rth_high, rth_low

    trade, trade_ts = _alpaca_trade_quote(sym)
    if trade:
        trade, _rej = _reject_phantom(sym, trade)
    if trade:
        last_price = round(float(trade), 4)
        price_as_of_ts = trade_ts
        feed = feed or "alpaca_trade"

    if today_open is None or today_high is None:
        from core.circuit_breaker import _yfinance_cb
        if _yfinance_cb.allow():
            try:
                import yfinance as yf
                h = _ungated_yf_ticker(sym).history(period="1d", interval="5m")
                if h is not None and not h.empty:
                    today_open = round(float(h["Open"].iloc[0]), 4)
                    today_high = round(float(h["High"].max()), 4)
                    today_low = round(float(h["Low"].min()), 4)
                    yf_last = round(float(h["Close"].iloc[-1]), 4)
                    if not last_price:
                        last_price = yf_last
                        price_as_of_ts = _timestamp_to_epoch(h.index[-1])
                    feed = feed or "yfinance_5m"
                _yfinance_cb.record_success()
            except Exception:
                _yfinance_cb.record_failure()

    # prev_close fallback: try yfinance first, then Polygon, then Stooq.
    # Alpaca daily bars may not always return prev_close for thin symbols.
    if prev_close is None:
        from core.circuit_breaker import _yfinance_cb
        if _yfinance_cb.allow():
            try:
                import yfinance as yf
                fi = _ungated_yf_ticker(sym).fast_info
                pc = getattr(fi, "previous_close", None) or getattr(fi, "previousClose", None)
                if pc:
                    prev_close = round(float(pc), 4)
                    prev_close_verified = True
                _yfinance_cb.record_success()
            except Exception:
                _yfinance_cb.record_failure()
    if prev_close is None:
        pc = _polygon_spot(sym)
        if pc:
            prev_close = round(float(pc), 4)
            prev_close_verified = True

    # Persistent prev_close cache: when all live feeds are down (market closed,
    # breakers open), fall back to the cached close -- only if it is the
    # expected previous session's close. A value is cached under that session
    # only when it came from a source that reports the previous close.
    if prev_close is None:
        prev_close = _prev_close_cache_get(sym, expected_prev)
    elif prev_close > 0 and prev_close_verified and not prev_close_from_cache:
        _prev_close_cache_put(sym, expected_prev, prev_close)

    chg_abs = chg_pct = None
    if last_price:
        last_price, _rej = _reject_phantom(sym, last_price)
    if last_price and prev_close and prev_close > 0:
        chg_abs = round(last_price - prev_close, 4)
        chg_pct = round(chg_abs / prev_close * 100, 3)

    request_ts = int(time.time())
    out = {
        "symbol": sym,
        "as_of_ts": request_ts,
        "requested_at_ts": request_ts,
        "price_as_of_ts": price_as_of_ts,
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
    """VIX spot — gated by yfinance circuit breaker (P3 audit fix)."""
    from core.circuit_breaker import _yfinance_cb
    if not _yfinance_cb.allow():
        return None
    try:
        import yfinance as yf
        h = _ungated_yf_ticker("^VIX").history(period="1d")
        if not h.empty:
            _yfinance_cb.record_success()
            return float(h["Close"].iloc[-1])
    except Exception:
        _yfinance_cb.record_failure()
        note_suppressed()
    return None


def check_feeds():
    """Health check — can we price the target symbol right now?

    Probes all 5 feeds for visibility, but ``priceable`` only counts the
    live spot tiers (Alpaca, yfinance, IEX). Polygon is prev_close-only
    (its /prev endpoint returns yesterday's close) and Stooq is deprecated,
    so neither can actually price the symbol live.
    """
    probe = os.getenv("HEALTH_PROBE_SYMBOL", "WOLF")
    _al = _alpaca(probe) is not None
    _yf = _yfinance(probe) is not None
    _pg = _polygon_spot(probe) is not None
    _ix = _iex_spot(probe) is not None
    priceable = bool(_al or _yf or _ix)
    r = {
        "alpaca_stock": _al, "yfinance": _yf, "polygon": _pg,
        "iex": _ix, "probe_symbol": probe, "priceable": priceable,
    }
    working = sum(1 for v in (_al, _yf, _pg, _ix) if v)
    r["summary"] = (f"{probe} priceable ({working}/4 feeds)" if priceable
                    else f"{probe} NOT priceable ({working}/4 feeds)")
    return r
