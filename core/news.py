"""
core/news.py - News brain using Finnhub (allowed by Railway network).
CryptoPanic for crypto news. Scores sentiment, alerts on open positions.
Runs every 30 minutes via scheduler.
"""
import os, time, logging, requests
from typing import List, Dict

LOGGER = logging.getLogger("ghost.news")
CRYPTOPANIC_KEY = os.getenv("CRYPTOPANIC_API_KEY", "")
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY", "")

BEARISH_WORDS = ["crash","collapse","fall","drop","decline","loss","bankrupt","fraud",
    "investigation","lawsuit","ban","restrict","inflation","recession","default","fail","warning","risk"]
BULLISH_WORDS = ["surge","rally","gain","rise","record","profit","growth","partnership",
    "acquisition","approval","launch","upgrade","beat","exceed","expansion","bullish","boost"]

_seen_headlines = set()
_cached_articles: List[Dict] = []

def _score_sentiment(text: str) -> str:
    text_lower = text.lower()
    bull = sum(1 for w in BULLISH_WORDS if w in text_lower)
    bear = sum(1 for w in BEARISH_WORDS if w in text_lower)
    if bear > bull: return "BEARISH"
    if bull > bear: return "BULLISH"
    return "NEUTRAL"

def _fetch_finnhub_market_news() -> List[Dict]:
    """Fetch general market news from Finnhub API."""
    if not FINNHUB_KEY: return []
    articles = []
    try:
        url = "https://finnhub.io/api/v1/news?category=general&token=" + FINNHUB_KEY
        r = requests.get(url, timeout=8)
        for item in r.json()[:15]:
            title = item.get("headline", "").strip()
            if not title or title in _seen_headlines: continue
            articles.append({
                "headline": title,
                "source": "finnhub",
                "sentiment": _score_sentiment(title),
                "symbols": [],
                "url": item.get("url", ""),
                "ts": item.get("datetime", int(time.time())),
            })
    except Exception as e:
        LOGGER.warning("Finnhub news error: " + str(e))
    return articles

def _fetch_cryptopanic() -> List[Dict]:
    """Fetch crypto news from CryptoPanic API."""
    if not CRYPTOPANIC_KEY: return []
    articles = []
    try:
        url = "https://cryptopanic.com/api/v1/posts/?auth_token=" + CRYPTOPANIC_KEY + "&filter=hot&public=true"
        r = requests.get(url, timeout=8)
        for item in r.json().get("results", [])[:10]:
            title = item.get("title", "").strip()
            if not title or title in _seen_headlines: continue
            votes = item.get("votes", {})
            bull = votes.get("positive", 0) + votes.get("important", 0)
            bear = votes.get("negative", 0) + votes.get("disliked", 0)
            sentiment = "BEARISH" if bear > bull else "BULLISH" if bull > bear else _score_sentiment(title)
            currencies = [c["code"] for c in item.get("currencies", [])]
            articles.append({
                "headline": title,
                "source": "cryptopanic",
                "sentiment": sentiment,
                "symbols": currencies,
                "ts": int(time.time()),
            })
    except Exception as e:
        LOGGER.warning("CryptoPanic error: " + str(e))
    return articles

def _get_open_symbols() -> List[str]:
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT symbol FROM predictions WHERE outcome IS NULL AND entry_price IS NOT NULL")
            return [r[0] for r in cur.fetchall()]
    except: return []

def run_news_cycle() -> int:
    """Main news cycle. Called every 30 min by scheduler."""
    global _cached_articles
    LOGGER.info("News cycle running...")
    articles = _fetch_finnhub_market_news() + _fetch_cryptopanic()
    _cached_articles = articles
    if not articles:
        LOGGER.info("No new articles")
        return 0
    open_symbols = _get_open_symbols()
    alerts_sent = 0
    try:
        from core.telegram import send_news_alert
        for article in articles:
            if article["sentiment"] == "NEUTRAL": continue
            headline_upper = article["headline"].upper()
            affected = list(article.get("symbols", []))
            if not affected:
                for sym in open_symbols:
                    if sym in headline_upper:
                        affected.append(sym)
            if affected and article["sentiment"] == "BEARISH":
                for sym in affected[:2]:
                    send_news_alert(sym, article["headline"], "BEARISH", "Check your stop loss")
                    alerts_sent += 1
            _seen_headlines.add(article["headline"])
    except Exception as e:
        LOGGER.error("News alert error: " + str(e))
    LOGGER.info("News cycle done: " + str(len(articles)) + " articles, " + str(alerts_sent) + " alerts")
    return alerts_sent

def get_recent_articles(limit: int = 20) -> List[Dict]:
    """Return cached articles for dashboard."""
    if not _cached_articles:
        # Fetch fresh if cache empty
        articles = _fetch_finnhub_market_news() + _fetch_cryptopanic()
        return sorted(articles, key=lambda x: x["ts"], reverse=True)[:limit]
    return sorted(_cached_articles, key=lambda x: x["ts"], reverse=True)[:limit]