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

import os
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Dict, List, Optional

from edge.providers import base as B

NAME = "alpaca"


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
    today = today or date.today()
    y = today - timedelta(days=1)
    while y.weekday() >= 5:
        y -= timedelta(days=1)
    start = datetime.combine(y, dtime(13, 30), tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    end = datetime.combine(y, dtime(20, 0), tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
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


def movers(get: B.HttpGet, top: int = 50) -> Dict[str, List[dict]]:
    """Whole-market top gainers and losers: {'gainers': [...], 'losers': [...]}."""
    r = get(_base_url() + "/v1beta1/screener/stocks/movers", params={"top": top},
            headers=_headers(), timeout=15)
    r.raise_for_status()
    p = r.json() or {}
    return {"gainers": list(p.get("gainers") or []), "losers": list(p.get("losers") or []),
            "last_updated": p.get("last_updated")}


# ----------------------------------------------------------- data calls --
# Used by edge.pipeline. Free-plan facts that shape them (Alpaca docs):
#   * feed=sip is allowed for data older than 15 minutes -- enough for daily
#     bars and for grading yesterday's/today's minute bars after the close;
#   * real-time quotes on the free plan are IEX only -- one exchange's view,
#     which is why premarket prices are treated as degraded evidence.

def _get_json(get: B.HttpGet, path: str, params: dict) -> dict:
    r = get(_base_url() + path, params=params, headers=_headers(), timeout=20)
    r.raise_for_status()
    return r.json() or {}


def bars_multi(get: B.HttpGet, symbols: List[str], *, timeframe: str, start: str, end: Optional[str] = None,
               feed: str = "sip", max_pages: int = 10) -> Dict[str, List[dict]]:
    """{symbol: [bar, ...]} for many symbols, following next_page_token."""
    out: Dict[str, List[dict]] = {s: [] for s in symbols}
    if not symbols:
        return out
    params = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": start,
              "feed": feed, "limit": 10000, "adjustment": "raw"}
    if end:
        params["end"] = end
    for _ in range(max_pages):
        p = _get_json(get, "/v2/stocks/bars", params)
        for sym, rows in (p.get("bars") or {}).items():
            out.setdefault(sym, []).extend(rows or [])
        tok = p.get("next_page_token")
        if not tok:
            break
        params = {**params, "page_token": tok}
    return out


def snapshots(get: B.HttpGet, symbols: List[str], *, feed: str = "iex") -> Dict[str, dict]:
    if not symbols:
        return {}
    return _get_json(get, "/v2/stocks/snapshots", {"symbols": ",".join(symbols), "feed": feed})


def news(get: B.HttpGet, symbols: List[str], *, start: str, limit: int = 50, max_pages: int = 4) -> List[dict]:
    if not symbols:
        return []
    params = {"symbols": ",".join(symbols), "start": start, "limit": limit, "sort": "desc"}
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
