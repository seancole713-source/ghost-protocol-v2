"""Polygon (branded Massive in 2026) -- stocks REST, per its public docs.

Auth is the apiKey query parameter. Capabilities probed, one request each:
  universe     GET /v3/reference/tickers?market=stocks&active=true
  daily_all    GET /v2/aggs/grouped/locale/us/market/stocks/{date}
  snapshot     GET /v2/snapshot/locale/us/markets/stocks/tickers
  minute       GET /v2/aggs/ticker/{T}/range/1/minute/{from}/{to}
  splits       GET /v3/reference/splits
  dividends    GET /v3/reference/dividends
  news         GET /v2/reference/news
Observed history: grouped-daily returned 403 for this account on 2026-09-05 and
200 with 12,626 tickers on 2026-09-22 (edge probe). The all-tickers SNAPSHOT
was still 403 ("upgrade your plan") on 2026-09-22. Trust the latest probe.
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from edge.providers import base as B

NAME = "polygon"


def _base_url() -> str:
    return (os.getenv("POLYGON_BASE_URL") or "https://api.polygon.io").rstrip("/")


def _key() -> str:
    return (os.getenv("POLYGON_API_KEY") or "").strip()


def _last_weekday(today: date) -> date:
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _probe_one(get, cap: str, path: str, params: Dict[str, Any], rows_key: str = "results",
               ts_field: Optional[str] = None, ts_scale: float = 1.0) -> B.Probe:
    key = _key()
    if not key:
        return B.Probe(cap, NAME, B.NO_KEY, note="POLYGON_API_KEY not set")
    r, ms, err = B.timed_get(get, _base_url() + path, params={**params, "apiKey": key})
    if r is None:
        return B.Probe(cap, NAME, B.ERROR, latency_ms=ms, note=err)
    status = B.classify(r.status_code)
    if status != B.OK:
        note = ""
        try:
            note = str((r.json() or {}).get("message") or (r.json() or {}).get("error") or "")[:160]
        except Exception:  # noqa: BLE001
            pass
        return B.Probe(cap, NAME, status, http_status=r.status_code, latency_ms=ms, note=note)
    try:
        payload = r.json()
    except Exception:  # noqa: BLE001
        return B.Probe(cap, NAME, B.ERROR, http_status=r.status_code, latency_ms=ms, note="non-JSON body")
    rows = payload.get(rows_key) if isinstance(payload, dict) else None
    n = len(rows) if isinstance(rows, list) else 0
    newest = None
    if ts_field and n:
        vals = [row.get(ts_field) for row in rows if isinstance(row, dict)]
        vals = [v for v in vals if isinstance(v, (int, float))]
        newest = int(max(vals) * ts_scale) if vals else None
    return B.Probe(cap, NAME, B.OK if n else B.EMPTY, http_status=r.status_code,
                   latency_ms=ms, rows=n, newest_ts=newest)


def probe(get: Optional[B.HttpGet] = None, *, today: Optional[date] = None,
          sample_symbol: str = "AAPL", pace_s: Optional[float] = None,
          sleep=None) -> List[B.Probe]:
    """One request per capability, PACED.

    A free-tier key allows ~5 calls/minute, and this key is shared with Ghost's
    live pricing. Seven back-to-back probe calls would rate-limit Ghost itself
    for a minute -- so production spaces them (EDGE_PROBE_POLYGON_PACE_S).
    """
    import time as _time
    get = get or B.default_get()
    sleep = sleep or _time.sleep
    if pace_s is None:
        pace_s = float(os.getenv("EDGE_PROBE_POLYGON_PACE_S", "13"))
    today = today or date.today()
    day = _last_weekday(today)
    calls = [
        lambda: _probe_one(get, B.UNIVERSE, "/v3/reference/tickers",
                           {"market": "stocks", "active": "true", "limit": 1000}),
        lambda: _probe_one(get, B.DAILY_ALL, f"/v2/aggs/grouped/locale/us/market/stocks/{day.isoformat()}",
                           {"adjusted": "true"}, ts_field="t", ts_scale=0.001),
        lambda: _probe_one(get, B.SNAPSHOT_ALL, "/v2/snapshot/locale/us/markets/stocks/tickers",
                           {}, rows_key="tickers", ts_field="updated", ts_scale=1e-9),
        lambda: _probe_one(get, B.MINUTE_BARS,
                           f"/v2/aggs/ticker/{sample_symbol}/range/1/minute/{day.isoformat()}/{day.isoformat()}",
                           {"adjusted": "true", "sort": "asc", "limit": 50000}, ts_field="t", ts_scale=0.001),
        lambda: _probe_one(get, B.SPLITS, "/v3/reference/splits", {"limit": 10}),
        lambda: _probe_one(get, B.DIVIDENDS, "/v3/reference/dividends", {"limit": 10}),
        lambda: _probe_one(get, B.NEWS, "/v2/reference/news", {"limit": 10}),
    ]
    out: List[B.Probe] = []
    for i, call in enumerate(calls):
        if i and pace_s > 0 and _key():
            sleep(pace_s)
        res = call()
        if res.status == B.RATE_LIMITED:
            # The shared key's per-minute allowance, not a verdict about the data (the
            # jobs themselves wait it out) -- so the probe waits once too before reporting.
            sleep(20.0)
            res = call()
            if res.status == B.RATE_LIMITED:
                res.note = (res.note + " (still 429 after one 20s wait)").strip()
        out.append(res)
    return out


def _get_patiently(get: B.HttpGet, url: str, params: Dict[str, Any], *, timeout: int = 30, sleep=None):
    """GET that waits out a 429 (the shared key's per-minute allowance) twice.

    Measured 2026-09-22: this account's Polygon key answers 429 "exceeded the
    maximum requests per minute" whenever Ghost's signal engine has just used
    it. A 429 is contention, never a verdict about the data.
    """
    import time as _time
    sleep = sleep or _time.sleep
    for attempt in range(3):
        r = get(url, params=params, timeout=timeout)
        if getattr(r, "status_code", 200) != 429 or attempt == 2:
            return r
        try:
            wait = float((getattr(r, "headers", None) or {}).get("Retry-After") or 15)
        except (TypeError, ValueError):
            wait = 15.0
        sleep(min(30.0, max(1.0, wait)))
    return r


def grouped_daily(get: B.HttpGet, day: date, *, sleep=None) -> List[Dict[str, Any]]:
    """Every US stock's daily bar for one session: [{T, o, h, l, c, v, vw, t}]."""
    r = _get_patiently(get, _base_url() + f"/v2/aggs/grouped/locale/us/market/stocks/{day.isoformat()}",
                       {"adjusted": "true", "apiKey": _key()}, sleep=sleep)
    r.raise_for_status()
    return list((r.json() or {}).get("results") or [])


def minute_bars(get: B.HttpGet, symbol: str, day: date) -> List[tuple]:
    """Minute bars as edge.resolver Bars: (ts_s, o, h, l, c, v), ts = bar start."""
    r = _get_patiently(get, _base_url() + f"/v2/aggs/ticker/{symbol.upper()}/range/1/minute/{day}/{day}",
                       {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": _key()})
    r.raise_for_status()
    out = []
    for b in (r.json() or {}).get("results") or []:
        out.append((int(b["t"] // 1000), float(b["o"]), float(b["h"]), float(b["l"]),
                    float(b["c"]), float(b.get("v") or 0)))
    return out
