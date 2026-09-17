"""core/market_sessions.py — batch quote/session reads with freshness truth (PR #136).

Born from the live-market audit: a caller sweeping /api/market/session/{sym}
43 times in a burst tripped the Alpaca and yfinance breakers. The fix is not
"more fetches" — it is cache-first serving with a bounded fresh-fetch budget
per request, partial results instead of failures, and per-symbol truth about
where each number came from and how old it is.

provider_state describes observation freshness, never cache insertion age.
Cache age remains separate in cache_age_seconds; reference-only fallbacks and
missing observation clocks cannot be presented as live quotes.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List

LOGGER = logging.getLogger("ghost.market_sessions")

_LIVE_AGE_S = 60  # maximum age for the live observation label


def _max_fresh_default() -> int:
    return max(1, int(os.getenv("MARKET_SESSIONS_MAX_FRESH", "8")))


def get_market_sessions(symbols: List[str], max_fresh: int | None = None) -> Dict[str, Any]:
    """Batch session snapshot. Never raises; every symbol gets a row."""
    from core.circuit_breaker import _alpaca_cb
    from core.prices import (
        INTRADAY_QUOTE_TTL_S, _intraday_cache, _timestamp_to_epoch, get_intraday_session,
    )

    budget = max_fresh if max_fresh is not None else _max_fresh_default()
    now = time.time()
    syms = [s.strip().upper() for s in symbols if s and s.strip()][:60]

    # Fresh-fetch priority: symbols with no cache first, then oldest cache.
    def _cache_age(sym: str) -> float:
        c = _intraday_cache.get(sym)
        if not c:
            return float("inf")
        observed = _timestamp_to_epoch(c[1].get("price_as_of_ts"))
        if not observed or observed > now:
            return float("inf")
        return max(now - c[0], now - observed)

    fetch_order = sorted(syms, key=_cache_age, reverse=True)
    fetch_budgeted = set(fetch_order[:max(0, budget)])

    rows: Dict[str, Any] = {}
    fetched = 0
    for sym in syms:
        cached = _intraday_cache.get(sym)
        age = int(now - cached[0]) if cached else None
        try:
            if age is not None and _cache_age(sym) < _LIVE_AGE_S:
                row = dict(cached[1])
                row.update(provider_state="live", freshness_seconds=age)
            elif sym in fetch_budgeted and _alpaca_cb.allow():
                row = dict(get_intraday_session(sym) or {})
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
        row["cache_age_seconds"] = row.get("freshness_seconds")
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
                row["price_as_of_ts"] = None
                row["quote_status"] = "reference_only"
                row["price_source"] = "rth_close_fallback" if row.get("rth_close") else "today_open_fallback"
        if (row.get("price") and row.get("previous_close")
                and row["previous_close"] > 0 and row.get("change_pct") is None):
            chg = round(row["price"] - row["previous_close"], 4)
            row["change_abs"] = chg
            row["change_pct"] = round(chg / row["previous_close"] * 100, 3)
        has_ohlc = row.get("today_open") is not None or row.get("today_high") is not None
        row["ok"] = bool(row.get("price") is not None or has_ohlc)
        if not row["ok"] and row.get("provider_state") in ("live", "cached", "stale"):
            # PR #140: a fresh cache entry can still be an empty failed-fetch row
            # because core.prices caches the session shell even when no trade/OHLC
            # was available. Do not call that "live"; provider_state should tell
            # the operator whether there is usable market truth.
            row["provider_state"] = "breaker_open" if not _alpaca_cb.allow() else "unavailable"
            row["state_note"] = "no usable price/OHLC in cached session row"
        observed = _timestamp_to_epoch(row.get("price_as_of_ts"))
        observation_age = int(time.time()) - observed if observed and observed > 0 else None
        row["freshness_seconds"] = observation_age
        if row.get("quote_status") == "reference_only":
            row["provider_state"] = "reference_only"
            row["freshness_seconds"] = None
        elif observation_age is None:
            row["quote_status"] = "unknown"
            if row["ok"]:
                row["provider_state"] = "unavailable"
        elif observation_age < 0:
            row["quote_status"] = "future_timestamp"
            row["provider_state"] = "unavailable"
        elif observation_age > INTRADAY_QUOTE_TTL_S or row.get("data_stale") is True:
            row["quote_status"] = "stale"
            row["provider_state"] = "stale"
        elif row["ok"]:
            row["quote_status"] = "fresh"
            row["provider_state"] = "live" if observation_age < _LIVE_AGE_S else "cached"
        else:
            row["quote_status"] = "unknown"
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
