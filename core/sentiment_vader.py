"""
core/sentiment_vader.py — Lightweight deterministic sentiment scorer.

Uses VADER (Valence Aware Dictionary and sEntiment Reasoner) from NLTK —
a rule-based sentiment analyzer tuned for social media and short text that
handles financial headlines reasonably well. No GPU, no API calls, no cost.

Feature-flagged: set SENTIMENT_ENGINE=vader to use this instead of Claude Haiku.
Default is "claude" (existing behavior). VADER is deterministic — same input
always produces the same score, unlike Claude which has sampling variance.

Install: pip install vaderSentiment (already in requirements.txt if enabled)
"""
from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional

LOGGER = logging.getLogger("ghost.sentiment")

# Which sentiment engine to use: "claude" (default) or "vader"
_SENTIMENT_ENGINE = (os.getenv("SENTIMENT_ENGINE", "claude") or "claude").strip().lower()


def sentiment_engine() -> str:
    """Active sentiment engine name for /api/ghost/blueprint."""
    return _SENTIMENT_ENGINE


def is_vader_enabled() -> bool:
    return _SENTIMENT_ENGINE == "vader"


def score_headlines_vader(articles: List[Dict]) -> Dict[str, float]:
    """Score headlines per symbol using VADER sentiment.

    Args:
        articles: list of dicts with 'title' and 'symbols' keys

    Returns:
        {SYMBOL: score} where score is -1.0 (bearish) to +1.0 (bullish).
        Empty dict if vaderSentiment is not installed.
    """
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    except ImportError:
        LOGGER.warning("vaderSentiment not installed — run: pip install vaderSentiment")
        return {}

    analyzer = SentimentIntensityAnalyzer()

    # Collect headlines per symbol
    sym_headlines: Dict[str, list] = {}
    for a in articles or []:
        title = a.get("title", "")
        if not title:
            continue
        syms = a.get("symbols", [])
        for sym in syms:
            s = str(sym).upper()
            if s not in sym_headlines:
                sym_headlines[s] = []
            sym_headlines[s].append(title)

    # Score each symbol's headlines
    scores: Dict[str, float] = {}
    for sym, headlines in sym_headlines.items():
        if not headlines:
            continue
        # VADER returns {'neg': 0.x, 'neu': 0.y, 'pos': 0.z, 'compound': -1..1}
        compounds = []
        for h in headlines:
            vs = analyzer.polarity_scores(h)
            compounds.append(vs["compound"])
        # Average compound score across headlines
        avg = sum(compounds) / len(compounds)
        # Clamp to [-1, 1]
        scores[sym] = round(max(-1.0, min(1.0, avg)), 3)

    return scores


def score_headlines_keyword_enhanced(articles: List[Dict]) -> Dict[str, float]:
    """Enhanced keyword scorer — same lexicon as core/news_sentiment.py but
    with intensity modifiers and negation handling. Deterministic fallback
    when neither Claude nor VADER is available.
    """
    from core.news_sentiment import score_articles as _kw_score
    return _kw_score(articles)


def score_headlines(articles: List[Dict]) -> Dict[str, float]:
    """Route to the active sentiment engine.

    Returns {SYMBOL: score} where score is -1.0 to +1.0.
    """
    engine = _SENTIMENT_ENGINE
    if engine == "vader":
        result = score_headlines_vader(articles)
        if result:
            return result
        LOGGER.info("VADER unavailable, falling back to keyword scorer")
        return score_headlines_keyword_enhanced(articles)
    # "claude" or unknown — caller should use Claude; this is the fallback path
    return score_headlines_keyword_enhanced(articles)
