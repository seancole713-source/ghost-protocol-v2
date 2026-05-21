"""
World Feed Fusion - RSS + NLP Sentiment Analysis
APEX Feature #8: +20% Event Awareness

Aggregates financial news from multiple sources (Reuters, Bloomberg, FT, MarketWatch, WSJ, CNBC)
and performs real-time NLP sentiment analysis. Feeds sentiment scores into NewsShockStrategy
and Feature Importance analysis.

Architecture:
- RSS feed ingestion with 15min refresh cycle
- Multi-source sentiment aggregation
- SQLite persistence for historical analysis
- Real-time sentiment scoring (-1 to +1 scale)
- Integration with existing GHOST systems

Author: GHOST APEX v11.0.0
Date: 2025-10-06
"""

import hashlib
import importlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum

# Try importing feedparser and NLP libraries
try:
    import feedparser

    FEEDPARSER_AVAILABLE = True
except ImportError:
    FEEDPARSER_AVAILABLE = False
    logging.warning("feedparser not available - RSS features disabled")

try:
    from textblob import TextBlob

    TEXTBLOB_AVAILABLE = True
except ImportError:
    TEXTBLOB_AVAILABLE = False
    logging.warning("TextBlob not available - using simple sentiment analysis")
pipeline = None

try:
    transformers_module = importlib.import_module("transformers")
    pipeline_candidate = getattr(transformers_module, "pipeline", None)
    if callable(pipeline_candidate):
        pipeline = pipeline_candidate
        TRANSFORMERS_AVAILABLE = True
    else:
        TRANSFORMERS_AVAILABLE = False
        logging.warning("transformers.pipeline not available - using TextBlob fallback")
except ImportError:
    TRANSFORMERS_AVAILABLE = False
    logging.warning("transformers not available - using TextBlob fallback")
    logging.warning("transformers not available - using TextBlob fallback")


LOGGER = logging.getLogger(__name__)


class SentimentSource(Enum):
    """Sentiment analysis source/method"""

    TEXTBLOB = "textblob"
    TRANSFORMERS = "transformers"
    SIMPLE = "simple"
    AGGREGATE = "aggregate"


class NewsCategory(Enum):
    """News article categories"""

    EARNINGS = "earnings"
    MARKETS = "markets"
    POLICY = "policy"
    COMPANY = "company"
    ECONOMIC = "economic"
    BREAKING = "breaking"
    ANALYSIS = "analysis"
    GENERAL = "general"


@dataclass
class FeedSource:
    """RSS feed source configuration"""

    source_id: str
    name: str
    url: str
    category: str
    priority: int  # 1-10, higher = more trusted
    refresh_interval: int  # seconds
    last_fetched: int
    is_active: bool
    error_count: int


@dataclass
class NewsArticle:
    """Individual news article with metadata"""

    article_id: str  # hash of URL
    source_id: str
    title: str
    summary: str
    url: str
    published: int  # Unix timestamp
    fetched: int
    category: str
    sentiment_score: float  # -1.0 to +1.0
    sentiment_source: str
    magnitude: float  # 0.0 to 1.0 (confidence)
    symbols: list[str]  # Extracted ticker symbols
    keywords: list[str]


@dataclass
class SentimentAggregate:
    """Aggregated sentiment across multiple articles"""

    symbol: str
    timeframe: str  # "1h", "6h", "1d", "7d"
    avg_sentiment: float
    article_count: int
    bullish_count: int
    bearish_count: int
    neutral_count: int
    weighted_sentiment: float  # Weighted by source priority
    confidence: float
    calculated_at: int


class WorldFeedFusion:
    """
    World Feed Fusion system for real-time news sentiment analysis.

    Capabilities:
    - Multi-source RSS feed aggregation
    - NLP sentiment analysis (TextBlob + transformers)
    - Symbol extraction and categorization
    - Historical sentiment tracking
    - Integration with trading strategies
    """

    # Default RSS feed sources
    DEFAULT_SOURCES = [
        {
            "source_id": "reuters_markets",
            "name": "Reuters Markets",
            "url": "https://www.reuters.com/finance/markets/rss",
            "category": "markets",
            "priority": 9,
            "refresh_interval": 900,  # 15 minutes
        },
        {
            "source_id": "reuters_companies",
            "name": "Reuters Companies",
            "url": "https://www.reuters.com/finance/companies/rss",
            "category": "company",
            "priority": 9,
            "refresh_interval": 900,
        },
        {
            "source_id": "bloomberg_markets",
            "name": "Bloomberg Markets",
            "url": "https://www.bloomberg.com/feed/podcast/market-insights.rss",
            "category": "markets",
            "priority": 10,
            "refresh_interval": 900,
        },
        {
            "source_id": "ft_markets",
            "name": "Financial Times Markets",
            "url": "https://www.ft.com/markets?format=rss",
            "category": "markets",
            "priority": 9,
            "refresh_interval": 1800,  # 30 minutes
        },
        {
            "source_id": "marketwatch",
            "name": "MarketWatch",
            "url": "http://feeds.marketwatch.com/marketwatch/topstories/",
            "category": "general",
            "priority": 7,
            "refresh_interval": 900,
        },
        {
            "source_id": "wsj_markets",
            "name": "Wall Street Journal Markets",
            "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
            "category": "markets",
            "priority": 9,
            "refresh_interval": 900,
        },
        {
            "source_id": "cnbc",
            "name": "CNBC Top News",
            "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
            "category": "general",
            "priority": 8,
            "refresh_interval": 600,  # 10 minutes
        },
        {
            "source_id": "seeking_alpha",
            "name": "Seeking Alpha Market News",
            "url": "https://seekingalpha.com/market_currents.xml",
            "category": "analysis",
            "priority": 7,
            "refresh_interval": 1200,  # 20 minutes
        },
        # ── WOLF-SPECIFIC FEEDS (added Phase 2, 2026-05-21) ───────────────────
        # Ghost is now WOLF-only. These feeds provide direct Wolfspeed coverage.
        {
            "source_id": "wolf_google_news",
            "name": "Google News — Wolfspeed",
            "url": "https://news.google.com/rss/search?q=Wolfspeed+WOLF+stock&hl=en-US&gl=US&ceid=US:en",
            "category": "wolf",
            "priority": 10,
            "refresh_interval": 600,   # 10 minutes — highest priority
        },
        {
            "source_id": "wolf_sic_news",
            "name": "Google News — Silicon Carbide Industry",
            "url": "https://news.google.com/rss/search?q=silicon+carbide+SiC+semiconductor+EV&hl=en-US&gl=US&ceid=US:en",
            "category": "wolf",
            "priority": 9,
            "refresh_interval": 900,
        },
        {
            "source_id": "wolf_contracts",
            "name": "Google News — Wolfspeed Contracts/Customers",
            "url": "https://news.google.com/rss/search?q=Wolfspeed+contract+customer+GM+Mercedes+BorgWarner&hl=en-US&gl=US&ceid=US:en",
            "category": "wolf",
            "priority": 10,
            "refresh_interval": 900,
        },
        {
            "source_id": "eetimes_power",
            "name": "EE Times — Power/SiC",
            "url": "https://www.eetimes.com/category/power-management/feed/",
            "category": "wolf",
            "priority": 8,
            "refresh_interval": 1800,
        },
        {
            "source_id": "wolf_doe_doe",
            "name": "Google News — DOE Wolfspeed Funding",
            "url": "https://news.google.com/rss/search?q=Wolfspeed+DOE+Department+Energy+grant+funding&hl=en-US&gl=US&ceid=US:en",
            "category": "wolf",
            "priority": 9,
            "refresh_interval": 1800,
        },
    ]

    def __init__(self, db_path: str = "data/wolf.db"):
        """Initialize World Feed Fusion system"""
        self.db_path = db_path
        self.sentiment_model = None
        self._init_db()
        self._init_sentiment_analyzer()
        self._load_or_create_sources()

    def _init_db(self):
        """Initialize SQLite database schema"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Feed sources table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS feed_sources (
                source_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                category TEXT,
                priority INTEGER DEFAULT 5,
                refresh_interval INTEGER DEFAULT 900,
                last_fetched INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                error_count INTEGER DEFAULT 0,
                created_at INTEGER DEFAULT (strftime('%s', 'now'))
            )
        """)

        # News articles table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS news_articles (
                article_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT,
                url TEXT UNIQUE,
                published INTEGER,
                fetched INTEGER,
                category TEXT,
                sentiment_score REAL,
                sentiment_source TEXT,
                magnitude REAL,
                symbols TEXT,
                keywords TEXT,
                FOREIGN KEY (source_id) REFERENCES feed_sources(source_id)
            )
        """)

        # Create index on published date for fast queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_articles_published
            ON news_articles(published DESC)
        """)

        # Create index on symbols for symbol-specific queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_articles_symbols
            ON news_articles(symbols)
        """)

        # Sentiment aggregates table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sentiment_aggregates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                timeframe TEXT,
                avg_sentiment REAL,
                article_count INTEGER,
                bullish_count INTEGER,
                bearish_count INTEGER,
                neutral_count INTEGER,
                weighted_sentiment REAL,
                confidence REAL,
                calculated_at INTEGER
            )
        """)

        # Create index on symbol + calculated_at for fast lookups
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_aggregates_symbol_time
            ON sentiment_aggregates(symbol, calculated_at DESC)
        """)

        conn.commit()
        conn.close()
        LOGGER.info("World Feed Fusion database initialized")

    def _init_sentiment_analyzer(self):
        """Initialize NLP sentiment analysis model"""
        if TRANSFORMERS_AVAILABLE and callable(pipeline):
            try:
                # Use FinBERT for financial sentiment (best for finance)
                self.sentiment_model = pipeline(
                    "sentiment-analysis", model="ProsusAI/finbert", top_k=None
                )
                self.sentiment_method = SentimentSource.TRANSFORMERS
                LOGGER.info("Using FinBERT (transformers) for sentiment analysis")
                return
            except Exception as e:
                LOGGER.warning(f"Failed to load FinBERT: {e}, falling back to TextBlob")
        elif TRANSFORMERS_AVAILABLE:
            LOGGER.warning("transformers pipeline callable unavailable - falling back to TextBlob")

        if TEXTBLOB_AVAILABLE:
            self.sentiment_method = SentimentSource.TEXTBLOB
            LOGGER.info("Using TextBlob for sentiment analysis")
        else:
            self.sentiment_method = SentimentSource.SIMPLE
            LOGGER.info("Using simple keyword-based sentiment analysis")

    def _load_or_create_sources(self):
        """Load existing sources or create defaults"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Check if sources exist
        cursor.execute("SELECT COUNT(*) FROM feed_sources")
        count = cursor.fetchone()[0]

        if count == 0:
            # Insert default sources
            for source in self.DEFAULT_SOURCES:
                cursor.execute(
                    """
                    INSERT INTO feed_sources
                    (source_id, name, url, category, priority, refresh_interval, is_active)
                    VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                    (
                        source["source_id"],
                        source["name"],
                        source["url"],
                        source["category"],
                        source["priority"],
                        source["refresh_interval"],
                    ),
                )
            conn.commit()
            LOGGER.info(f"Initialized {len(self.DEFAULT_SOURCES)} default feed sources")

        conn.close()

    def _generate_article_id(self, url: str) -> str:
        """Generate unique article ID from URL"""
        return hashlib.md5(url.encode()).hexdigest()

    def _extract_symbols(self, text: str) -> list[str]:
        """Extract ticker symbols from text (simple regex)"""
        # Match $SYMBOL or uppercase 1-5 letter words
        pattern = r"\$([A-Z]{1,5})\b|(?<!\w)([A-Z]{2,5})(?=\s|,|\.|\))"
        matches = re.findall(pattern, text)
        symbols = [m[0] or m[1] for m in matches]

        # Filter common words that look like symbols
        blacklist = {
            "THE",
            "AND",
            "FOR",
            "ARE",
            "BUT",
            "NOT",
            "YOU",
            "ALL",
            "CAN",
            "HER",
            "WAS",
            "ONE",
            "OUR",
            "OUT",
            "DAY",
            "GET",
            "HAS",
            "HIM",
            "HIS",
            "HOW",
            "MAN",
            "NEW",
            "NOW",
            "OLD",
            "SEE",
            "TWO",
            "WAY",
            "WHO",
            "BOY",
            "ITS",
            "LET",
            "PUT",
            "SAY",
            "SHE",
            "TOO",
            "USE",
        }
        symbols = [s for s in symbols if s not in blacklist]

        return list(set(symbols))[:10]  # Max 10 symbols per article

    def _extract_keywords(self, text: str) -> list[str]:
        """Extract important keywords from text"""
        # Simple frequency-based extraction
        words = re.findall(r"\b[a-z]{4,}\b", text.lower())

        # Common financial keywords get boosted
        important_keywords = {
            "earnings",
            "revenue",
            "profit",
            "loss",
            "growth",
            "decline",
            "merger",
            "acquisition",
            "dividend",
            "buyback",
            "guidance",
            "inflation",
            "interest",
            "federal",
            "reserve",
            "market",
            "stock",
        }

        # Count frequencies
        freq = {}
        for word in words:
            freq[word] = freq.get(word, 0) + 1

        # Boost important keywords
        for word in important_keywords:
            if word in freq:
                freq[word] *= 3

        # Sort by frequency and return top 10
        sorted_words = sorted(freq.items(), key=lambda x: x[1], reverse=True)
        return [word for word, _ in sorted_words[:10]]

    def _analyze_sentiment_simple(self, text: str) -> tuple[float, float]:
        """Simple keyword-based sentiment analysis (fallback)"""
        text_lower = text.lower()

        bullish_keywords = [
            "surge",
            "rally",
            "gain",
            "rise",
            "up",
            "growth",
            "profit",
            "beat",
            "strong",
            "positive",
            "bull",
            "upgrade",
            "outperform",
            "buy",
            "boom",
        ]
        bearish_keywords = [
            "plunge",
            "crash",
            "fall",
            "drop",
            "down",
            "loss",
            "miss",
            "weak",
            "negative",
            "bear",
            "downgrade",
            "sell",
            "decline",
            "slump",
            "worry",
        ]

        bullish_count = sum(1 for kw in bullish_keywords if kw in text_lower)
        bearish_count = sum(1 for kw in bearish_keywords if kw in text_lower)

        total = bullish_count + bearish_count
        if total == 0:
            return 0.0, 0.0  # Neutral

        score = (bullish_count - bearish_count) / total
        magnitude = min(total / 5.0, 1.0)  # Confidence based on keyword count

        return score, magnitude

    def _analyze_sentiment_textblob(self, text: str) -> tuple[float, float]:
        """Sentiment analysis using TextBlob"""
        try:
            blob = TextBlob(text)
            # Polarity is -1 to 1, subjectivity is 0 to 1
            polarity = blob.sentiment.polarity  # type: ignore
            magnitude = blob.sentiment.subjectivity  # type: ignore
            return polarity, magnitude
        except Exception as e:
            LOGGER.error(f"TextBlob analysis failed: {e}")
            return self._analyze_sentiment_simple(text)

    def _analyze_sentiment_transformers(self, text: str) -> tuple[float, float]:
        """Sentiment analysis using transformers (FinBERT)"""
        try:
            # Truncate to 512 tokens (BERT limit)
            text = text[:2000]

            result = self.sentiment_model(text)[0]  # type: ignore

            # FinBERT returns positive, negative, neutral scores
            sentiment_map = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}

            # Weighted average of scores
            score = 0.0
            confidence = 0.0
            for item in result:
                label = item["label"].lower()
                prob = item["score"]
                if label in sentiment_map:
                    score += sentiment_map[label] * prob
                    confidence = max(confidence, prob)

            return score, confidence
        except Exception as e:
            LOGGER.error(f"Transformers analysis failed: {e}")
            return self._analyze_sentiment_textblob(text)

    def analyze_sentiment(self, text: str) -> tuple[float, float]:
        """
        Analyze sentiment of text.

        Returns:
            (score, magnitude) where score is -1.0 to 1.0 and magnitude is 0.0 to 1.0
        """
        if not text:
            return 0.0, 0.0

        if self.sentiment_method == SentimentSource.TRANSFORMERS:
            return self._analyze_sentiment_transformers(text)
        elif self.sentiment_method == SentimentSource.TEXTBLOB:
            return self._analyze_sentiment_textblob(text)
        else:
            return self._analyze_sentiment_simple(text)

    def fetch_feed(self, source_id: str) -> list[NewsArticle]:
        """
        Fetch articles from a specific RSS feed source.

        Args:
            source_id: Source identifier

        Returns:
            List of NewsArticle objects
        """
        if not FEEDPARSER_AVAILABLE:
            LOGGER.error("feedparser not available - cannot fetch feeds")
            return []

        # Get source info from database
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT url, category, priority
            FROM feed_sources
            WHERE source_id = ? AND is_active = 1
        """,
            (source_id,),
        )

        row = cursor.fetchone()
        if not row:
            conn.close()
            LOGGER.warning(f"Source {source_id} not found or inactive")
            return []

        url, category, priority = row

        # Fetch feed
        try:
            feed = feedparser.parse(url)
            articles = []

            for entry in feed.entries[:50]:  # Limit to 50 most recent
                # Extract article data with explicit type conversions
                title = str(entry.get("title", "") or "")
                summary = str(entry.get("summary", entry.get("description", "")) or "")
                article_url = str(entry.get("link", "") or "")

                if not article_url:
                    continue

                # Parse published date
                published_struct = entry.get("published_parsed") or entry.get("updated_parsed")
                if published_struct and hasattr(published_struct, "tm_year"):
                    published = int(time.mktime(published_struct))  # type: ignore
                else:
                    published = int(time.time())

                # Generate article ID
                article_id = self._generate_article_id(article_url)

                # Check if article already exists
                cursor.execute(
                    """
                    SELECT article_id FROM news_articles WHERE article_id = ?
                """,
                    (article_id,),
                )

                if cursor.fetchone():
                    continue  # Skip existing articles

                # Analyze sentiment
                full_text = f"{title}. {summary}"
                sentiment_score, magnitude = self.analyze_sentiment(full_text)

                # Extract symbols and keywords
                symbols = self._extract_symbols(full_text)
                keywords = self._extract_keywords(full_text)

                # Create article object
                article = NewsArticle(
                    article_id=article_id,
                    source_id=source_id,
                    title=title,
                    summary=summary,
                    url=article_url,
                    published=published,
                    fetched=int(time.time()),
                    category=category,
                    sentiment_score=sentiment_score,
                    sentiment_source=self.sentiment_method.value,
                    magnitude=magnitude,
                    symbols=symbols,
                    keywords=keywords,
                )

                articles.append(article)

                # Insert into database
                cursor.execute(
                    """
                    INSERT INTO news_articles
                    (article_id, source_id, title, summary, url, published, fetched,
                     category, sentiment_score, sentiment_source, magnitude, symbols, keywords)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        article.article_id,
                        article.source_id,
                        article.title,
                        article.summary,
                        article.url,
                        article.published,
                        article.fetched,
                        article.category,
                        article.sentiment_score,
                        article.sentiment_source,
                        article.magnitude,
                        json.dumps(article.symbols),
                        json.dumps(article.keywords),
                    ),
                )

            # Update last_fetched timestamp
            cursor.execute(
                """
                UPDATE feed_sources
                SET last_fetched = ?, error_count = 0
                WHERE source_id = ?
            """,
                (int(time.time()), source_id),
            )

            conn.commit()
            conn.close()

            LOGGER.info(f"Fetched {len(articles)} new articles from {source_id}")
            return articles

        except Exception as e:
            # Increment error count
            cursor.execute(
                """
                UPDATE feed_sources
                SET error_count = error_count + 1
                WHERE source_id = ?
            """,
                (source_id,),
            )
            conn.commit()
            conn.close()

            LOGGER.error(f"Failed to fetch feed {source_id}: {e}")
            return []

    def fetch_all_feeds(self) -> int:
        """
        Fetch articles from all active feed sources.

        Returns:
            Total number of new articles fetched
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Get sources that need refresh
        now = int(time.time())
        cursor.execute(
            """
            SELECT source_id
            FROM feed_sources
            WHERE is_active = 1
            AND (last_fetched + refresh_interval) < ?
            AND error_count < 5
        """,
            (now,),
        )

        sources = [row[0] for row in cursor.fetchall()]
        conn.close()

        total_articles = 0
        for source_id in sources:
            articles = self.fetch_feed(source_id)
            total_articles += len(articles)

        if total_articles > 0:
            # Recalculate aggregates after fetching new articles
            self._update_all_aggregates()

        LOGGER.info(f"Fetched {total_articles} total new articles from {len(sources)} sources")
        return total_articles

    def get_latest_articles(self, limit: int = 20, symbol: str | None = None) -> list[dict]:
        """
        Get latest news articles.

        Args:
            limit: Maximum number of articles
            symbol: Filter by ticker symbol (optional)

        Returns:
            List of article dictionaries
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        if symbol:
            cursor.execute(
                """
                SELECT article_id, source_id, title, summary, url, published,
                       sentiment_score, magnitude, symbols, keywords
                FROM news_articles
                WHERE symbols LIKE ?
                ORDER BY published DESC
                LIMIT ?
            """,
                (f'%"{symbol}"%', limit),
            )
        else:
            cursor.execute(
                """
                SELECT article_id, source_id, title, summary, url, published,
                       sentiment_score, magnitude, symbols, keywords
                FROM news_articles
                ORDER BY published DESC
                LIMIT ?
            """,
                (limit,),
            )

        articles = []
        for row in cursor.fetchall():
            articles.append(
                {
                    "article_id": row[0],
                    "source_id": row[1],
                    "title": row[2],
                    "summary": row[3],
                    "url": row[4],
                    "published": row[5],
                    "sentiment_score": row[6],
                    "magnitude": row[7],
                    "symbols": json.loads(row[8]) if row[8] else [],
                    "keywords": json.loads(row[9]) if row[9] else [],
                }
            )

        conn.close()
        return articles

    def get_sentiment_aggregate(
        self, symbol: str, timeframe: str = "1d"
    ) -> SentimentAggregate | None:
        """
        Get aggregated sentiment for a symbol over a timeframe.

        Args:
            symbol: Ticker symbol
            timeframe: "1h", "6h", "1d", "7d"

        Returns:
            SentimentAggregate object or None
        """
        # Check cache first
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Get most recent aggregate (within last 15 minutes)
        cache_cutoff = int(time.time()) - 900
        cursor.execute(
            """
            SELECT avg_sentiment, article_count, bullish_count, bearish_count,
                   neutral_count, weighted_sentiment, confidence, calculated_at
            FROM sentiment_aggregates
            WHERE symbol = ? AND timeframe = ? AND calculated_at > ?
            ORDER BY calculated_at DESC
            LIMIT 1
        """,
            (symbol, timeframe, cache_cutoff),
        )

        row = cursor.fetchone()
        if row:
            conn.close()
            return SentimentAggregate(
                symbol=symbol,
                timeframe=timeframe,
                avg_sentiment=row[0],
                article_count=row[1],
                bullish_count=row[2],
                bearish_count=row[3],
                neutral_count=row[4],
                weighted_sentiment=row[5],
                confidence=row[6],
                calculated_at=row[7],
            )

        # Calculate fresh aggregate
        conn.close()
        return self._calculate_sentiment_aggregate(symbol, timeframe)

    def _calculate_sentiment_aggregate(
        self, symbol: str, timeframe: str
    ) -> SentimentAggregate | None:
        """Calculate sentiment aggregate from scratch"""
        # Convert timeframe to seconds
        timeframe_seconds = {"1h": 3600, "6h": 21600, "1d": 86400, "7d": 604800}.get(
            timeframe, 86400
        )

        cutoff = int(time.time()) - timeframe_seconds

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Get articles mentioning symbol within timeframe
        cursor.execute(
            """
            SELECT a.sentiment_score, a.magnitude, f.priority
            FROM news_articles a
            JOIN feed_sources f ON a.source_id = f.source_id
            WHERE a.symbols LIKE ? AND a.published > ?
        """,
            (f'%"{symbol}"%', cutoff),
        )

        rows = cursor.fetchall()

        if not rows:
            conn.close()
            return None

        # Calculate statistics
        sentiments = [row[0] for row in rows]
        magnitudes = [row[1] for row in rows]
        priorities = [row[2] for row in rows]

        avg_sentiment = sum(sentiments) / len(sentiments)

        bullish_count = sum(1 for s in sentiments if s > 0.1)
        bearish_count = sum(1 for s in sentiments if s < -0.1)
        neutral_count = len(sentiments) - bullish_count - bearish_count

        # Weighted sentiment (by source priority)
        total_weight = sum(priorities)
        weighted_sentiment = (
            sum(s * p for s, p in zip(sentiments, priorities, strict=False)) / total_weight
        )

        # Confidence (average magnitude)
        confidence = sum(magnitudes) / len(magnitudes)

        # Create aggregate
        aggregate = SentimentAggregate(
            symbol=symbol,
            timeframe=timeframe,
            avg_sentiment=avg_sentiment,
            article_count=len(sentiments),
            bullish_count=bullish_count,
            bearish_count=bearish_count,
            neutral_count=neutral_count,
            weighted_sentiment=weighted_sentiment,
            confidence=confidence,
            calculated_at=int(time.time()),
        )

        # Save to database
        cursor.execute(
            """
            INSERT INTO sentiment_aggregates
            (symbol, timeframe, avg_sentiment, article_count, bullish_count,
             bearish_count, neutral_count, weighted_sentiment, confidence, calculated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                aggregate.symbol,
                aggregate.timeframe,
                aggregate.avg_sentiment,
                aggregate.article_count,
                aggregate.bullish_count,
                aggregate.bearish_count,
                aggregate.neutral_count,
                aggregate.weighted_sentiment,
                aggregate.confidence,
                aggregate.calculated_at,
            ),
        )

        conn.commit()
        conn.close()

        return aggregate

    def _update_all_aggregates(self):
        """Update aggregates for all recently mentioned symbols"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Get symbols mentioned in last 7 days
        cutoff = int(time.time()) - 604800
        cursor.execute(
            """
            SELECT DISTINCT symbols FROM news_articles WHERE published > ?
        """,
            (cutoff,),
        )

        all_symbols = set()
        for row in cursor.fetchall():
            if row[0]:
                symbols = json.loads(row[0])
                all_symbols.update(symbols)

        conn.close()

        # Update aggregates for top symbols
        for symbol in list(all_symbols)[:50]:  # Limit to 50 most active
            for timeframe in ["1h", "6h", "1d", "7d"]:
                self._calculate_sentiment_aggregate(symbol, timeframe)

    def search_articles(self, query: str, limit: int = 20) -> list[dict]:
        """
        Search articles by keyword.

        Args:
            query: Search query
            limit: Maximum results

        Returns:
            List of matching articles
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        query_pattern = f"%{query}%"
        cursor.execute(
            """
            SELECT article_id, source_id, title, summary, url, published,
                   sentiment_score, magnitude, symbols, keywords
            FROM news_articles
            WHERE title LIKE ? OR summary LIKE ? OR keywords LIKE ?
            ORDER BY published DESC
            LIMIT ?
        """,
            (query_pattern, query_pattern, query_pattern, limit),
        )

        articles = []
        for row in cursor.fetchall():
            articles.append(
                {
                    "article_id": row[0],
                    "source_id": row[1],
                    "title": row[2],
                    "summary": row[3],
                    "url": row[4],
                    "published": row[5],
                    "sentiment_score": row[6],
                    "magnitude": row[7],
                    "symbols": json.loads(row[8]) if row[8] else [],
                    "keywords": json.loads(row[9]) if row[9] else [],
                }
            )

        conn.close()
        return articles

    def get_sources(self) -> list[dict]:
        """Get all feed sources"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT source_id, name, url, category, priority,
                   refresh_interval, last_fetched, is_active, error_count
            FROM feed_sources
        """)

        sources = []
        for row in cursor.fetchall():
            sources.append(
                {
                    "source_id": row[0],
                    "name": row[1],
                    "url": row[2],
                    "category": row[3],
                    "priority": row[4],
                    "refresh_interval": row[5],
                    "last_fetched": row[6],
                    "is_active": bool(row[7]),
                    "error_count": row[8],
                }
            )

        conn.close()
        return sources


# Singleton instance
_feed_fusion_instance: WorldFeedFusion | None = None


def get_feed_fusion() -> WorldFeedFusion:
    """Get singleton WorldFeedFusion instance"""
    global _feed_fusion_instance
    if _feed_fusion_instance is None:
        _feed_fusion_instance = WorldFeedFusion()
    return _feed_fusion_instance
