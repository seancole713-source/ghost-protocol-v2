"""yfinance rate-limit budget: "CB yfinance: 15 calls in 60s -- rate-limit
circuit OPEN for 600s" every 15 min in production (2026-09-25).

Two causes, both fixed here:
  1. api/wolf_endpoints patches yfinance.Ticker process-wide with a wrapper that
     takes a _yfinance_cb slot. Nearly every production site already calls
     _yfinance_cb.allow() (or a separate breaker) first, so each real request
     was counted twice.
  2. core.prices._reject_phantom cross-checks every priced symbol against
     yfinance with a 900 s per-symbol cache; references set in one pass all
     expired together, so the next pricing pass burst one cross-check per
     symbol inside a minute.
"""
from __future__ import annotations

import sys
import types

import pytest

from core import prices as px
from core.circuit_breaker import _yfinance_cb


class _FastInfo:
    def __init__(self, price):
        self.last_price = price
        self.previous_close = price


def _fake_yf(monkeypatch, price=100.0):
    """A yfinance module whose Ticker is the breaker-gate wrapper."""
    real_calls, gate_calls = [], []

    class _RealTicker:
        def __init__(self, symbol):
            real_calls.append(symbol)
            self.fast_info = _FastInfo(price)

    def _gate(symbol):
        gate_calls.append(symbol)
        return _RealTicker(symbol)

    _gate._ghost_breaker_gate = True
    mod = types.ModuleType("yfinance")
    mod.Ticker = _gate
    mod._ghost_ungated_Ticker = _RealTicker
    monkeypatch.setitem(sys.modules, "yfinance", mod)
    return real_calls, gate_calls


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    px._cross_check_cache.clear()
    px._cross_check_calls.clear()
    _yfinance_cb.reset()
    monkeypatch.setattr(px, "PRICE_SANITY_FAIL_CLOSED", False)
    monkeypatch.setattr(px, "PRICE_SANITY_DIVERGENCE_PCT", 50.0)
    yield
    px._cross_check_cache.clear()
    px._cross_check_calls.clear()
    _yfinance_cb.reset()


def test_ungated_ticker_bypasses_only_the_gate_wrapper(monkeypatch):
    from core.yfinance_client import ungated_ticker

    real_calls, gate_calls = _fake_yf(monkeypatch)
    ungated_ticker("AAPL")
    assert real_calls == ["AAPL"] and gate_calls == []

    # A test double (or no wrapper at all) installed as Ticker is honoured.
    seen = []
    sys.modules["yfinance"].Ticker = lambda s: seen.append(s) or "double"
    assert ungated_ticker("MSFT") == "double"
    assert seen == ["MSFT"]


def test_wolf_endpoints_gate_is_marked_and_never_wraps_itself():
    import yfinance
    import api.wolf_endpoints  # noqa: F401  (installs the process-wide gate)

    assert getattr(yfinance.Ticker, "_ghost_breaker_gate", False) is True
    original = yfinance._ghost_ungated_Ticker
    assert not getattr(original, "_ghost_breaker_gate", False)


def test_cross_check_takes_exactly_one_breaker_slot(monkeypatch):
    real_calls, gate_calls = _fake_yf(monkeypatch, price=100.0)
    out, rejected = px._reject_phantom("AAPL", 101.0)
    assert (out, rejected) == (101.0, False)
    assert real_calls == ["AAPL"]
    assert gate_calls == []  # not double-counted by the process-wide gate
    assert _yfinance_cb.status()["recent_calls"] == 1


def test_expired_references_do_not_burst_the_breaker(monkeypatch):
    """15 symbols whose references expired together (the portfolio refresh
    case): at most PRICE_SANITY_CROSS_CHECK_MAX_PER_MIN cross-checks per
    minute, the breaker stays closed, and every price is still served."""
    monkeypatch.setattr(px, "PRICE_SANITY_CROSS_CHECK_MAX_PER_MIN", 6)
    monkeypatch.setattr(_yfinance_cb, "rate_limit_max_calls", 15)
    real_calls, _gate_calls = _fake_yf(monkeypatch, price=50.0)
    symbols = [f"S{i:02d}" for i in range(15)]
    served = [px._reject_phantom(sym, 50.5)[0] for sym in symbols]
    assert served == [50.5] * 15  # fail-open, exactly as when yfinance is unavailable
    assert len(real_calls) == 6
    assert _yfinance_cb.state == "closed"
    assert _yfinance_cb.status()["recent_calls"] == 6


def test_over_budget_reuses_a_recent_reference_to_catch_phantoms(monkeypatch):
    monkeypatch.setattr(px, "PRICE_SANITY_CROSS_CHECK_MAX_PER_MIN", 1)
    real_calls, _ = _fake_yf(monkeypatch)
    now = px.time.time()
    # MU's reference is past the 15-min TTL but inside the grace window.
    px._cross_check_cache["MU"] = (now - px.PRICE_SANITY_CROSS_CHECK_TTL_S - 60, 100.0)
    px._cross_check_calls.append(now)  # this minute's single slot is spent
    out, rejected = px._reject_phantom("MU", 993.42)
    assert (out, rejected) == (None, True)
    assert real_calls == []


def test_over_budget_without_reference_follows_fail_closed_setting(monkeypatch):
    monkeypatch.setattr(px, "PRICE_SANITY_CROSS_CHECK_MAX_PER_MIN", 1)
    monkeypatch.setattr(px, "PRICE_SANITY_FAIL_CLOSED", True)
    _fake_yf(monkeypatch)
    px._cross_check_calls.append(px.time.time())
    assert px._reject_phantom("NEWCO", 12.0) == (None, True)
