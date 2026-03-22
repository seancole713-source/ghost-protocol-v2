"""
core/news.py - News brain. Fetches Reuters + CryptoPanic, scores sentiment,
fires Telegram alerts when breaking news hits an open position.
Runs every 30 minutes via scheduler.
"""
import os, time, logging, requests
from typing import List, Dict
from xml.etree import ElementTree as ET

LOGGER = logging.getLogger("ghost.news")
CRYPTOPANIC_KEY = os.getenv("CRYPTOPANIC_API_KEY", "")
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY", "")

RSS_FEEDS = [
    "https://feeds.reuters.com/reuters/businessNews",
    "https://feeds.reuters.com/reuters/technologyNews",
    "https://feeds.reuters.com/reuters/topNews",
]

# Simple keyword sentiment scoring
BEARISH_WORDS = ["crash","collapse","fall","drop","decline","loss","bankrupt","fraud",
    "investigation","lawsuit","ban","restrict","inflation","recession","default","fail"]
BULLISH_WORDS = ["surge","rally","gain","rise","record","profit","growth","partnership",
    "acquisition","approval","launch","upgrade","beat","exceed","expansion","bullish"]

# Cache to avoid re-alerting same headline
_seen_headlines = set()
_last_fetch = 0
FETCH_INTERVAL = 1800  # 30 minutes

def _score_sentiment(text: str) -> str:
    """Score headline sentiment. Returns BULLISH, BEARISH, or NEUTRAL."""
    text_lower = text.lower()
    bull = sum(1 for w in BULLISH_WORDS if w in text_lower)
    bear = sum(1 for w in BEARISH_WORDS if w in text_lower)
    if bear > bull: return "BEARISH"
    if bull > bear: return "BULLISH"
    return "NEUTRAL"

def _fetch_rss() -> List[Dict]:
    """Fetch articles from RSS feeds."""
    articles = []
    for url in RSS_FEEDS:
        try:
            r = requests.get(url, timeout=8)
            root = ET.fromstring(r.text)
            for item in root.findall(".//item")[:5]:
                title = item.findtext("title", "").strip()
                if title and title not in _seen_headlines:
                    articles.append({
                        "headline": title,
                        "source": "reuters",
                        "sentiment": _score_sentiment(title),
                        "url": item.findtext("link", ""),
                        "ts": int(time.time()),
                    })
        except Exception as e:
            LOGGER.warning("RSS fetch error: " + str(e))
    return articles

def _fetch_cryptopanic() -> List[Dict]:
    """Fetch crypto news from CryptoPanic API."""
    if not CRYPTOPANIC_KEY: return []
    articles = []
    try:
        r = requests.get(
            "https://cryptopanic.com/api/v1/posts/?auth_token=" + CRYPTOPANIC_KEY + "&filter=hot&public=true",
            timeout=8
        )
        for item in r.json().get("results", [])[:10]:
            title = item.get("title", "").strip()
            if not title or title in _seen_headlines: continue
            # CryptoPanic provides votes for sentiment
            votes = item.get("votes", {})
            bull = votes.get("positive", 0) + votes.get("important", 0)
            bear = votes.get("negative", 0) + votes.get("disliked", 0)
            sentiment = "BEARISH" if bear > bull else "BULLISH" if bull > bear else _score_sentiment(title)
            # Extract symbols mentioned
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
    """Get symbols with active open predictions."""
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT symbol FROM predictions WHERE outcome IS NULL AND entry_price IS NOT NULL")
            return [r[0] for r in cur.fetchall()]
    except: return []

def run_news_cycle() -> int:
    """
    Main news cycle. Called every 30 minutes by scheduler.
    Returns number of alerts sent.
    """
    global _last_fetch
    now = int(time.time())
    LOGGER.info("News cycle running...")
    articles = _fetch_rss() + _fetch_cryptopanic()
    open_symbols = _get_open_symbols()
    alerts_sent = 0
    from core.telegram import send_news_alert
    for article in articles:
        headline = article["headline"]
        sentiment = article["sentiment"]
        # Only alert on BEARISH or strong BULLISH affecting open positions
        if sentiment == "NEUTRAL": continue
        # Check if headline mentions any open symbol
        headline_upper = headline.upper()
        affected = []
        # Check explicit symbols list (CryptoPanic)
        for sym in article.get("symbols", []):
            if sym in open_symbols:
                affected.append(sym)
        # Check by name mention in headline
        if not affected:
            for sym in open_symbols:
                if sym in headline_upper:
                    affected.append(sym)
        if affected and sentiment == "BEARISH":
            for sym in affected:
                action = "Consider early exit if near stop loss"
                send_news_alert(sym, headline, sentiment, action)
                alerts_sent += 1
        _seen_headlines.add(headline)
    _last_fetch = now
    LOGGER.info("News cycle done: " + str(len(articles)) + " articles, " + str(alerts_sent) + " alerts")
    return alerts_sent

def get_recent_articles(limit: int = 20) -> List[Dict]:
    """Return recent articles for dashboard news tab."""
    articles = _fetch_rss() + _fetch_cryptopanic()
    return sorted(articles, key=lambda x: x["ts"], reverse=True)[:limit]