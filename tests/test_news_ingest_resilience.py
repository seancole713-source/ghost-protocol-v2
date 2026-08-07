"""Quota-aware structured news polling."""
import core.news_ingest as ingest


def test_finnhub_batch_rotates_with_bounded_budget(monkeypatch):
    monkeypatch.setenv("NEWS_INGEST_FINNHUB_SYMBOLS_PER_CYCLE", "2")
    monkeypatch.setattr(ingest, "_finnhub_symbol_cursor", 0)
    symbols = ["A", "B", "C"]

    assert ingest._next_finnhub_batch(symbols) == ["A", "B"]
    assert ingest._next_finnhub_batch(symbols) == ["C", "A"]


def test_finnhub_429_is_explicit(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "test")

    class Response:
        status_code = 429

    monkeypatch.setattr(ingest.requests, "get", lambda *args, **kwargs: Response())

    try:
        ingest._fetch_finnhub("AAPL", 1)
    except ingest._FinnhubRateLimited:
        pass
    else:
        raise AssertionError("429 must stop the provider loop")
