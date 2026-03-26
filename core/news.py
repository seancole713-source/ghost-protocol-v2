"""
core/news.py - News brain with Claude-powered sentiment scoring.
Fetches Finnhub + CryptoPanic every 30 min.
Scores headlines via Claude Haiku batch call, stores per-symbol sentiment score.
prediction.py reads get_symbol_sentiment() to adjust confidence - no new Telegram alerts.
"""
import os, time, logging, json, requests
from typing import List, Dict

LOGGER = logging.getLogger("ghost.news")
CRYPTOPANIC_KEY = os.getenv("CRYPTOPANIC_API_KEY", "")
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

BEARISH_WORDS = ["crash","collapse","fall","drop","decline","loss","bankrupt","fraud",
    "investigation","lawsuit","ban","restrict","inflation","recession","default","fail","warning","risk"]
BULLISH_WORDS = ["surge","rally","gain","rise","record","profit","growth","partnership",
    "acquisition","approval","launch","upgrade","beat","exceed","expansion","bullish","boost"]

_seen_headlines = set()
_cached_articles: List[Dict] = []
# Per-symbol sentiment scores updated every 30 min. Read by prediction.py.
# Values: -1.0 (very bearish) to +1.0 (very bullish), 0.0 = neutral/unknown
_symbol_sentiment: Dict[str, float] = {}


def get_symbol_sentiment(symbol: str) -> float:
    """Return latest Claude sentiment score for a symbol. 0.0 = neutral."""
    return _symbol_sentiment.get(symbol.upper(), 0.0)


def get_all_sentiments() -> Dict[str, float]:
    """Return full sentiment dict for dashboard/API."""
    return dict(_symbol_sentiment)


def _keyword_score(text: str) -> float:
    """Fallback keyword-based scoring when Claude unavailable."""
    t = text.lower()
    b = sum(1 for w in BEARISH_WORDS if w in t)
    u = sum(1 for w in BULLISH_WORDS if w in t)
    total = b + u
    if total == 0:
        return 0.0
    return round((u - b) / total, 3)


def _score_with_claude(articles: List[Dict]) -> Dict[str, float]:
    """
    Batch call Claude Haiku to score sentiment per symbol from headlines.
    Returns {SYMBOL: score} where score is -1.0 (bearish) to +1.0 (bullish).
    One API call per 30-min cycle - not per prediction.
    """
    if not ANTHROPIC_KEY or not articles:
        return {}

    headline_lines = ""
    for a in articles[:40]:
        syms = ",".join(a.get("symbols", ["MARKET"]))
        headline_lines += "[" + syms + "] " + a.get("title", "")[:120] + "\n"

    prompt = (
        "Analyze these financial news headlines and score sentiment per symbol.\n\n"
        "Headlines (format: [SYMBOLS] headline):\n"
        + headline_lines +
        "\nFor each symbol mentioned, give a sentiment score: "
        "-1.0 = very bearish, 0.0 = neutral, +1.0 = very bullish.\n"
        "Consider: bad news/crash/fraud = bearish, good news/launch/profit = bullish.\n"
        "Respond ONLY with JSON like: {\"BTC\": -0.4, \"ETH\": 0.2, \"LINK\": -0.7}\n"
        "Only include symbols from the headlines. No explanation."
    )

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )
        if resp.status_code == 200:
            raw = resp.json()["content"][0]["text"].strip()
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start >= 0 and end > start:
                scores = json.loads(raw[start:end])
                clean = {k.upper(): max(-1.0, min(1.0, float(v))) for k, v in scores.items()}
                LOGGER.info(f"Claude sentiment: {clean}")
                return clean
        else:
            LOGGER.warning(f"Claude API {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        LOGGER.warning(f"Claude sentiment error: {e}")
    return {}


def _fallback_scores(articles: List[Dict]) -> Dict[str, float]:
    """Aggregate keyword scores per symbol across all articles."""
    scores: Dict[str, list] = {}
    for a in articles:
        score = _keyword_score(a.get("title", ""))
        for sym in a.get("symbols", []):
            s = sym.upper()
            scores.setdefault(s, []).append(score)
    return {s: round(sum(v)/len(v), 3) for s, v in scores.items() if v}


def _fetch_cryptopanic() -> List[Dict]:
    if not CRYPTOPANIC_KEY:
        return []
    try:
        r = requests.get(
            "https://cryptopanic.com/api/v1/posts/",
            params={"auth_token": CRYPTOPANIC_KEY, "kind": "news", "public": "true"},
            timeout=3,
        )
        articles = []
        for item in r.json().get("results", [])[:20]:
            syms = [c["code"].upper() for c in item.get("currencies", [])]
            title = item.get("title", "")
            if title and title not in _seen_headlines:
                _seen_headlines.add(title)
                articles.append({"title": title, "symbols": syms or ["CRYPTO"], "source": "cryptopanic"})
        return articles
    except Exception as e:
        LOGGER.warning(f"CryptoPanic fetch error: {e}")
        return []


def _fetch_finnhub_crypto() -> List[Dict]:
    if not FINNHUB_KEY:
        return []
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/news",
            params={"category": "crypto", "token": FINNHUB_KEY},
            timeout=3,
        )
        articles = []
        for a in r.json()[:15]:
            title = a.get("headline", "")
            if title and title not in _seen_headlines:
                _seen_headlines.add(title)
                articles.append({"title": title, "symbols": ["CRYPTO"], "source": "finnhub"})
        return articles
    except Exception as e:
        LOGGER.warning(f"Finnhub fetch error: {e}")
        return []


def _fetch_finnhub_stock(symbol: str) -> List[Dict]:
    if not FINNHUB_KEY:
        return []
    try:
        import datetime
        today = datetime.date.today().isoformat()
        from_date = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
        r = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={"symbol": symbol, "from": from_date, "to": today, "token": FINNHUB_KEY},
            timeout=3,
        )
        return [{"title": a["headline"], "symbols": [symbol], "source": "finnhub_stock"}
                for a in r.json()[:5] if "headline" in a]
    except Exception:
        return []


def run_news_cycle() -> List[Dict]:
    """
    Main cycle called every 30 min by scheduler.
    1. Fetch headlines from Finnhub + CryptoPanic
    2. Score sentiment with Claude (or keyword fallback)
    3. Store scores in _symbol_sentiment - prediction.py reads these
    No Telegram alerts sent here.
    """
    global _cached_articles, _symbol_sentiment

    articles = []
    articles.extend(_fetch_cryptopanic())
    articles.extend(_fetch_finnhub_crypto())
    for sym in ["AAPL", "NVDA", "TSLA", "MSFT", "META", "AMZN"]:
        articles.extend(_fetch_finnhub_stock(sym))

    if not articles:
        LOGGER.info("News cycle: no new articles")
        return _cached_articles

    # Score sentiment - Claude if available, else keyword fallback
    if ANTHROPIC_KEY:
        scores = _score_with_claude(articles)
        if scores:
            # Decay old scores toward neutral before updating
            for sym in list(_symbol_sentiment):
                _symbol_sentiment[sym] = round(_symbol_sentiment[sym] * 0.7, 3)
            _symbol_sentiment.update(scores)
        else:
            _symbol_sentiment.update(_fallback_scores(articles))
    else:
        _symbol_sentiment.update(_fallback_scores(articles))
        LOGGER.info("No ANTHROPIC_API_KEY set - using keyword fallback")

    _cached_articles = articles
    LOGGER.info(f"News cycle: {len(articles)} articles, {len(_symbol_sentiment)} symbols scored")
    return articles


# Alias for prediction.py compatibility
get_sentiment_for_symbol = get_symbol_sentiment

def get_cached_articles(limit=None) -> List[Dict]:
    """Return cached articles with proper per-article sentiment scoring."""
    try:
        source = list(_cached_articles) if _cached_articles else []
        enriched = []
        for a in source:
            try:
                art = dict(a)
                # First try symbol-level sentiment
                syms = a.get("symbols", [])
                sym_scores = [_symbol_sentiment.get(s.upper(), 0.0) for s in syms if s.upper() in _symbol_sentiment]
                if sym_scores:
                    art["sentiment"] = round(sum(sym_scores) / len(sym_scores), 3)
                else:
                    # Fall back to keyword scoring on headline
                    title = (a.get("title") or a.get("headline") or "").lower()
                    bull = sum(1 for w in BULLISH_WORDS if w in title)
                    bear = sum(1 for w in BEARISH_WORDS if w in title)
                    if bear > bull: art["sentiment"] = round(-0.3 - (bear - bull) * 0.1, 2)
                    elif bull > bear: art["sentiment"] = round(0.3 + (bull - bear) * 0.1, 2)
                    else: art["sentiment"] = 0.0
                enriched.append(art)
            except Exception:
                enriched.append(a)
        return enriched if limit is None else enriched[:limit]
    except Exception as _e:
        LOGGER.error("get_cached_articles error: " + str(_e))
        return []


def get_recent_articles(limit: int = 20) -> List[Dict]:
    """Alias for get_cached_articles — returns cached news, never blocks."""
    return get_cached_articles(limit=limit)

def get_sentiment_for_symbol(symbol: str) -> float:
    """Return cached sentiment score for a symbol. Alias for get_symbol_sentiment."""
    return get_symbol_sentiment(symbol)
