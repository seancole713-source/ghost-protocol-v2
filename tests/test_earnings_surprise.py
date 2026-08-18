"""Tests for core/earnings_surprise.py — pure mapping + fetch (mocked)."""
from core import earnings_surprise as es


def test_earnings_surprise_to_trigger_uses_relative_surprise(monkeypatch):
    """A smaller-than-expected loss still scores as a positive surprise."""
    monkeypatch.setattr(
        es, "get_earnings_surprise",
        lambda s: {
            "available": True,
            "eps_actual": -0.10,
            "eps_expected": -0.50,
            "revenue_actual": None,
            "quarter": "2026Q2",
        },
    )
    out = es.earnings_surprise_to_trigger("WOLF")
    assert out["earnings_available"] is True
    assert out["earnings_surprise"] > 50.0
    assert out["eps_surprise_pct"] == 80.0


def test_earnings_surprise_to_trigger_unavailable(monkeypatch):
    monkeypatch.setattr(es, "get_earnings_surprise", lambda s: {"available": False})
    out = es.earnings_surprise_to_trigger("WOLF")
    assert out["earnings_available"] is False
    assert out["earnings_surprise"] == 0.0


def test_get_earnings_surprise_empty_symbol():
    assert es.get_earnings_surprise("") == {"available": False}
