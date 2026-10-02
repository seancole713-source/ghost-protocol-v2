"""Core Polygon calls share one per-minute budget and back off after a 429.

Production: ~20 ``[ghost.signal_v3] Polygon <SYM>: HTTP 429 ... exceeded the
maximum requests per minute`` lines every hour from the hourly core scan. The
log shows the order: Alpaca SIP 429, Alpaca IEX 429 (same account limit),
Polygon 429, then the retry asks the whole chain again; the bars finally come
from Alpaca once its minute rolls.
"""
from __future__ import annotations

import logging

import pytest

from core import polygon_rate


class Clock:
    def __init__(self, t=1_000_040.0):        # 20 s into a wall-clock minute (1_000_020 = 16667 x 60)
        self.t = t

    def __call__(self):
        return self.t


class Resp:
    def __init__(self, status, bars=None, polygon=None):
        self.status_code = status
        self._bars = bars
        self._polygon = polygon
        self.text = '{"status":"ERROR","error":"You\'ve exceeded the maximum requests per minute"}'

    def json(self):
        if self._polygon is not None:
            return self._polygon
        return {"bars": self._bars}


def _polygon_ok():
    return {"status": "OK", "results": [
        {"t": 1_758_600_000_000 + i * 86_400_000, "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 1000}
        for i in range(30)]}


def test_token_bucket_allows_the_free_tier_rate_and_refills(monkeypatch):
    monkeypatch.delenv("POLYGON_MAX_RPM", raising=False)
    clock = Clock()
    b = polygon_rate.PolygonBudget(clock)
    assert [b.try_acquire() for _ in range(6)] == [True] * 5 + [False]
    clock.t += 12.0                                     # 5 per minute = one every 12 s
    assert b.try_acquire() is True
    assert b.try_acquire() is False
    assert b.skipped == 2


def test_max_rpm_is_configurable_and_zero_disables_the_bucket(monkeypatch):
    clock = Clock()
    monkeypatch.setenv("POLYGON_MAX_RPM", "2")
    b = polygon_rate.PolygonBudget(clock)
    assert [b.try_acquire() for _ in range(3)] == [True, True, False]
    monkeypatch.setenv("POLYGON_MAX_RPM", "0")
    b = polygon_rate.PolygonBudget(clock)
    assert all(b.try_acquire() for _ in range(50))
    monkeypatch.setenv("POLYGON_MAX_RPM", "junk")
    assert polygon_rate.max_rpm() == polygon_rate.DEFAULT_MAX_RPM


def test_a_429_skips_polygon_for_the_rest_of_the_minute(monkeypatch):
    monkeypatch.setenv("POLYGON_MAX_RPM", "0")         # even with no bucket
    clock = Clock(1_000_040.0)
    b = polygon_rate.PolygonBudget(clock)
    assert b.note_rate_limited() is True
    assert b.note_rate_limited() is False               # same cooldown: logged once
    clock.t = 1_000_079.9                               # the next minute starts at 1_000_080
    assert b.try_acquire() is False and b.cooling_down()
    clock.t = 1_000_080.0
    assert b.try_acquire() is True


@pytest.fixture
def polygon_env(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "test-key")
    monkeypatch.delenv("POLYGON_MAX_RPM", raising=False)
    clock = Clock()
    monkeypatch.setattr(polygon_rate, "BUDGET", polygon_rate.PolygonBudget(clock))
    return clock


def test_after_one_429_the_scan_burst_stops_hitting_polygon(monkeypatch, polygon_env, caplog):
    import core.signal_engine as se
    calls = []

    def fake_get(url, headers=None, timeout=None, **kw):
        calls.append(url)
        return Resp(429)

    monkeypatch.setattr("requests.get", fake_get)
    caplog.set_level(logging.INFO)
    syms = [f"S{i}" for i in range(20)]
    assert all(se._try_polygon_ohlcv(s, "1y") is None for s in syms)
    assert len(calls) == 1                               # one 429, then straight to the fallback
    assert "S0: HTTP 429" not in caplog.text             # one cooldown line, not one per symbol
    assert caplog.text.count("skipping Polygon until the next minute") == 1
    polygon_env.t = (int(polygon_env.t // 60) + 1) * 60  # the minute rolls
    se._try_polygon_ohlcv("LATE", "1y")
    assert len(calls) == 2


def test_a_burst_of_successful_calls_stays_within_the_budget(monkeypatch, polygon_env):
    import core.signal_engine as se
    calls = []

    def fake_get(url, headers=None, timeout=None, **kw):
        calls.append(url)
        return Resp(200, polygon=_polygon_ok())

    monkeypatch.setattr("requests.get", fake_get)
    got = [se._try_polygon_ohlcv(f"S{i}", "1y") for i in range(20)]
    assert len(calls) == 5 and sum(1 for g in got if g) == 5


def test_prev_close_polygon_call_shares_the_budget(monkeypatch, polygon_env):
    import core.prices as px
    monkeypatch.setattr(px, "POLYGON_KEY", "test-key")
    calls = []

    def fake_get(url, params=None, timeout=None, **kw):
        calls.append(url)
        return Resp(429)

    monkeypatch.setattr(px.requests, "get", fake_get)
    assert px._polygon_spot("AAA") is None
    assert px._polygon_spot("BBB") is None
    assert len(calls) == 1


def _alpaca_env(monkeypatch):
    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")


def test_alpaca_429_retries_alpaca_before_burning_the_fallbacks(monkeypatch, polygon_env):
    """Before the last attempt an Alpaca 429 goes back to Alpaca (the source that
    works once its minute rolls), not to IEX (same account limit) or Polygon."""
    import core.signal_engine as se
    _alpaca_env(monkeypatch)
    calls = []

    def fake_get(url, headers=None, timeout=None, **kw):
        calls.append(url)
        return Resp(429)

    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr(se, "_try_yfinance_ohlcv", lambda *a, **k: pytest.fail("fallback before last attempt"))
    se._TIER_TRACE.final_attempt = False
    try:
        assert se._fetch_ohlcv_once("HTZ", "stock", period="1mo") is None
    finally:
        se._TIER_TRACE.final_attempt = True
    assert len(calls) == 1 and "feed=sip" in calls[0]


def test_alpaca_429_on_the_last_attempt_skips_iex_and_uses_the_fallbacks(monkeypatch, polygon_env):
    import core.signal_engine as se
    _alpaca_env(monkeypatch)
    calls = []

    def fake_get(url, headers=None, timeout=None, **kw):
        calls.append(url)
        if "api.polygon.io" in url:
            return Resp(200, polygon=_polygon_ok())
        return Resp(429)

    monkeypatch.setattr("requests.get", fake_get)
    rows = se._fetch_ohlcv_once("HTZ", "stock", period="1mo")
    assert rows
    kinds = ["polygon" if "polygon" in c else ("iex" if "feed=iex" in c else "sip") for c in calls]
    assert kinds == ["sip", "polygon"]


def test_full_fetch_gets_alpaca_on_retry_and_never_calls_polygon(monkeypatch, polygon_env):
    import core.signal_engine as se
    _alpaca_env(monkeypatch)
    monkeypatch.setenv("V3_OHLCV_FETCH_RETRIES", "3")
    monkeypatch.setattr(se.time, "sleep", lambda s: None)
    monkeypatch.setattr(se, "_try_yfinance_ohlcv", lambda *a, **k: None)
    monkeypatch.setattr(se, "_try_stooq_ohlcv", lambda *a, **k: None)
    se.clear_ohlcv_cache()
    calls = []
    bars = [{"t": f"2026-09-{d:02d}T04:00:00Z", "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 1e6}
            for d in range(1, 29)]

    def fake_get(url, headers=None, timeout=None, **kw):
        calls.append(url)
        if "api.polygon.io" in url:
            return Resp(200, polygon=_polygon_ok())
        sip = [c for c in calls if "feed=sip" in c]
        return Resp(429) if len(sip) == 1 else Resp(200, bars=bars)

    monkeypatch.setattr("requests.get", fake_get)
    rows = se._fetch_ohlcv("HTZ429", "stock", period="1mo")
    assert rows
    assert not any("polygon" in c or "feed=iex" in c for c in calls)
    assert [c for c in calls if "feed=sip" in c] == calls and len(calls) == 2
