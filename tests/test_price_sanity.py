"""Price sanity guard — reject phantom quotes (wrong security / stale feed).

Catches the MU/SNDK class of bug where Alpaca IEX serves a foreign-listed twin
at ~10x the real price. The guard cross-checks against an independent source
(yfinance) and rejects when the candidate diverges by more than
PRICE_SANITY_DIVERGENCE_PCT.
"""
from core import prices as px


def test_reject_phantom_rejects_large_divergence(monkeypatch):
    px._cross_check_cache.clear()
    monkeypatch.setattr(px, "PRICE_SANITY_DIVERGENCE_PCT", 50.0)
    # Independent reference says ~$100; candidate says ~$993 (10x off).
    monkeypatch.setattr(px, "_cross_check_cache", {"MU": (px.time.time(), 100.0)})
    out, rejected = px._reject_phantom("MU", 993.42)
    assert out is None
    assert rejected is True


def test_reject_phantom_accepts_close_price(monkeypatch):
    px._cross_check_cache.clear()
    monkeypatch.setattr(px, "PRICE_SANITY_DIVERGENCE_PCT", 50.0)
    monkeypatch.setattr(px, "_cross_check_cache", {"AAPL": (px.time.time(), 305.0)})
    out, rejected = px._reject_phantom("AAPL", 307.04)
    assert out == 307.04
    assert rejected is False


def test_reject_phantom_fail_open_without_reference(monkeypatch):
    px._cross_check_cache.clear()
    monkeypatch.setattr(px, "_cross_check_cache", {})
    # No independent reference available → fail-open (return the price).
    out, rejected = px._reject_phantom("WOLF", 31.81)
    assert out == 31.81
    assert rejected is False


def test_reject_phantom_handles_garbage(monkeypatch):
    px._cross_check_cache.clear()
    assert px._reject_phantom("X", None) == (None, False)
    assert px._reject_phantom("X", "abc") == (None, False)
    assert px._reject_phantom("X", 0) == (None, False)
    assert px._reject_phantom("X", -5) == (None, False)


def test_get_stock_price_rejects_phantom_then_falls_back(monkeypatch):
    """When Alpaca returns a phantom price, get_stock_price must reject it and
    fall through to yfinance instead of caching the bad value."""
    px._mem_cache.clear()
    px._cross_check_cache.clear()
    monkeypatch.setattr(px, "PRICE_SANITY_DIVERGENCE_PCT", 50.0)
    monkeypatch.setattr(px, "_cross_check_cache", {"MU": (px.time.time(), 100.0)})
    monkeypatch.setattr(px, "_alpaca", lambda s: 993.42)  # phantom
    monkeypatch.setattr(px, "_yfinance", lambda s: 101.5)  # sane fallback
    monkeypatch.setattr(px, "_iex_spot", lambda s: None)
    out = px.get_stock_price("MU")
    assert out == 101.5
    # The phantom value must NOT be cached.
    assert px._mem_cache.get("MU", (None,))[0] == 101.5
