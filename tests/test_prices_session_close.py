"""Main price path (core.prices): previous close by SESSION, and the phantom
guard outside RTH. Both feed ghost_symbol_quote and the morning $1,000 card.

F01 -- 2026-09-25 03:19 CDT: GLND showed previous_close 2.90, the 9/23 close;
the 9/24 close was ~4, so its premarket gap read ~38% too high. The prev-close
cache kept (write_time, close) for 24h, and Alpaca's snapshot prevDailyBar is
two sessions back in early premarket when no bar exists for today yet.

F02 -- the 50% phantom guard compared a premarket print with the prior regular
close, rejected real gaps (PFSA +74.6%, APUS +169% on 9/24), fell back to the
stale close, and still labelled it "current_session" with a 0% gap.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from core import prices

CT = ZoneInfo("America/Chicago")
# Fri 2026-09-25 03:19 CT: premarket; the previous session is Thu 9/24.
FRI_PREMARKET = dt.datetime(2026, 9, 25, 3, 19, tzinfo=CT)


def _bar(day: str, close: float) -> dict:
    # Alpaca daily bars are stamped at midnight America/New_York, in UTC.
    return {"t": f"{day}T04:00:00Z", "o": close, "h": close, "l": close, "c": close}


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


@pytest.fixture
def alpaca(monkeypatch):
    """Route Alpaca HTTP to canned payloads; record every request."""
    calls: list = []
    routes: dict = {}

    def _get(url, headers=None, params=None, timeout=None):
        calls.append({"url": url, "params": dict(params or {})})
        for suffix, payload in routes.items():
            if url.endswith(suffix):
                return _Resp(payload)
        return _Resp({}, status=404)

    monkeypatch.setenv("ALPACA_KEY_ID", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    monkeypatch.setattr(prices._alpaca_cb, "allow", lambda: True)
    monkeypatch.setattr(prices._alpaca_cb, "record_success", lambda: None)
    monkeypatch.setattr(prices._alpaca_cb, "record_failure", lambda: None)
    monkeypatch.setattr(prices, "_alpaca_bar_feeds", lambda: ("iex",))
    monkeypatch.setattr(prices.requests, "get", _get)
    return routes, calls


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(prices, "_prev_close_cache", {})
    monkeypatch.setattr(prices, "_save_prev_close_cache", lambda: None)
    monkeypatch.setattr(prices, "_cross_check_cache", {})
    monkeypatch.setattr(prices, "_cross_check_calls", [])
    monkeypatch.setattr(prices, "PRICE_SANITY_FAIL_CLOSED", False)
    monkeypatch.setattr(prices, "PRICE_SANITY_DIVERGENCE_PCT", 50.0)
    monkeypatch.setattr(prices, "PRICE_SANITY_EXTENDED_MAX_RATIO", 8.0)


# ------------------------------------------------------------ F01: session --

@pytest.mark.parametrize("now, expected", [
    (FRI_PREMARKET, dt.date(2026, 9, 24)),
    (dt.datetime(2026, 9, 24, 10, 0, tzinfo=CT), dt.date(2026, 9, 23)),  # RTH
    (dt.datetime(2026, 9, 28, 6, 0, tzinfo=CT), dt.date(2026, 9, 25)),   # Monday -> Friday
    (dt.datetime(2026, 9, 8, 6, 0, tzinfo=CT), dt.date(2026, 9, 4)),     # after Labor Day
    (dt.datetime(2026, 9, 26, 12, 0, tzinfo=CT), dt.date(2026, 9, 24)),  # Saturday: Fri's prev
])
def test_expected_previous_session_uses_the_market_calendar(now, expected):
    assert prices._expected_prev_session(now) == expected


def _breaker_open(monkeypatch, *, alpaca_close=None):
    from core.circuit_breaker import _yfinance_cb
    monkeypatch.setattr(prices, "_now_ct", lambda: FRI_PREMARKET)
    monkeypatch.setattr(prices, "_alpaca_trade_quote", lambda _s: (4.40, int(FRI_PREMARKET.timestamp()) - 60))
    monkeypatch.setattr(prices, "_reject_phantom", lambda _s, p: (p, False))
    monkeypatch.setattr(_yfinance_cb, "allow", lambda: False)
    asked = []
    monkeypatch.setattr(prices, "_alpaca_prev_close", lambda s: asked.append(s) or alpaca_close)
    return asked


def test_premarket_rejects_the_d_minus_1_cache_entry(monkeypatch):
    """The 9/23 close cached on the 9/24 morning is not Friday's previous close."""
    asked = _breaker_open(monkeypatch, alpaca_close=4.0)
    prices._prev_close_cache["GLND"] = ("2026-09-23", 2.90)
    out = prices.get_extended_session("GLND")
    assert asked == ["GLND"]  # the stale entry was not accepted
    assert out["previous_close"] == 4.0
    assert out["previous_close_session"] == "2026-09-24"
    assert out["gap_pct"] == 10.0
    # ...and the fresh, dated close replaced it.
    assert prices._prev_close_cache["GLND"] == ("2026-09-24", 4.0)


def test_premarket_stale_cache_without_alpaca_gives_no_gap(monkeypatch):
    _breaker_open(monkeypatch, alpaca_close=None)
    prices._prev_close_cache["GLND"] = ("2026-09-23", 2.90)
    out = prices.get_extended_session("GLND")
    assert out["previous_close"] is None and out["gap_pct"] is None


def test_legacy_write_time_entry_is_never_accepted(monkeypatch):
    _breaker_open(monkeypatch, alpaca_close=None)
    prices._prev_close_cache["GLND"] = (prices.time.time() - 60, 2.90)
    out = prices.get_extended_session("GLND")
    assert out["previous_close"] is None


def test_the_previous_sessions_cache_entry_is_used(monkeypatch):
    asked = _breaker_open(monkeypatch, alpaca_close=None)
    prices._prev_close_cache["GLND"] = ("2026-09-24", 4.0)
    out = prices.get_extended_session("GLND")
    assert asked == []
    assert out["previous_close"] == 4.0 and out["gap_pct"] == 10.0


def test_lagging_snapshot_uses_dailybar_close(alpaca):
    """Early premarket: no bar for today yet, so dailyBar IS the 9/24 session."""
    routes, _calls = alpaca
    routes["/GLND/snapshot"] = {
        "dailyBar": _bar("2026-09-24", 4.0),
        "prevDailyBar": _bar("2026-09-23", 2.90),
    }
    assert prices._alpaca_prev_close("GLND", dt.date(2026, 9, 24)) == 4.0


def test_current_snapshot_uses_prevdailybar_close(alpaca):
    routes, _calls = alpaca
    routes["/GLND/snapshot"] = {
        "dailyBar": _bar("2026-09-25", 4.60),  # today's bar already exists
        "prevDailyBar": _bar("2026-09-24", 4.0),
    }
    assert prices._alpaca_prev_close("GLND", dt.date(2026, 9, 24)) == 4.0


def test_snapshot_without_the_session_falls_back_to_dated_daily_bars(alpaca):
    routes, calls = alpaca
    routes["/GLND/snapshot"] = {
        "dailyBar": _bar("2026-09-23", 2.90),
        "prevDailyBar": _bar("2026-09-22", 2.70),
    }
    routes["/GLND/bars"] = {"bars": [_bar("2026-09-24", 4.0), _bar("2026-09-23", 2.90)]}
    assert prices._alpaca_prev_close("GLND", dt.date(2026, 9, 24)) == 4.0
    assert calls[-1]["params"]["timeframe"] == "1Day"


def test_no_bar_for_the_session_means_no_previous_close(alpaca):
    routes, _calls = alpaca
    routes["/GLND/snapshot"] = {"dailyBar": _bar("2026-09-23", 2.90), "prevDailyBar": _bar("2026-09-22", 2.70)}
    routes["/GLND/bars"] = {"bars": [_bar("2026-09-23", 2.90), _bar("2026-09-22", 2.70)]}
    assert prices._alpaca_prev_close("GLND", dt.date(2026, 9, 24)) is None


def test_u17_daily_bars_request_newest_first_and_match_by_date(alpaca):
    """limit=5 ascending over a 10-day window returned the OLDEST bars."""
    routes, calls = alpaca
    routes["/GLND/bars"] = {"bars": [
        _bar("2026-09-24", 4.0), _bar("2026-09-23", 2.90), _bar("2026-09-22", 2.70),
    ]}
    assert prices._alpaca_daily_close("GLND", dt.date(2026, 9, 24), {"h": "x"}) == 4.0
    params = calls[-1]["params"]
    assert params["sort"] == "desc"
    assert params["start"].startswith("2026-09-17")
    # Order does not matter: the bar is chosen by its session date.
    routes["/GLND/bars"] = {"bars": list(reversed(routes["/GLND/bars"]["bars"]))}
    assert prices._alpaca_daily_close("GLND", dt.date(2026, 9, 23), {"h": "x"}) == 2.90


def test_squeeze_monitor_prev_close_is_dated_too(alpaca, monkeypatch):
    from core import squeeze_monitor as sm
    routes, _calls = alpaca
    monkeypatch.setattr(prices, "_now_ct", lambda: FRI_PREMARKET)
    routes["/GLND/bars"] = {"bars": [_bar("2026-09-23", 2.90), _bar("2026-09-22", 2.70)]}
    assert sm._alpaca_prev_close("GLND") is None  # 9/24 is missing: no stale substitute
    routes["/GLND/bars"] = {"bars": [_bar("2026-09-24", 4.0), _bar("2026-09-23", 2.90)]}
    assert sm._alpaca_prev_close("GLND") == 4.0


def test_persisted_legacy_entries_are_dropped_on_load(monkeypatch):
    import json
    import contextlib

    payload = json.dumps({"OLD": [1790000000.0, 2.9], "NEW": ["2026-09-24", 4.0], "BAD": "x"})

    class _Cur:
        def execute(self, *_a):
            pass

        def fetchone(self):
            return (payload,)

    class _Conn:
        def cursor(self):
            return _Cur()

    import core.db
    monkeypatch.setattr(core.db, "db_conn", lambda: contextlib.nullcontext(_Conn()))
    prices._load_prev_close_cache()
    assert prices._prev_close_cache == {"NEW": ("2026-09-24", 4.0)}


# ----------------------------------------------------- F02: phantom guard --

def _yf(monkeypatch, *, last=5.0, prev=5.0, pre_market=None):
    import yfinance as yf
    from core.circuit_breaker import _yfinance_cb

    class _FastInfo:
        last_price = last
        previous_close = prev
        pre_market_price = pre_market
        post_market_price = None

    class _Ticker:
        fast_info = _FastInfo()

    monkeypatch.setattr(_yfinance_cb, "allow", lambda: True)
    monkeypatch.setattr(_yfinance_cb, "record_success", lambda: None)
    monkeypatch.setattr(yf, "Ticker", lambda _s: _Ticker())


def _at(monkeypatch, now, print_price):
    monkeypatch.setattr(prices, "_now_ct", lambda: now)
    ts = int(now.timestamp()) - 60
    monkeypatch.setattr(prices, "_alpaca_trade_quote", lambda _s: (print_price, ts))
    return ts


def test_premarket_60pct_gap_is_not_a_phantom(monkeypatch):
    """The reproduction: print 8.00 (60 s old) vs fast_info 5.00 at 07:00 CT."""
    _yf(monkeypatch)
    ts = _at(monkeypatch, dt.datetime(2026, 9, 25, 7, 0, tzinfo=CT), 8.0)
    out = prices.get_extended_session("PFSA")
    assert out["session"] == "premarket"
    assert out["live_price"] == 8.0 and out["session_price"] == 8.0
    assert out["gap_pct"] == 60.0
    assert out["session_price_basis"] == "current_session"
    assert out["price_as_of_ts"] == ts
    assert out["phantom_rejected"] is False


def test_premarket_real_169pct_gap_survives(monkeypatch):
    _yf(monkeypatch, last=2.0, prev=2.0)
    _at(monkeypatch, dt.datetime(2026, 9, 25, 7, 0, tzinfo=CT), 5.38)
    out = prices.get_extended_session("APUS")
    assert out["gap_pct"] == 169.0 and out["session_price_basis"] == "current_session"


def test_premarket_10x_phantom_is_still_rejected(monkeypatch):
    monkeypatch.setattr(prices, "_now_ct", lambda: dt.datetime(2026, 9, 25, 7, 0, tzinfo=CT))
    prices._cross_check_cache["MU"] = (prices.time.time(), 100.0, False)
    assert prices._reject_phantom("MU", 993.42) == (None, True)
    assert prices._reject_phantom("MU", 9.9) == (None, True)
    assert prices._reject_phantom("MU", 160.0) == (160.0, False)


def test_rth_phantom_is_still_rejected_and_never_current_session(monkeypatch):
    """In RTH the tight band stands. The rejected print's timestamp is dropped
    and the untimed fallback (the stale close) carries no gap."""
    _yf(monkeypatch)
    _at(monkeypatch, dt.datetime(2026, 9, 25, 10, 0, tzinfo=CT), 8.0)
    monkeypatch.setattr(prices, "get_stock_price", lambda _s: 5.0)
    out = prices.get_extended_session("MU")
    assert out["session"] == "rth"
    assert out["phantom_rejected"] is True
    assert out["session_price"] == 5.0
    assert out["price_as_of_ts"] is None and out["price_source"] is None
    assert out["session_price_basis"] == "phantom_rejected"
    assert out["gap_pct"] is None and out["gap_abs"] is None


def test_premarket_reference_does_not_reject_the_open(monkeypatch):
    """A reference taken in premarket is the prior close; at 08:31 CT a +150%
    gapper's first RTH print must not be rejected against it."""
    monkeypatch.setattr(prices, "_now_ct", lambda: dt.datetime(2026, 9, 25, 8, 31, tzinfo=CT))
    prices._cross_check_cache["APUS"] = (prices.time.time(), 2.0, False)
    assert prices._reject_phantom("APUS", 5.0) == (5.0, False)
    # A reference taken in RTH keeps the tight band.
    prices._cross_check_cache["APUS"] = (prices.time.time(), 2.0, True)
    assert prices._reject_phantom("APUS", 5.0) == (None, True)


def test_cross_check_records_the_session_it_was_taken_in(monkeypatch):
    _yf(monkeypatch)
    monkeypatch.setattr(prices, "_now_ct", lambda: dt.datetime(2026, 9, 25, 7, 0, tzinfo=CT))
    assert prices._reject_phantom("PFSA", 8.0) == (8.0, False)
    assert prices._cross_check_cache["PFSA"][1:] == (5.0, False)
