"""Dead-symbol guard on the old engine's OHLCV chain (core.signal_engine).

Production 2026-09-25: GPS (Gap Inc. -> GAP, 2024) and SATS (EchoStar -> ECHO,
2026-06-24) were off the watchlist config, yet open rows issued while they were
on it (research predictions, hunter evaluations, super-ghost ledger, picks) kept
every resolver walking SIP -> IEX -> Polygon -> yfinance -> Stooq x3 retries for
them all day: 41 Polygon 429s and ~48 "Stooq parsed 0 rows" lines.
"""
from __future__ import annotations

import logging

import pytest

import core.signal_engine as se


@pytest.fixture(autouse=True)
def _fresh_guard(monkeypatch):
    se.reset_no_data_guard()
    se.clear_ohlcv_cache()
    monkeypatch.setenv("V3_OHLCV_FETCH_RETRIES", "1")
    monkeypatch.setenv("V3_OHLCV_NEG_CACHE_TTL_S", "0")
    monkeypatch.delenv("V3_OHLCV_NO_DATA_SKIP_AFTER", raising=False)
    yield
    se.reset_no_data_guard()
    se.clear_ohlcv_cache()


def _bars(n=3):
    return [
        {"ts": f"2026-09-{10 + i:02d}T00:00:00Z", "open": 10.0, "high": 11.0,
         "low": 9.0, "close": 10.5, "volume": 1000.0}
        for i in range(n)
    ]


class _Chain:
    """Stand-in for _fetch_ohlcv_once that reports tier answers like the real one."""

    def __init__(self, notes, rows=None):
        self.notes = notes
        self.rows = rows
        self.calls = []

    def __call__(self, symbol, asset_type, period='1y', interval='1d', **_kw):
        self.calls.append((symbol, period, interval))
        for tier, outcome in self.notes:
            se._note_tier(tier, outcome)
        return self.rows


def test_retired_symbols_never_reach_a_provider(monkeypatch, caplog):
    chain = _Chain([])
    monkeypatch.setattr(se, "_fetch_ohlcv_once", chain)
    with caplog.at_level(logging.WARNING, logger="ghost.signal_v3"):
        for _ in range(3):
            assert se._fetch_ohlcv("GPS", "stock", period="3m") is None
            assert se._fetch_ohlcv("sats", "stock", period="1y") is None
    assert chain.calls == []
    retired_logs = [r for r in caplog.records if "retired ticker" in r.getMessage()]
    assert len(retired_logs) == 2  # once per symbol, not once per call
    assert "ECHO" in " ".join(r.getMessage() for r in retired_logs)


def test_symbol_with_no_data_anywhere_is_skipped_for_the_day_and_logged_once(monkeypatch, caplog):
    chain = _Chain([("alpaca", "empty"), ("polygon", "error"), ("stooq", "empty")])
    monkeypatch.setattr(se, "_fetch_ohlcv_once", chain)
    monkeypatch.setattr(se, "_session_day", lambda now=None: "2026-09-25")
    with caplog.at_level(logging.WARNING, logger="ghost.signal_v3"):
        for _ in range(6):
            assert se._fetch_ohlcv("DEADCO", "stock", period="3m") is None
    assert len(chain.calls) == 3  # N=3 misses, then skipped
    skip_logs = [r for r in caplog.records if "no bars from any source" in r.getMessage()]
    assert len(skip_logs) == 1
    assert "DEADCO" in skip_logs[0].getMessage()


def test_skip_expires_next_session_day_with_one_probe(monkeypatch):
    chain = _Chain([("alpaca", "empty"), ("stooq", "empty")])
    monkeypatch.setattr(se, "_fetch_ohlcv_once", chain)
    day = {"d": "2026-09-25"}
    monkeypatch.setattr(se, "_session_day", lambda now=None: day["d"])
    for _ in range(5):
        se._fetch_ohlcv("DEADCO", "stock", period="3m")
    assert len(chain.calls) == 3
    day["d"] = "2026-09-28"
    for _ in range(3):
        se._fetch_ohlcv("DEADCO", "stock", period="3m")
    assert len(chain.calls) == 4  # one probe, still dead -> skipped again


def test_outages_and_rate_limits_never_count_toward_the_skip(monkeypatch):
    """Every tier failing (429s, open breakers, timeouts) says nothing about
    the symbol -- a valid ticker during an outage must keep being retried."""
    chain = _Chain([("alpaca", "error"), ("polygon", "error"), ("stooq", "error")])
    monkeypatch.setattr(se, "_fetch_ohlcv_once", chain)
    for _ in range(6):
        assert se._fetch_ohlcv("AAPL", "stock", period="3m") is None
    assert len(chain.calls) == 6


def test_one_definitive_source_is_not_enough(monkeypatch):
    chain = _Chain([("alpaca", "empty"), ("polygon", "error")])
    monkeypatch.setattr(se, "_fetch_ohlcv_once", chain)
    for _ in range(6):
        se._fetch_ohlcv("WOLF", "stock", period="3m")
    assert len(chain.calls) == 6


def test_success_resets_the_streak_and_valid_rows_are_unchanged(monkeypatch):
    dead = _Chain([("alpaca", "empty"), ("stooq", "empty")])
    live = _Chain([], rows=_bars())
    monkeypatch.setattr(se, "_fetch_ohlcv_once", dead)
    se._fetch_ohlcv("FLAKY", "stock", period="3m")
    se._fetch_ohlcv("FLAKY", "stock", period="3m")
    monkeypatch.setattr(se, "_fetch_ohlcv_once", live)
    rows = se._fetch_ohlcv("FLAKY", "stock", period="3m")
    assert rows and len(rows) == 3
    se.clear_ohlcv_cache()
    monkeypatch.setattr(se, "_fetch_ohlcv_once", dead)
    se._fetch_ohlcv("FLAKY", "stock", period="3m")
    se._fetch_ohlcv("FLAKY", "stock", period="3m")
    assert len(dead.calls) == 4  # streak restarted at 0 after the success: not skipped


def test_skip_is_scoped_to_the_request_that_kept_failing(monkeypatch):
    """A renamed ticker can still have stale long-window history; the skip is
    per (symbol, period, interval), so a request that does return rows is
    untouched."""
    def once(symbol, asset_type, period='1y', interval='1d', **_kw):
        if period == "3m":
            se._note_tier("alpaca", "empty")
            se._note_tier("stooq", "empty")
            return None
        return _bars()

    monkeypatch.setattr(se, "_fetch_ohlcv_once", once)
    for _ in range(4):
        se._fetch_ohlcv("OLDCO", "stock", period="3m")
    assert se._ohlcv_fetch_blocked("OLDCO", ("OLDCO", "3m", "1d")) is True
    assert se._fetch_ohlcv("OLDCO", "stock", period="1y")


def test_guard_can_be_disabled(monkeypatch):
    monkeypatch.setenv("V3_OHLCV_NO_DATA_SKIP_AFTER", "0")
    chain = _Chain([("alpaca", "empty"), ("stooq", "empty")])
    monkeypatch.setattr(se, "_fetch_ohlcv_once", chain)
    for _ in range(5):
        se._fetch_ohlcv("DEADCO", "stock", period="3m")
    assert len(chain.calls) == 5


class _Resp:
    def __init__(self, status, *, json_body=None, text=""):
        self.status_code = status
        self._json = json_body
        self.text = text

    def json(self):
        return self._json


def test_real_tier_chain_reports_definitive_empties(monkeypatch):
    """Wiring check through the real tier helpers: Alpaca 200 + bars:null,
    Polygon 429, Stooq CSV whose rows all predate the window (the exact GPS
    signature) -> skipped after three misses, with zero further HTTP calls."""
    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("POLYGON_API_KEY", "p")
    monkeypatch.setattr(se, "_try_yfinance_ohlcv", lambda symbol, period: None)
    monkeypatch.setattr(se, "_session_day", lambda now=None: "2026-09-25")
    urls = []

    def fake_get(url, *args, **kwargs):
        urls.append(url)
        if "alpaca" in url:
            return _Resp(200, json_body={"bars": None})
        if "polygon" in url:
            return _Resp(429, text="exceeded the maximum requests per minute")
        if "stooq" in url:
            return _Resp(200, text="Date,Open,High,Low,Close,Volume\n2024-08-20,20,21,19,20.5,1000\n")
        raise AssertionError(url)

    monkeypatch.setattr("requests.get", fake_get)
    for _ in range(3):
        assert se._fetch_ohlcv("DEADCO", "stock", period="3m") is None
    calls_after_streak = len(urls)
    assert calls_after_streak > 0
    assert se._fetch_ohlcv("DEADCO", "stock", period="3m") is None
    assert len(urls) == calls_after_streak
