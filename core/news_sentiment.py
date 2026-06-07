"""Per-symbol news sentiment — used by stock_engine during prediction."""
from __future__ import annotations

from typing import Any, Dict

from core.news import get_symbol_sentiment
from core.news_store import list_articles


def fetch_news_sentiment(symbol: str, limit: int = 10) -> Dict[str, Any]:
    sym = (symbol or "").strip().upper()
    score = float(get_symbol_sentiment(sym))
    articles = list_articles(symbol=sym, limit=max(1, min(limit, 20)))
    if score == 0.0 and articles:
        vals = [float(a.get("sentiment") or 0.0) for a in articles if a.get("sentiment") is not None]
        if vals:
            score = round(sum(vals) / len(vals), 3)
    label = "NEUTRAL"
    if score >= 0.3:
        label = "BULLISH"
    elif score <= -0.3:
        label = "BEARISH"
    return {
        "ok": True,
        "symbol": sym,
        "sentiment_score": score,
        "sentiment_label": label,
        "article_count": len(articles),
    }
