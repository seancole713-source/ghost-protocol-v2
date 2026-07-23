"""Earnings guard: intraday lane must not hold through a report (sniper PR).

Fail-open is deliberate — the guard is a seatbelt, not an interlock — but
ignorance (fetch failed) must be distinguishable from safety (no earnings).
"""
import core.earnings_guard as eg


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


def _clear_cache():
    eg._cache.clear()


def test_upcoming_earnings_parses_and_uppercases(monkeypatch):
    _clear_cache()
    monkeypatch.setenv("FINNHUB_API_KEY", "x" * 10)
    import requests
    monkeypatch.setattr(
        requests, "get",
        lambda *a, **k: _Resp(200, {"earningsCalendar": [
            {"symbol": "tsla"}, {"symbol": "LMT"}, {"symbol": ""}]}),
    )
    syms, ok = eg.upcoming_earnings_symbols()
    assert ok is True
    assert syms == {"TSLA", "LMT"}


def test_upcoming_earnings_fail_open_on_http_error(monkeypatch):
    _clear_cache()
    monkeypatch.setenv("FINNHUB_API_KEY", "x" * 10)
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(500))
    syms, ok = eg.upcoming_earnings_symbols()
    assert syms == set()
    assert ok is False  # empty-from-failure must NOT look like empty-from-safety


def test_upcoming_earnings_blind_without_key(monkeypatch):
    _clear_cache()
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    syms, ok = eg.upcoming_earnings_symbols()
    assert syms == set()
    assert ok is False


def test_upcoming_earnings_uses_cache(monkeypatch):
    _clear_cache()
    monkeypatch.setenv("FINNHUB_API_KEY", "x" * 10)
    calls = {"n": 0}
    import requests

    def _get(*a, **k):
        calls["n"] += 1
        return _Resp(200, {"earningsCalendar": [{"symbol": "AMC"}]})

    monkeypatch.setattr(requests, "get", _get)
    s1, _ = eg.upcoming_earnings_symbols()
    s2, _ = eg.upcoming_earnings_symbols()
    assert s1 == s2 == {"AMC"}
    assert calls["n"] == 1


def test_guard_enabled_default_on(monkeypatch):
    monkeypatch.delenv("PAPER_INTRADAY_EARNINGS_GUARD", raising=False)
    assert eg.earnings_guard_enabled() is True
    monkeypatch.setenv("PAPER_INTRADAY_EARNINGS_GUARD", "0")
    assert eg.earnings_guard_enabled() is False
