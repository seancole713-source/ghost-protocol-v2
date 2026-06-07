"""Persistent multi-symbol news store — Finnhub auto-pull + manual JSON import."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

LOGGER = logging.getLogger("ghost.news_store")

_VALID_CATEGORIES = frozenset({"news", "earnings", "press", "research", "all"})


def ensure_news_tables() -> None:
    from core.db import db_conn

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ghost_news_articles (
                id SERIAL PRIMARY KEY,
                article_id TEXT UNIQUE NOT NULL,
                symbol VARCHAR(20) NOT NULL,
                title TEXT NOT NULL,
                summary TEXT,
                url TEXT,
                published_at BIGINT,
                ingested_at BIGINT NOT NULL,
                source VARCHAR(80),
                origin VARCHAR(20) NOT NULL DEFAULT 'import',
                category VARCHAR(20) DEFAULT 'news',
                sentiment REAL
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ghost_news_symbol_time
            ON ghost_news_articles (symbol, published_at DESC)
            """
        )


def _article_id(symbol: str, title: str, url: Optional[str], published_at: Optional[int]) -> str:
    raw = (url or "").strip() or f"{symbol}|{title}|{published_at or 0}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _normalize_article(entry: Dict[str, Any], *, default_origin: str = "import") -> Dict[str, Any]:
    sym = str(entry.get("symbol") or entry.get("ticker") or "").strip().upper()
    title = str(entry.get("title") or entry.get("headline") or "").strip()
    if not sym or not title:
        raise ValueError("each article needs symbol and title")
    summary = str(entry.get("summary") or entry.get("body") or entry.get("description") or "").strip()
    url = str(entry.get("url") or entry.get("link") or "").strip() or None
    published_at = entry.get("published_at") or entry.get("publishedAt") or entry.get("ts")
    if published_at is not None:
        published_at = int(published_at)
    else:
        published_at = int(time.time())
    source = str(entry.get("source") or entry.get("publisher") or "import").strip()[:80]
    category = str(entry.get("category") or "news").strip().lower()
    if category not in _VALID_CATEGORIES - {"all"}:
        category = "news"
    sentiment = entry.get("sentiment")
    if sentiment is not None:
        sentiment = max(-1.0, min(1.0, float(sentiment)))
    origin = str(entry.get("origin") or default_origin).strip()[:20] or default_origin
    aid = _article_id(sym, title, url, published_at)
    return {
        "article_id": aid,
        "symbol": sym,
        "title": title,
        "summary": summary or None,
        "url": url,
        "published_at": published_at,
        "ingested_at": int(time.time()),
        "source": source,
        "origin": origin,
        "category": category,
        "sentiment": sentiment,
    }


def import_articles_payload(payload: Dict[str, Any], *, watchlist_only: bool = True) -> Dict[str, Any]:
    """Import articles from API/file JSON. Returns counts + errors."""
    ensure_news_tables()
    articles_in = payload.get("articles")
    if articles_in is None and isinstance(payload.get("article"), dict):
        articles_in = [payload["article"]]
    if not isinstance(articles_in, list):
        raise ValueError("payload must include an articles array")

    from config.symbols import OFFICIAL_WATCHLIST

    allowed = set(OFFICIAL_WATCHLIST)
    inserted = 0
    updated = 0
    skipped = 0
    errors: List[str] = []

    with __import__("core.db", fromlist=["db_conn"]).db_conn() as conn:
        cur = conn.cursor()
        for i, raw in enumerate(articles_in):
            if not isinstance(raw, dict):
                errors.append(f"articles[{i}]: not an object")
                skipped += 1
                continue
            try:
                row = _normalize_article(raw, default_origin="import")
                if watchlist_only and row["symbol"] not in allowed:
                    errors.append(f"articles[{i}]: {row['symbol']} not on watchlist")
                    skipped += 1
                    continue
                cur.execute(
                    """
                    INSERT INTO ghost_news_articles
                    (article_id, symbol, title, summary, url, published_at, ingested_at,
                     source, origin, category, sentiment)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (article_id) DO UPDATE SET
                        title = EXCLUDED.title,
                        summary = EXCLUDED.summary,
                        published_at = EXCLUDED.published_at,
                        ingested_at = EXCLUDED.ingested_at,
                        source = EXCLUDED.source,
                        category = EXCLUDED.category,
                        sentiment = COALESCE(EXCLUDED.sentiment, ghost_news_articles.sentiment)
                    RETURNING (xmax = 0) AS inserted
                    """,
                    (
                        row["article_id"],
                        row["symbol"],
                        row["title"],
                        row["summary"],
                        row["url"],
                        row["published_at"],
                        row["ingested_at"],
                        row["source"],
                        row["origin"],
                        row["category"],
                        row["sentiment"],
                    ),
                )
                was_insert = cur.fetchone()[0]
                if was_insert:
                    inserted += 1
                else:
                    updated += 1
            except Exception as e:
                errors.append(f"articles[{i}]: {str(e)[:120]}")
                skipped += 1

    refresh_symbol_sentiments()
    return {
        "ok": True,
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "errors": errors[:20],
        "total_received": len(articles_in),
    }


def upsert_fetched_articles(articles: List[Dict[str, Any]], *, origin: str = "finnhub") -> int:
    """Store auto-fetched headlines. Returns number of rows touched."""
    if not articles:
        return 0
    ensure_news_tables()
    count = 0
    with __import__("core.db", fromlist=["db_conn"]).db_conn() as conn:
        cur = conn.cursor()
        for raw in articles:
            try:
                syms = raw.get("symbols") or []
                sym = str(syms[0] if syms else raw.get("symbol") or "").upper()
                if not sym:
                    continue
                row = _normalize_article(
                    {
                        "symbol": sym,
                        "title": raw.get("title") or raw.get("headline"),
                        "summary": raw.get("summary") or "",
                        "url": raw.get("url") or raw.get("link") or "",
                        "published_at": raw.get("published_at") or raw.get("datetime") or int(time.time()),
                        "source": raw.get("source") or origin,
                        "origin": origin,
                        "category": raw.get("category") or "news",
                        "sentiment": raw.get("sentiment"),
                    },
                    default_origin=origin,
                )
                cur.execute(
                    """
                    INSERT INTO ghost_news_articles
                    (article_id, symbol, title, summary, url, published_at, ingested_at,
                     source, origin, category, sentiment)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (article_id) DO NOTHING
                    """,
                    (
                        row["article_id"],
                        row["symbol"],
                        row["title"],
                        row["summary"],
                        row["url"],
                        row["published_at"],
                        row["ingested_at"],
                        row["source"],
                        row["origin"],
                        row["category"],
                        row["sentiment"],
                    ),
                )
                if cur.rowcount:
                    count += 1
            except Exception as e:
                LOGGER.debug("upsert_fetched skip: %s", str(e)[:80])
    return count


def list_articles(
    *,
    symbol: Optional[str] = None,
    category: str = "all",
    limit: int = 50,
) -> List[Dict[str, Any]]:
    ensure_news_tables()
    cat = (category or "all").lower()
    lim = max(1, min(200, int(limit)))
    clauses = ["published_at >= %s"]
    params: List[Any] = [int(time.time()) - 14 * 86400]
    if symbol:
        clauses.append("symbol = %s")
        params.append(symbol.strip().upper())
    if cat != "all":
        clauses.append("category = %s")
        params.append(cat)
    where = " AND ".join(clauses)
    params.append(lim)
    with __import__("core.db", fromlist=["db_conn"]).db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT symbol, title, summary, url, published_at, source, origin, category, sentiment
            FROM ghost_news_articles
            WHERE {where}
            ORDER BY published_at DESC
            LIMIT %s
            """,
            tuple(params),
        )
        rows = cur.fetchall()
    out = []
    for sym, title, summary, url, pub, source, origin, category_v, sentiment in rows:
        out.append(
            {
                "symbol": sym,
                "symbols": [sym],
                "title": title,
                "summary": summary or "",
                "url": url or "",
                "published_at": int(pub or 0),
                "source": source or origin or "News",
                "origin": origin or "import",
                "category": category_v or "news",
                "sentiment": float(sentiment) if sentiment is not None else 0.0,
            }
        )
    return out


def refresh_symbol_sentiments() -> Dict[str, float]:
    """Aggregate recent article sentiment per symbol into memory cache."""
    from core.news import _fallback_scores, _score_with_claude, _symbol_sentiment

    ensure_news_tables()
    articles = list_articles(limit=200)
    scores: Dict[str, float] = {}
    if articles:
        if os.getenv("ANTHROPIC_API_KEY", "").strip():
            scores = _score_with_claude(articles) or {}
        if not scores:
            scores = _fallback_scores(articles)
        for row in articles:
            sym = row["symbol"]
            if row.get("sentiment") is not None and sym not in scores:
                scores[sym] = float(row["sentiment"])
    _symbol_sentiment.clear()
    _symbol_sentiment.update(scores)
    return dict(scores)


def watchlist_batch_for_cycle() -> Tuple[List[str], int]:
    """Return the next symbol batch for Finnhub rotation."""
    from config.symbols import OFFICIAL_WATCHLIST

    syms = list(OFFICIAL_WATCHLIST)
    if not syms:
        return ["WOLF"], 0
    batch_size = max(1, int(os.getenv("NEWS_SYMBOLS_PER_CYCLE", "8")))
    offset = 0
    try:
        with __import__("core.db", fromlist=["db_conn"]).db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO ghost_state(key,val) VALUES('news_rotation_offset','0') "
                "ON CONFLICT(key) DO NOTHING"
            )
            cur.execute("SELECT val FROM ghost_state WHERE key='news_rotation_offset'")
            row = cur.fetchone()
            offset = int(row[0]) if row and row[0] else 0
    except Exception:
        offset = 0
    batch = [syms[(offset + i) % len(syms)] for i in range(min(batch_size, len(syms)))]
    next_offset = (offset + batch_size) % len(syms)
    try:
        with __import__("core.db", fromlist=["db_conn"]).db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO ghost_state(key,val) VALUES('news_rotation_offset',%s) "
                "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                (str(next_offset),),
            )
    except Exception as e:
        LOGGER.debug("news rotation offset write failed: %s", str(e)[:80])
    return batch, next_offset


def import_format_doc() -> Dict[str, Any]:
    return {
        "version": 1,
        "description": "Ghost news import — JSON file or POST /api/admin/news/import body",
        "required_per_article": ["symbol", "title"],
        "optional_per_article": [
            "summary",
            "url",
            "published_at",
            "source",
            "category",
            "sentiment",
        ],
        "categories": sorted(_VALID_CATEGORIES - {"all"}),
        "example": {
            "version": 1,
            "articles": [
                {
                    "symbol": "ABCL",
                    "title": "AbCellera reports Q1 2026 results",
                    "summary": "Revenue ~$8M, net loss $43.2M; ABCL635 Phase 2 readout expected Q3.",
                    "url": "https://example.com/abcl-q1",
                    "published_at": 1715097600,
                    "source": "manual_research",
                    "category": "earnings",
                    "sentiment": -0.15,
                }
            ],
        },
    }
