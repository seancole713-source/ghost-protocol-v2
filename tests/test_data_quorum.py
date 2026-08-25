"""tests/test_data_quorum.py — truthful multi-provider price quorum."""
from __future__ import annotations

import math
import threading
import time

import core.data_quorum as dq


def _now() -> int:
    return int(time.time())


def test_quorum_agrees_when_independent_families_close(monkeypatch):
    now = _now()
    monkeypatch.setattr(dq, "_provider_quotes", lambda sym: [
        ("alpaca", 10.0, now, "live", "alpaca"),
        ("yfinance", 10.1, now, "live", "yahoo"),
        ("iex", 9.95, now, "live", "alpaca"),
    ])
    out = dq.evaluate_quorum("WOLF")
    assert out["verdict"] == "agree"
    assert out["disagreeing"] == []
    assert out["independent_groups"] == 2
    assert abs(out["median_price"] - 10.0375) < 0.001


def test_quorum_disagrees_on_independent_divergence(monkeypatch):
    now = _now()
    monkeypatch.setattr(dq, "_provider_quotes", lambda sym: [
        ("alpaca", 10.0, now, "live", "alpaca"),
        ("yfinance", 20.0, now, "live", "yahoo"),
    ])
    out = dq.evaluate_quorum("WOLF")
    assert out["verdict"] == "disagree"
    assert set(out["disagreeing_families"]) == {"alpaca", "yahoo"}


def test_quorum_insufficient_with_one_provider(monkeypatch):
    now = _now()
    monkeypatch.setattr(dq, "_provider_quotes", lambda sym: [
        ("alpaca", 10.0, now),
        ("yfinance", None, None),
        ("iex", None, None),
    ])
    out = dq.evaluate_quorum("WOLF")
    assert out["verdict"] == "insufficient"


def test_quorum_rejects_stale_future_missing_timestamp_and_invalid_prices(monkeypatch):
    now = _now()
    monkeypatch.setattr(dq, "_provider_quotes", lambda sym: [
        ("stale", 10.0, now - dq.MAX_FRESHNESS_S - 1, "live", "stale"),
        ("future", 10.0, now + dq.MAX_FUTURE_SKEW_S + 2, "live", "future"),
        ("missing_ts", 10.0, None, "live", "missing"),
        ("nan", math.nan, now, "live", "nan"),
        ("inf", math.inf, now, "live", "inf"),
        ("negative", -1.0, now, "live", "negative"),
    ])
    out = dq.evaluate_quorum("WOLF")
    assert out["verdict"] == "insufficient"
    assert out["independent_groups"] == 0
    assert not any(p["eligible"] for p in out["providers"])


def test_polygon_previous_close_is_reference_only(monkeypatch):
    now = _now()
    monkeypatch.setattr(dq, "_provider_quotes", lambda sym: [
        ("alpaca", 10.0, now, "live", "alpaca"),
        ("polygon_prev", 50.0, now, "reference", "polygon"),
    ])
    out = dq.evaluate_quorum("WOLF")
    polygon = next(p for p in out["providers"] if p["name"] == "polygon_prev")
    assert polygon["exclusion_reason"] == "reference_only"
    assert out["independent_groups"] == 1
    assert out["median_price"] == 10.0


def test_quorum_uses_true_even_median_and_consolidates_alpaca_family(monkeypatch):
    now = _now()
    monkeypatch.setattr(dq, "_provider_quotes", lambda sym: [
        ("alpaca", 10.0, now, "live", "alpaca"),
        ("iex", 14.0, now, "live", "alpaca"),
        ("yfinance", 20.0, now, "live", "yahoo"),
    ])
    out = dq.evaluate_quorum("WOLF")
    assert out["family_votes"] == {"alpaca": 12.0, "yahoo": 20.0}
    assert out["median_price"] == 16.0
    assert out["independent_groups"] == 2


def test_positive_and_negative_cache(monkeypatch):
    dq.clear_quorum_cache()
    now = _now()
    calls = 0

    def quotes(sym):
        nonlocal calls
        calls += 1
        return [("alpaca", 10.0, now), ("yfinance", 10.1, now)]

    monkeypatch.setattr(dq, "_provider_quotes", quotes)
    assert dq.evaluate_quorum("WOLF", use_cache=True)["cache_state"] == "live"
    assert dq.evaluate_quorum("WOLF", use_cache=True)["cache_state"] == "cached"
    assert calls == 1

    dq.clear_quorum_cache()
    monkeypatch.setattr(dq, "_provider_quotes", lambda sym: [("alpaca", 10.0, now)])
    assert dq.evaluate_quorum("ARCT", use_cache=True)["verdict"] == "insufficient"
    assert dq.evaluate_quorum("ARCT", use_cache=True)["cache_state"] == "cached"


def test_single_flight_coalesces_same_symbol(monkeypatch):
    dq.clear_quorum_cache()
    now = _now()
    calls = 0
    entered = threading.Event()
    release = threading.Event()

    def quotes(sym):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(2)
        return [("alpaca", 10.0, now), ("yfinance", 10.1, now)]

    monkeypatch.setattr(dq, "_provider_quotes", quotes)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(dq.evaluate_quorum("WOLF", use_cache=True)))
        for _ in range(3)
    ]
    for thread in threads:
        thread.start()
    assert entered.wait(1)
    release.set()
    for thread in threads:
        thread.join(2)
    assert len(results) == 3
    assert calls == 1


def test_invalid_symbol_is_rejected():
    try:
        dq.evaluate_quorum("WOLF;DROP TABLE")
    except ValueError as exc:
        assert "invalid" in str(exc)
    else:
        raise AssertionError("invalid symbol accepted")
