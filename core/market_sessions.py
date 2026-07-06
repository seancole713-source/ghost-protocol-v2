"""core/market_sessions.py — batch quote/session reads with freshness truth (PR #136).

Born from the live-market audit: a caller sweeping /api/market/session/{sym}
43 times in a burst tripped the Alpaca and yfinance breakers. The fix is not
"more fetches" — it is cache-first serving with a bounded fresh-fetch budget
per request, partial results instead of failures, and per-symbol truth about
where each number came from and how old it is.

provider_state values:
  live         — fetched from the provider during this request (or cache <60s)
  cached       — served from the intraday cache, within TTL
  stale        — cache older than TTL returned anyway (better labeled-old than
                 silently missing); freshness_seconds says how old
  breaker_open — no usable cache and the Alpaca breaker is open
  unavailable  — no cache and the fetch failed
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List

LOGGER = logging.getLogger("ghost.market_sessions")

_LIVE_AGE_S = 60  # cache this young is indistinguishable from live


def _max_fresh_default() -> int:
    return max(1, int(os.getenv("MARKET_SESSIONS_MAX_FRESH", "8")))


def get_market_sessions(symbols: List[str], max_fresh: int | None = None) -> Dict[str, Any]:
    """Batch session snapshot. Never raises; every symbol gets a row."""
    from core.circuit_breaker import _alpaca_cb
    from core.prices import INTRADAY_QUOTE_TTL_S, _intraday_cache, get_intraday_session

    budget = max_fresh if max_fresh is not None else _max_fresh_default()
    now = time.time()
    syms = [s.strip().upper() for s in symbols if s and s.strip()][:60]

    # Fresh-fetch priority: symbols with no cache first, then oldest cache.
    def _cache_age(sym: str) -> float:
        c = _intraday_cache.get(sym)
        return (now - c[0]) if c else float("inf")

    fetch_order = sorted(syms, key=_cache_age, reverse=True)
    fetch_budgeted = set(fetch_order[:max(0, budget)])

    rows: Dict[str, Any] = {}
    fetched = 0
    for sym in syms:
        cached = _intraday_cache.get(sym)
        age = int(now - cached[0]) if cached else None
        try:
            if age is not None and age < _LIVE_AGE_S:
                row = dict(cached[1])
                row.update(provider_state="live", freshness_seconds=age)
            elif sym in fetch_budgeted and _alpaca_cb.allow():
                row = get_intraday_session(sym) or {}
                fetched += 1
                got_cache = _intraday_cache.get(sym)
                f_age = int(time.time() - got_cache[0]) if got_cache else 0
                state = "live" if f_age < _LIVE_AGE_S else "cached"
                row.update(provider_state=state, freshness_seconds=f_age)
            elif age is not None and age < INTRADAY_QUOTE_TTL_S:
                row = dict(cached[1])
                row.update(provider_state="cached", freshness_seconds=age)
            elif age is not None:
                row = dict(cached[1])
                row.update(provider_state="stale", freshness_seconds=age)
            elif not _alpaca_cb.allow():
                row = {"provider_state": "breaker_open", "freshness_seconds": None}
            else:
                row = {"provider_state": "unavailable", "freshness_seconds": None}
        except Exception as exc:
            LOGGER.warning("market_sessions %s: %s", sym, str(exc)[:100])
            if cached:
                row = dict(cached[1])
                row.update(provider_state="stale", freshness_seconds=age)
            else:
                row = {"provider_state": "unavailable", "freshness_seconds": None,
                       "error": str(exc)[:80]}
        row["symbol"] = sym
        row["price_source"] = row.get("feed")
        # PR #137 (audit fix): cached rows written while the trade fetch failed
        # carry price=null even though the session's RTH truth exists. Mirror
        # the single-symbol endpoint's semantics — but WITHOUT provider calls
        # (get_price could hit feeds; the whole point here is bounded fetches).
        # rth_close is the most recent regular-hours price in the cached row.
        if not row.get("price"):
            fb = row.get("rth_close") or row.get("today_open")
            if fb:
                row["price"] = fb
                row["price_source"] = "rth_close_fallback" if row.get("rth_close") else "today_open_fallback"
        if (row.get("price") and row.get("previous_close")
                and row["previous_close"] > 0 and row.get("change_pct") is None):
            chg = round(row["price"] - row["previous_close"], 4)
            row["change_abs"] = chg
            row["change_pct"] = round(chg / row["previous_close"] * 100, 3)
        has_ohlc = row.get("today_open") is not None or row.get("today_high") is not None
        row["ok"] = bool(row.get("price") is not None or has_ohlc)
        rows[sym] = row

    return {
        "ok": True,
        "count": len(rows),
        "fresh_fetches": fetched,
        "fresh_budget": budget,
        "note": ("cache-first: at most fresh_budget symbols hit providers per call; "
                 "the rest serve from cache with provider_state + freshness_seconds truth"),
        "sessions": rows,
        "as_of_ts": int(time.time()),
    }
