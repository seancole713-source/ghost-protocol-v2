"""News import format and normalization."""
import pytest

from core.news_store import _normalize_article, import_format_doc


def test_import_format_doc_has_required_fields():
    doc = import_format_doc()
    assert doc["version"] == 1
    assert "symbol" in doc["required_per_article"]
    assert "title" in doc["required_per_article"]
    assert "articles" in doc["example"]


def test_normalize_article_minimal():
    row = _normalize_article({"symbol": "abcl", "title": "Test headline"})
    assert row["symbol"] == "ABCL"
    assert row["title"] == "Test headline"
    assert row["category"] == "news"
    assert row["origin"] == "import"


def test_normalize_article_full():
    row = _normalize_article(
        {
            "symbol": "WOLF",
            "title": "Wolfspeed update",
            "summary": "Guidance revised.",
            "url": "https://example.com/wolf",
            "published_at": 1700000000,
            "source": "research_batch",
            "category": "earnings",
            "sentiment": 0.4,
        }
    )
    assert row["sentiment"] == 0.4
    assert row["category"] == "earnings"
    assert row["url"] == "https://example.com/wolf"


def test_normalize_article_requires_symbol_and_title():
    with pytest.raises(ValueError):
        _normalize_article({"title": "no symbol"})
    with pytest.raises(ValueError):
        _normalize_article({"symbol": "AI"})


def test_fetch_news_sentiment_wrapper(monkeypatch):
    from core.news_sentiment import fetch_news_sentiment
    import core.news as news

    news._symbol_sentiment["WOLF"] = 0.42
    monkeypatch.setattr("core.news_sentiment.list_articles", lambda **kw: [])
    out = fetch_news_sentiment("WOLF")
    assert out["ok"] is True
    assert out["sentiment_label"] == "BULLISH"
    assert out["sentiment_score"] == 0.42
