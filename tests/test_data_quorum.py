"""tests/test_data_quorum.py — multi-provider price quorum."""
from __future__ import annotations

import core.data_quorum as dq


def test_quorum_agrees_when_providers_close(monkeypatch):
    monkeypatch.setattr(dq, "_provider_quotes", lambda sym: [
        ("alpaca", 10.0, 1780000000),
        ("yfinance", 10.1, 1780000000),
        ("iex", 9.95, 1780000000),
    ])
    out = dq.evaluate_quorum("WOLF")
    assert out["verdict"] == "agree"
    assert out["disagreeing"] == []
    assert abs(out["median_price"] - 10.0) < 0.01


def test_quorum_disagrees_on_divergence(monkeypatch):
    monkeypatch.setattr(dq, "_provider_quotes", lambda sym: [
        ("alpaca", 10.0, 1780000000),
        ("yfinance", 10.1, 1780000000),
        ("iex", 20.0, 1780000000),  # phantom 2x
    ])
    out = dq.evaluate_quorum("WOLF")
    assert out["verdict"] == "disagree"
    assert "iex" in out["disagreeing"]


def test_quorum_insufficient_with_one_provider(monkeypatch):
    monkeypatch.setattr(dq, "_provider_quotes", lambda sym: [
        ("alpaca", 10.0, 1780000000),
        ("yfinance", None, None),
        ("iex", None, None),
    ])
    out = dq.evaluate_quorum("WOLF")
    assert out["verdict"] == "insufficient"
