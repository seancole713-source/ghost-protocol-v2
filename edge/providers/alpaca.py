"""Alpaca market data v2 -- per its public docs.

Header auth (APCA-API-KEY-ID / APCA-API-SECRET-KEY). The same key's ENTITLEMENT
differs by feed: the free plan documents IEX real-time and SIP only >15 min
delayed; real-time SIP needs the paid plan (Algo Trader Plus in Alpaca's docs --
verify the current price with Alpaca). The probe asks for `feed=sip` explicitly
so a refusal names itself instead of silently degrading to IEX.

Probed:
  quotes.sip   GET /v2/stocks/{T}/trades/latest?feed=sip
  quotes.iex   GET /v2/stocks/{T}/trades/latest?feed=iex
  minute       GET /v2/stocks/{T}/bars?timeframe=1Min&feed=sip  (yesterday)
  movers       GET /v1beta1/screener/stocks/movers?top=50       (whole market)
  news         GET /v1beta1/news?limit=10
Also documented: bracket orders do NOT support extended hours.
"""
from __future__ import annotations

import logging
import os
import time as _time
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from edge.providers import base as B

NAME = "alpaca"
LOG = logging.getLogger("edge.providers.alpaca")


def _base_url() -> str:
    return (os.getenv("ALPACA_DATA_URL") or "https://data.alpaca.markets").rstrip("/")


def _headers() -> Optional[Dict[str, str]]:
    kid = (os.getenv("ALPACA_KEY_ID") or os.getenv("APCA_API_KEY_ID") or "").strip()
    sec = (os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY") or "").strip()
    if not kid or not sec:
        return None
    return {"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": sec}


def _iso_ts(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _probe_one(get, cap: str, path: str, params: dict, extract) -> B.Probe:
    h = _headers()
    if h is None:
        return B.Probe(cap, NAME, B.NO_KEY, note="ALPACA_KEY_ID / ALPACA_SECRET_KEY not set")
    r, ms, err = B.timed_get(get, _base_url() + path, params=params, headers=h)
    if r is None:
        return B.Probe(cap, NAME, B.ERROR, latency_ms=ms, note=err)
    status = B.classify(r.status_code)
    if status != B.OK:
        note = ""
        try:
            note = str((r.json() or {}).get("message") or "")[:160]
        except Exception:  # noqa: BLE001
            pass
        return B.Probe(cap, NAME, status, http_status=r.status_code, latency_ms=ms, note=note)
    try:
        n, newest = extract(r.json() or {})
    except Exception as exc:  # noqa: BLE001
        return B.Probe(cap, NAME, B.ERROR, http_status=r.status_code, latency_ms=ms,
                       note=f"unexpected payload: {type(exc).__name__}")
    return B.Probe(cap, NAME, B.OK if n else B.EMPTY, http_status=r.status_code,
                   latency_ms=ms, rows=n, newest_ts=newest)


def probe(get: Optional[B.HttpGet] = None, *, today: Optional[date] = None,
          sample_symbol: str = "AAPL") -> List[B.Probe]:
    get = get or B.default_get()
    today = today or B.et_today()
    y = today - timedelta(days=1)
    while y.weekday() >= 5:
        y -= timedelta(days=1)
    # The regular session on the exchange clock (13:30-20:00 UTC in summer, 14:30-21:00 in winter).
    et = ZoneInfo("America/New_York")
    start = datetime.combine(y, dtime(9, 30), tzinfo=et).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    end = datetime.combine(y, dtime(16, 0), tzinfo=et).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    trade = lambda p: (1 if p.get("trade") else 0, _iso_ts((p.get("trade") or {}).get("t")))
    return [
        _probe_one(get, B.QUOTE_SIP, f"/v2/stocks/{sample_symbol}/trades/latest", {"feed": "sip"}, trade),
        _probe_one(get, B.QUOTE_IEX, f"/v2/stocks/{sample_symbol}/trades/latest", {"feed": "iex"}, trade),
        _probe_one(get, B.MINUTE_BARS, f"/v2/stocks/{sample_symbol}/bars",
                   {"timeframe": "1Min", "start": start, "end": end, "feed": "sip", "limit": 1000},
                   lambda p: (len(p.get("bars") or []),
                              max((_iso_ts(b.get("t")) or 0 for b in p.get("bars") or []), default=None))),
        _probe_one(get, B.MOVERS, "/v1beta1/screener/stocks/movers", {"top": 50},
                   lambda p: (len(p.get("gainers") or []) + len(p.get("losers") or []),
                              _iso_ts(p.get("last_updated")))),
        _probe_one(get, B.NEWS, "/v1beta1/news", {"limit": 10},
                   lambda p: (len(p.get("news") or []),
                              max((_iso_ts(n.get("created_at")) or 0 for n in p.get("news") or []), default=None))),
    ]


# ------------------------------------------------------------ 429 retry --
# The data key's per-minute allowance is SHARED with Ghost's core scan. One 429
# on the radar's bars call (Task #64, 12:09 CT) dropped a whole intraday tick.
# A 429 is contention, not a verdict about the data: wait it out, briefly and
# boundedly, then fail with an error that names the rate limit.
RETRY_429_MAX = 2               # retries after the first attempt
RETRY_429_BUDGET_S = 10.0       # total seconds one call may spend waiting out 429s
RETRY_429_BACKOFF_S = (1.0, 3.0)
_sleep = _time.sleep            # patched by tests


class RateLimited(RuntimeError):
    """Alpaca kept answering 429 after the bounded retries: a named source error."""

    def __init__(self, path: str, attempts: int, waited_s: float) -> None:
        self.path, self.attempts, self.waited_s = path, attempts, waited_s
        super().__init__(f"alpaca rate limited (429) on {path} after {attempts} attempts "
                         f"({waited_s:.1f}s waited); the limit is shared with the core scan")


def _retry_after_s(r) -> Optional[float]:
    try:
        v = (getattr(r, "headers", None) or {}).get("Retry-After")
        return max(0.0, float(v)) if v is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def _get_with_429_retry(get: B.HttpGet, path: str, *, params: dict, timeout: int):
    """GET that retries a 429 at most RETRY_429_MAX times, honoring Retry-After, never
    waiting more than RETRY_429_BUDGET_S in total. A persistent 429 raises RateLimited."""
    waited = 0.0
    attempts = 0
    for attempt in range(RETRY_429_MAX + 1):
        attempts = attempt + 1
        r = get(_base_url() + path, params=params, headers=_headers(), timeout=timeout)
        if getattr(r, "status_code", 200) != 429:
            return r
        if attempt == RETRY_429_MAX:
            break
        ra = _retry_after_s(r)
        wait = ra if ra is not None else RETRY_429_BACKOFF_S[min(attempt, len(RETRY_429_BACKOFF_S) - 1)]
        wait = min(wait, RETRY_429_BUDGET_S - waited)
        if wait <= 0:
            break
        LOG.warning("alpaca 429 on %s; retry %d/%d in %.1fs", path, attempt + 1, RETRY_429_MAX, wait)
        _sleep(wait)
        waited += wait
    raise RateLimited(path, attempts, waited)


def movers(get: B.HttpGet, top: int = 50) -> Dict[str, List[dict]]:
    """Whole-market top gainers and losers: {'gainers': [...], 'losers': [...]}."""
    r = _get_with_429_retry(get, "/v1beta1/screener/stocks/movers", params={"top": top}, timeout=15)
    r.raise_for_status()
    p = r.json() or {}
    return {"gainers": list(p.get("gainers") or []), "losers": list(p.get("losers") or []),
            "last_updated": p.get("last_updated")}


def most_actives(get: B.HttpGet, top: int = 100) -> Dict[str, Any]:
    """Whole-market most active stocks by volume: {'most_actives': [{'symbol', 'volume', ...}], ...}."""
    r = _get_with_429_retry(get, "/v1beta1/screener/stocks/most-actives",
                            params={"by": "volume", "top": top}, timeout=15)
    r.raise_for_status()
    p = r.json() or {}
    return {"most_actives": list(p.get("most_actives") or []), "last_updated": p.get("last_updated")}


# ----------------------------------------------------------- data calls --
# Used by edge.pipeline. Free-plan facts that shape them (Alpaca docs):
#   * feed=sip is allowed for data older than 15 minutes -- enough for daily
#     bars and for grading yesterday's/today's minute bars after the close;
#   * real-time quotes on the free plan are IEX only -- one exchange's view,
#     which is why premarket prices are treated as degraded evidence.

def _get_json(get: B.HttpGet, path: str, params: dict) -> dict:
    r = _get_with_429_retry(get, path, params=params, timeout=20)
    r.raise_for_status()
    return r.json() or {}


def bars_pages(get: B.HttpGet, symbols: List[str], *, timeframe: str, start: str, end: Optional[str] = None,
               feed: str = "sip", max_pages: int = 40,
               adjustment: str = "raw") -> Tuple[Dict[str, List[dict]], bool]:
    """({symbol: [bar, ...]}, complete). complete is False when `max_pages` ran out with a
    next_page_token still pending: the answer is TRUNCATED -- the symbols paged last (Alpaca
    pages symbol by symbol) are short or missing -- and a caller must not keep it as whole.

    `adjustment` is Alpaca's corporate-action basis: "raw" (default) is prices and volume as
    traded; "split" / "dividend" / "all" restate history for actions known TODAY."""
    out: Dict[str, List[dict]] = {s: [] for s in symbols}
    if not symbols:
        return out, True
    params = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": start,
              "feed": feed, "limit": 10000, "adjustment": adjustment}
    if end:
        params["end"] = end
    for _ in range(max_pages):
        p = _get_json(get, "/v2/stocks/bars", params)
        for sym, rows in (p.get("bars") or {}).items():
            out.setdefault(sym, []).extend(rows or [])
        tok = p.get("next_page_token")
        if not tok:
            return out, True
        params = {**params, "page_token": tok}
    LOG.warning("alpaca bars page cap hit: %d pages of %s %s bars for %d symbols (%s..%s); "
                "the answer is truncated", max_pages, feed, timeframe, len(symbols), symbols[0], symbols[-1])
    return out, False


def bars_multi(get: B.HttpGet, symbols: List[str], *, timeframe: str, start: str, end: Optional[str] = None,
               feed: str = "sip", max_pages: int = 40, adjustment: str = "raw") -> Dict[str, List[dict]]:
    """{symbol: [bar, ...]} for many symbols, following next_page_token (a hit page cap is
    logged; use bars_pages to learn whether the answer is complete)."""
    return bars_pages(get, symbols, timeframe=timeframe, start=start, end=end, feed=feed,
                      max_pages=max_pages, adjustment=adjustment)[0]


def snapshots(get: B.HttpGet, symbols: List[str], *, feed: str = "iex") -> Dict[str, dict]:
    if not symbols:
        return {}
    return _get_json(get, "/v2/stocks/snapshots", {"symbols": ",".join(symbols), "feed": feed})


def news(get: B.HttpGet, symbols: List[str], *, start: str, end: Optional[str] = None, limit: int = 50,
         max_pages: int = 8) -> List[dict]:
    """Newest first. A PAST window must pass `end`: without it Alpaca pages back from NOW, and
    the capped pages hold only recent articles -- none from the session being studied."""
    if not symbols:
        return []
    params = {"symbols": ",".join(symbols), "start": start, "limit": limit, "sort": "desc"}
    if end:
        params["end"] = end
    out: List[dict] = []
    for _ in range(max_pages):
        p = _get_json(get, "/v1beta1/news", params)
        out.extend(p.get("news") or [])
        tok = p.get("next_page_token")
        if not tok:
            break
        params = {**params, "page_token": tok}
    return out


def iso_to_epoch(s: Optional[str]) -> Optional[int]:
    return _iso_ts(s)
