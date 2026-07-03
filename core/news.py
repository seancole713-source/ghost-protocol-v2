"""
core/news.py - News brain with Claude-powered sentiment scoring.

Fetches Finnhub company-news for watchlist symbols (rotating batch each cycle).
Manual imports land in ghost_news_articles via core/news_store.py.
Scores headlines via Claude Haiku (or keyword fallback); prediction reads get_symbol_sentiment().

P1-3 (audit): circuit breakers on Finnhub and Anthropic to prevent cascading waste.
"""
import os, time, logging, json, requests
from typing import List, Dict, Optional

LOGGER = logging.getLogger("ghost.news")
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# P1-3: circuit breakers for external API calls
from core.circuit_breaker import _finnhub_cb, _anthropic_cb

BEARISH_WORDS = [
    "crash","collapse","fall","drop","decline","loss","bankrupt","fraud",
    "investigation","lawsuit","ban","restrict","inflation","recession","default","fail","warning","risk",
    "slips","slump","sink","tumble","plunge","dump","bearish","weak","low","below","down","sell",
    "concern","fear","uncertainty","volatile","struggle","miss","disappoint","cut","halts","pauses",
    "loses","slides","retreats","pressure","caution","selloff","correction","overbought"]
BULLISH_WORDS = [
    "surge","rally","gain","rise","record","profit","growth","partnership",
    "acquisition","approval","launch","upgrade","beat","exceed","expansion","bullish","boost",
    "high","top","soar","jump","spike","recover","rebound","buy","bullish","strong",
    "breakout","milestone","win","success","positive","optimistic","upside","outperform",
    "holds","holds ground","above","climbing","adds","gains","advances","up "]

_seen_headlines: set = set()
_MAX_SEEN_HEADLINES = 5000  # PR #125 audit: cap to prevent unbounded memory growth
_cached_articles: List[Dict] = []
# Per-symbol sentiment scores updated every 30 min. Read by prediction.py.
# Values: -1.0 (very bearish) to +1.0 (very bullish), 0.0 = neutral/unknown
_symbol_sentiment: Dict[str, float] = {}
_finnhub_fail_streak = 0


def _safe_log_snippet(text: str, max_len: int = 160) -> str:
    """
    Normalize third-party response text for logs.

    - Collapses newlines/whitespace to one line to avoid log spam fan-out.
    - Suppresses raw HTML bodies to a concise marker.
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if "<!doctype html" in lowered or "<html" in lowered:
        return "[html-response-suppressed]"
    compact = " ".join(raw.split())
    return compact[:max_len]


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
        "Consider: bad news/lawsuit/miss = bearish, good news/launch/profit = bullish.\n"
        "Respond ONLY with JSON like: {\"WOLF\": -0.4}\n"
        "Only include symbols from the headlines. No explanation."
    )

    try:
        # P1-3: circuit breaker gate for Anthropic
        if not _anthropic_cb.allow():
            LOGGER.info("Claude sentiment skipped: Anthropic circuit breaker open")
            return {}
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
            _anthropic_cb.record_success()
            raw = resp.json()["content"][0]["text"].strip()
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start >= 0 and end > start:
                scores = json.loads(raw[start:end])
                clean = {k.upper(): max(-1.0, min(1.0, float(v))) for k, v in scores.items()}
                LOGGER.info(f"Claude sentiment: {clean}")
                return clean
        else:
            _anthropic_cb.record_failure()
            LOGGER.warning(f"Claude API {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        _anthropic_cb.record_failure()
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


def _fetch_finnhub_stock(symbol: str) -> List[Dict]:
    global _finnhub_fail_streak
    if not FINNHUB_KEY:
        return []
    # P1-3: circuit breaker gate
    if not _finnhub_cb.allow():
        return []
    try:
        import datetime
        today = datetime.date.today().isoformat()
        from_date = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
        attempts = 2
        for i in range(attempts):
            try:
                r = requests.get(
                    "https://finnhub.io/api/v1/company-news",
                    params={"symbol": symbol, "from": from_date, "to": today, "token": FINNHUB_KEY},
                    timeout=3,
                )
                r.raise_for_status()
                if _finnhub_fail_streak >= 3:
                    LOGGER.info("Finnhub recovered after %s consecutive failures", _finnhub_fail_streak)
                _finnhub_fail_streak = 0
                _finnhub_cb.record_success()
                return [{"title": a["headline"], "symbols": [symbol], "source": "finnhub_stock"}
                        for a in r.json()[:5] if "headline" in a]
            except requests.RequestException as e:
                if i < attempts - 1:
                    time.sleep(0.2)
                    continue
                _finnhub_fail_streak += 1
                _finnhub_cb.record_failure()
                if _finnhub_fail_streak >= 3:
                    LOGGER.warning(
                        "Finnhub stock fetch failing %s (streak=%s): %s",
                        symbol,
                        _finnhub_fail_streak,
                        str(e)[:160],
                    )
                else:
                    LOGGER.info(
                        "Finnhub stock transient error %s (streak=%s): %s",
                        symbol,
                        _finnhub_fail_streak,
                        str(e)[:160],
                    )
                return []
    except Exception:
        _finnhub_cb.record_failure()
        return []


def run_news_cycle() -> List[Dict]:
    """
    Main cycle called every 30 min by scheduler.
    1. Fetch Finnhub headlines for a watchlist batch (NEWS_SYMBOLS_PER_CYCLE, default 8)
    2. Persist to ghost_news_articles + score per-symbol sentiment
    3. prediction.py reads get_symbol_sentiment()
    """
    global _cached_articles

    from core.news_store import (
        list_articles,
        refresh_symbol_sentiments,
        upsert_fetched_articles,
        watchlist_batch_for_cycle,
    )

    batch, _next_off = watchlist_batch_for_cycle()
    fetched: List[Dict] = []
    for sym in batch:
        fetched.extend(_fetch_finnhub_stock(sym))

    stored = upsert_fetched_articles(fetched, origin="finnhub")
    scores = refresh_symbol_sentiments()
    _cached_articles = list_articles(limit=50)

    LOGGER.info(
        "News cycle: batch=%s fetched=%s stored=%s symbols_scored=%s",
        ",".join(batch),
        len(fetched),
        stored,
        len(scores),
    )
    return _cached_articles


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
                    if bear > bull: art["sentiment"] = round(-0.2 - (bear - bull) * 0.1, 2)
                    elif bull > bear: art["sentiment"] = round(0.2 + (bull - bear) * 0.1, 2)
                    elif bear == 1: art["sentiment"] = -0.1
                    elif bull == 1: art["sentiment"] = 0.1
                    else: art["sentiment"] = 0.0
                enriched.append(art)
            except Exception:
                enriched.append(a)
        return enriched if limit is None else enriched[:limit]
    except Exception as _e:
        LOGGER.error("get_cached_articles error: " + str(_e))
        return []


def get_recent_articles(limit: int = 20, symbol: Optional[str] = None) -> List[Dict]:
    """Recent headlines from DB (import + auto-fetch), with in-memory fallback."""
    try:
        from core.news_store import list_articles

        rows = list_articles(symbol=symbol, limit=limit)
        if rows:
            return rows
    except Exception as e:
        LOGGER.debug("get_recent_articles db read failed: %s", str(e)[:80])
    cached = get_cached_articles(limit=limit)
    if symbol:
        sym = symbol.strip().upper()
        cached = [a for a in cached if sym in [s.upper() for s in (a.get("symbols") or [])]]
    return cached

def get_sentiment_for_symbol(symbol: str) -> float:
    """Return cached sentiment score for a symbol. Alias for get_symbol_sentiment."""
    return get_symbol_sentiment(symbol)
