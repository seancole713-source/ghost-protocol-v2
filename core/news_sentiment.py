"""Lightweight news sentiment scorer (Phase 2) — lexicon, no FinBERT yet."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_BULL = re.compile(
    r"\b(beat|surge|soar|rally|upgrade|raise|growth|record|contract|win|strong|bullish|breakout)\b",
    re.I,
)
_BEAR = re.compile(
    r"\b(miss|cut|downgrade|lower|weak|loss|decline|fall|drop|bearish|bankruptcy|delay|recall)\b",
    re.I,
)


def score_headline(text: Optional[str]) -> float:
    """Return sentiment in [-1, 1]."""
    if not text:
        return 0.0
    bull = len(_BULL.findall(text))
    bear = len(_BEAR.findall(text))
    if bull == bear == 0:
        return 0.0
    return round((bull - bear) / max(bull + bear, 1), 3)


def score_articles(articles: List[Dict[str, Any]], *, symbol: Optional[str] = None) -> Dict[str, Any]:
    sym = (symbol or "").upper()
    rows = []
    for a in articles or []:
        if sym and sym not in (a.get("symbol") or a.get("symbols") or sym):
            if isinstance(a.get("symbols"), list) and sym not in a["symbols"]:
                continue
        title = a.get("title") or a.get("headline") or ""
        s = score_headline(title)
        rows.append({"title": title[:120], "sentiment": s})
    if not rows:
        return {"ok": True, "count": 0, "avg_sentiment": None, "articles": []}
    avg = sum(r["sentiment"] for r in rows) / len(rows)
    label = "bullish" if avg > 0.15 else ("bearish" if avg < -0.15 else "neutral")
    return {
        "ok": True,
        "count": len(rows),
        "avg_sentiment": round(avg, 3),
        "label": label,
        "articles": rows[:10],
        "model": "lexicon_v1",
    }


def _score_to_label(score: float) -> str:
    if score > 0.15:
        return "BULLISH"
    if score < -0.15:
        return "BEARISH"
    return "NEUTRAL"


def fetch_news_sentiment(symbol: str, *, limit: int = 10) -> Dict[str, Any]:
    """Stock-engine wrapper: DB headlines + Claude cache fallback."""
    sym = (symbol or "").upper()
    articles: List[Dict[str, Any]] = []
    try:
        from core.news_store import list_articles

        articles = list_articles(symbol=sym, limit=limit) or []
    except Exception:
        pass
    scored = score_articles(articles, symbol=sym)
    if scored.get("count"):
        avg = float(scored["avg_sentiment"] or 0.0)
        return {
            "ok": True,
            "symbol": sym,
            "sentiment_score": avg,
            "sentiment_label": _score_to_label(avg),
            "articles": scored.get("articles") or [],
            "model": scored.get("model"),
        }
    try:
        from core.news import get_symbol_sentiment

        cached = float(get_symbol_sentiment(sym))
    except Exception:
        cached = 0.0
    return {
        "ok": True,
        "symbol": sym,
        "sentiment_score": cached,
        "sentiment_label": _score_to_label(cached),
        "articles": [],
        "model": "claude_cache" if cached else "lexicon_v1",
    }
