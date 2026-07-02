"""core/market_history.py - Railway-friendly daily OHLCV history.

PR #88 (Data Coverage Upgrade).

Why this exists
---------------
``core/super_ghost._fetch_live_snapshot`` previously sourced *all* price
history from yfinance (``yf_history``). Yahoo blocks Railway's datacenter IPs,
so on production those calls return nothing - which silently killed every
price-action checklist item (30d perf, 52w range, volume, RVOL, relative
strength, moving averages, support/resistance) and, by knock-on, the
price-derived risk items (risk/reward, stop, target). That is the real reason
live coverage sat at ~7/25 while the deterministic ``snapshot`` path scored
25/25 in tests.

The fix is not new scorers - those already exist and are correct. The fix is
feeding them from a source that actually works on Railway. PR #87 already
proved that Alpaca's bars API returns real OHLC from the production host
(``/api/market/session`` is live and correct), so this module reuses that same
Alpaca path for *daily* history, with yfinance as a secondary fallback for
local/dev where Alpaca keys may be absent.

Design rules (consistent with the rest of Ghost):
- Never raise; a failed fetch returns ``[]`` so the caller just marks the
  affected checks unknown (coverage drops, nothing is faked).
- Circuit-breaker gated for both Alpaca and yfinance.
- Cached per (symbol, period) for a short TTL so one report build does not
  hammer the provider for the symbol + SPY + QQQ + sector ETF.
- Output rows are dicts compatible with ``core.super_ghost._history_points``:
  ``{"ts", "open", "high", "low", "close", "volume"}`` oldest -> newest.
"""
from __future__ import annotations

import logging
import math
import os
import time
from typing import Any, Dict, List, Optional

import requests

LOGGER = logging.getLogger("ghost.market_history")

_TIMEOUT = float(os.getenv("MARKET_HISTORY_TIMEOUT_S", "10.0"))
_CACHE_TTL_S = int(os.getenv("MARKET_HISTORY_TTL_S", "1800"))  # 30 min
_ALPACA_DATA_BASE = "https://data.alpaca.markets/v2/stocks"

# Cache: {(symbol, days): (ts, rows)}
_history_cache: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}


def _finite(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> Optional[int]:
    f = _finite(v)
    return int(f) if f is not None else None


def _alpaca_headers() -> Optional[Dict[str, str]]:
    key = os.getenv("ALPACA_KEY_ID", "")
    secret = os.getenv("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        return None
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def _alpaca_daily_bars(symbol: str, days: int) -> List[Dict[str, Any]]:
    """Daily OHLCV bars from Alpaca - the Railway-proven price source (PR #87).

    Tries the SIP feed first, then IEX (free tier). Handles pagination. Returns
    ``[]`` on any failure so callers degrade to ``unknown`` rather than fake.
    """
    from core.circuit_breaker import _alpaca_cb

    headers = _alpaca_headers()
    if not headers:
        return []
    if not _alpaca_cb.allow():
        return []

    import datetime as _dt

    end = _dt.datetime.now(_dt.timezone.utc)
    # Pad calendar days generously (markets closed ~30% of days) so we reliably
    # capture ``days`` trading sessions (e.g. ~365 sessions needs ~530 cal days).
    start = end - _dt.timedelta(days=int(days * 1.6) + 10)
    start_str = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = end.strftime("%Y-%m-%dT%H:%M:%SZ")

    from core.prices import _alpaca_bar_feeds, _note_alpaca_feed_status
    for feed_name in _alpaca_bar_feeds():
        rows: List[Dict[str, Any]] = []
        page_token: Optional[str] = None
        ok = True
        for _ in range(12):  # hard page cap (safety)
            url = (
                f"{_ALPACA_DATA_BASE}/{symbol.upper()}/bars"
                f"?timeframe=1Day&start={start_str}&end={end_str}&limit=10000&feed={feed_name}"
                + (f"&page_token={page_token}" if page_token else "")
            )
            try:
                r = requests.get(url, headers=headers, timeout=_TIMEOUT)
            except Exception as exc:  # network/timeout - count as breaker failure
                _alpaca_cb.record_failure()
                LOGGER.debug("alpaca daily %s (%s): %s", symbol, feed_name, str(exc)[:100])
                ok = False
                break
            if r.status_code != 200:
                # 403 = feed not authorised on free tier (expected) -> try next feed.
                # 5xx/429 = real outage -> breaker failure.
                if r.status_code >= 500 or r.status_code == 429:
                    _alpaca_cb.record_failure()
                _note_alpaca_feed_status(feed_name, r.status_code)
                ok = False
                break
            body = r.json() or {}
            for bar in body.get("bars") or []:
                close = _finite(bar.get("c"))
                if close is None:
                    continue
                rows.append({
                    "ts": bar.get("t"),
                    "open": _finite(bar.get("o")),
                    "high": _finite(bar.get("h")),
                    "low": _finite(bar.get("l")),
                    "close": close,
                    "volume": _int(bar.get("v")),
                })
            page_token = body.get("next_page_token")
            if not page_token:
                break
        if ok and rows:
            _alpaca_cb.record_success()
            return rows[-days:] if len(rows) > days else rows
    return []


def _yfinance_daily_bars(symbol: str, days: int) -> List[Dict[str, Any]]:
    """Fallback daily bars via the breaker-gated yfinance wrapper."""
    try:
        from core.yfinance_client import yf_history
    except Exception:
        return []
    period = "1y" if days <= 270 else "2y"
    df = yf_history(symbol, period, "1d")
    if df is None or getattr(df, "empty", True):
        return []
    rows: List[Dict[str, Any]] = []
    try:
        for ix, row in df.iterrows():
            close = _finite(row.get("Close"))
            if close is None:
                continue
            rows.append({
                "ts": int(ix.timestamp()) if hasattr(ix, "timestamp") else str(ix),
                "open": _finite(row.get("Open")),
                "high": _finite(row.get("High")),
                "low": _finite(row.get("Low")),
                "close": close,
                "volume": _int(row.get("Volume")),
            })
    except Exception:
        return []
    return rows[-days:] if len(rows) > days else rows


def _period_for_days(days: int) -> str:
    """Map a trading-day count to the period strings ``_fetch_ohlcv`` accepts."""
    if days <= 90:
        return "3m"
    if days <= 180:
        return "6m"
    if days <= 365:
        return "1y"
    return "2y"


def _signal_engine_ohlcv(symbol: str, days: int) -> List[Dict[str, Any]]:
    """Delegate to the production-proven multi-tier OHLCV chain.

    ``core.signal_engine._fetch_ohlcv`` already powers live model training on
    Railway (Alpaca SIP -> IEX -> Polygon -> yfinance -> Stooq) and returns rows
    in the exact ``{ts, open, high, low, close, volume}`` shape we need. Never
    raises; returns ``[]`` on any problem so the caller can fall through.
    """
    try:
        from core.signal_engine import _fetch_ohlcv
    except Exception:
        return []
    try:
        rows = _fetch_ohlcv(symbol, "stock", period=_period_for_days(days), interval="1d")
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.debug("signal_engine ohlcv %s: %s", symbol, str(exc)[:100])
        return []
    if not rows:
        return []
    out: List[Dict[str, Any]] = []
    for r in rows:
        close = _finite(r.get("close"))
        if close is None:
            continue
        out.append({
            "ts": r.get("ts"),
            "open": _finite(r.get("open")),
            "high": _finite(r.get("high")),
            "low": _finite(r.get("low")),
            "close": close,
            "volume": _int(r.get("volume")),
        })
    return out[-days:] if len(out) > days else out


def get_daily_history(symbol: str, days: int = 400) -> List[Dict[str, Any]]:
    """Best-effort daily OHLCV history (oldest -> newest), Railway-friendly.

    Source order:
      1. ``core.signal_engine._fetch_ohlcv`` - the production-proven multi-tier
         chain (Alpaca SIP -> IEX -> Polygon -> yfinance -> Stooq) that the live
         model trainer already relies on. This is the most reliable path on
         Railway, so we try it first.
      2. Direct Alpaca daily bars (this module) - kept as a secondary so the
         module is still useful if signal_engine is unavailable.
      3. yfinance (dev/local).
    Returns ``[]`` if every source fails; callers must treat empty as "unknown".
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return []
    now = time.time()
    ck = f"{sym}:{days}"
    cached = _history_cache.get(ck)
    if cached and (now - cached[0]) < _CACHE_TTL_S:
        return [dict(r) for r in cached[1]]

    rows = _signal_engine_ohlcv(sym, days)
    if not rows:
        rows = _alpaca_daily_bars(sym, days)
    if not rows:
        rows = _yfinance_daily_bars(sym, days)

    if rows:
        _history_cache[ck] = (now, [dict(r) for r in rows])
    return rows


def history_source_status() -> Dict[str, Any]:
    """Lightweight status for health/diagnostics panels."""
    return {
        "alpaca_keyed": _alpaca_headers() is not None,
        "cache_entries": len(_history_cache),
        "ttl_s": _CACHE_TTL_S,
    }


def clear_cache() -> None:
    """Test/diagnostic helper."""
    _history_cache.clear()
