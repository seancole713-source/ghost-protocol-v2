"""core/news_ingest.py — provider abstraction + polling ingest (PR #134).

Pulls recent company news from Alpaca and Finnhub, normalizes each article to
one internal shape, and stores article + extracted events via core.news_events.
Polling (default 15 min via the app scheduler) — not WebSockets: Ghost's model
horizon is daily/3-day, so feed latency is not the bottleneck (per plan).

Provider failures are per-provider and loud-but-contained: one dead provider
must not kill the cycle, and a fully dead cycle must read as "news unavailable"
downstream (core.news_events.news_available), never as neutral.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import time
from typing import Any, Dict, List

import requests

from core.news_events import ensure_news_tables, store_article_and_events

LOGGER = logging.getLogger("ghost.news_ingest")

_TIMEOUT = float(os.getenv("NEWS_INGEST_TIMEOUT_S", "15"))


def ingest_enabled() -> bool:
    return (os.getenv("NEWS_INGEST_ENABLED", "1") or "1").strip().lower() in ("1", "on", "true", "yes")


def _fetch_alpaca(symbols: List[str], since_ts: int) -> List[Dict[str, Any]]:
    key = os.getenv("ALPACA_KEY_ID", "")
    sec = os.getenv("ALPACA_SECRET_KEY", "")
    if not key or not sec:
        return []
    start = _dt.datetime.fromtimestamp(since_ts, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out: List[Dict[str, Any]] = []
    r = requests.get(
        "https://data.alpaca.markets/v1beta1/news",
        params={"symbols": ",".join(symbols), "start": start, "limit": 50, "sort": "desc"},
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec},
        timeout=_TIMEOUT,
    )
    if r.status_code != 200:
        LOGGER.warning("alpaca news HTTP %s", r.status_code)
        return []
    for a in (r.json() or {}).get("news", []):
        ts = a.get("created_at") or a.get("updated_at") or ""
        try:
            published = int(_dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp())
        except Exception:
            continue
        for sym in (a.get("symbols") or []):
            if sym.upper() not in symbols:
                continue
            out.append({
                "provider": "alpaca", "provider_article_id": a.get("id"),
                "symbol": sym.upper(), "headline": a.get("headline") or "",
                "summary": a.get("summary") or "", "url": a.get("url") or "",
                "source": a.get("source") or "", "published_at": published,
                "raw": {"author": a.get("author")},
            })
    return out


def _fetch_finnhub(symbol: str, since_ts: int) -> List[Dict[str, Any]]:
    token = os.getenv("FINNHUB_API_KEY", "")
    if not token:
        return []
    frm = _dt.datetime.fromtimestamp(since_ts, _dt.timezone.utc).strftime("%Y-%m-%d")
    to = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    r = requests.get(
        "https://finnhub.io/api/v1/company-news",
        params={"symbol": symbol, "from": frm, "to": to, "token": token},
        timeout=_TIMEOUT,
    )
    if r.status_code != 200:
        LOGGER.warning("finnhub news %s HTTP %s", symbol, r.status_code)
        return []
    out = []
    for a in (r.json() or [])[:50]:
        published = int(a.get("datetime") or 0)
        if published <= since_ts:
            continue
        out.append({
            "provider": "finnhub", "provider_article_id": a.get("id"),
            "symbol": symbol.upper(), "headline": a.get("headline") or "",
            "summary": a.get("summary") or "", "url": a.get("url") or "",
            "source": a.get("source") or "", "published_at": published,
            "raw": {"category": a.get("category")},
        })
    return out


def run_news_ingest_cycle(symbols: List[str] | None = None,
                          lookback_s: int = 2 * 86400) -> Dict[str, Any]:
    """One polling pass: fetch, normalize, dedupe-store, extract events."""
    if not ingest_enabled():
        return {"ok": True, "skipped": "NEWS_INGEST_ENABLED=0"}
    if symbols is None:
        from config.symbols import OFFICIAL_WATCHLIST
        symbols = list(OFFICIAL_WATCHLIST)
    since = int(time.time()) - int(lookback_s)
    started = time.time()
    articles: List[Dict[str, Any]] = []
    provider_status: Dict[str, Any] = {}

    try:
        got = _fetch_alpaca(symbols, since)
        articles += got
        provider_status["alpaca"] = {"ok": True, "articles": len(got)}
    except Exception as exc:
        provider_status["alpaca"] = {"ok": False, "error": str(exc)[:100]}
        LOGGER.warning("alpaca news fetch failed: %s", str(exc)[:120])

    fh_total = 0
    fh_errors = 0
    for sym in symbols:
        try:
            got = _fetch_finnhub(sym, since)
            articles += got
            fh_total += len(got)
        except Exception as exc:
            fh_errors += 1
            if fh_errors <= 2:
                LOGGER.warning("finnhub news %s failed: %s", sym, str(exc)[:100])
        time.sleep(float(os.getenv("NEWS_INGEST_SYMBOL_DELAY_S", "0.25")))
    provider_status["finnhub"] = {"ok": fh_errors < len(symbols), "articles": fh_total,
                                  "symbol_errors": fh_errors}

    stored_articles = stored_events = 0
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_news_tables(cur)
            for art in articles:
                res = store_article_and_events(cur, art)
                stored_articles += 1 if res["article_stored"] else 0
                stored_events += res["events_stored"]
            conn.commit()
    except Exception as exc:
        LOGGER.warning("news ingest store failed: %s", str(exc)[:140])
        return {"ok": False, "error": str(exc)[:140], "providers": provider_status}

    out = {
        "ok": True, "symbols": len(symbols), "fetched": len(articles),
        "stored_articles": stored_articles, "stored_events": stored_events,
        "providers": provider_status, "duration_s": round(time.time() - started, 1),
    }
    LOGGER.info("[news_ingest] %s", out)
    return out
