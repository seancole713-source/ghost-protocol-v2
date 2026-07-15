"""Multi-symbol Alpaca bar batching for the squeeze scanner.

Guards the breaker-cascade fix: one paginated multi-symbol request replaces
~2 per-symbol Alpaca calls, and _fetch_volumes reads the prewarmed bars
without touching the network, falling back to the per-symbol path on a miss.
"""

import core.squeeze_monitor as sm


def test_volumes_from_bars_matches_manual_arithmetic():
    daily = [{"v": 100}, {"v": 200}, {"v": 300}]  # 20-day mean over all 3 = 200
    intraday = [
        {"v": 10, "h": 11, "l": 9, "c": 10},   # typical price 10
        {"v": 20, "h": 12, "l": 10, "c": 11},  # typical price 11
    ]
    avg_vol, session_vol, vwap = sm._volumes_from_bars(daily, intraday)
    assert avg_vol == 200.0
    assert session_vol == 30.0
    # vwap = (10*10 + 11*20) / 30 = 320/30
    assert abs(vwap - 10.6667) < 1e-3


def test_volumes_from_bars_handles_empty():
    assert sm._volumes_from_bars([], []) == (None, None, None)


def test_fetch_volumes_uses_batch_store_without_network(monkeypatch):
    # If _fetch_volumes touches the network when a batch entry exists, fail loud.
    def _boom():
        raise AssertionError("_fetch_volumes hit the network despite a batch hit")

    monkeypatch.setattr(sm, "_alpaca_headers", _boom)
    monkeypatch.setattr(
        sm, "_batch_bars",
        {"AAPL": {"daily": [{"v": 100}, {"v": 300}], "intraday": [{"v": 40, "h": 10, "l": 8, "c": 9}]}},
    )
    avg_vol, session_vol, vwap = sm._fetch_volumes("aapl")
    assert avg_vol == 200.0
    assert session_vol == 40.0
    assert vwap is not None


def test_fetch_volumes_batch_miss_falls_through(monkeypatch):
    # Symbol absent from the batch → per-symbol path runs (here: no headers → None).
    monkeypatch.setattr(sm, "_batch_bars", {"MSFT": {"daily": [{"v": 1}], "intraday": []}})
    monkeypatch.setattr(sm, "_alpaca_headers", lambda: None)
    monkeypatch.setattr(sm, "_yf_fallback_enabled", lambda: False)
    assert sm._fetch_volumes("AAPL") == (None, None, None)


def test_fetch_volumes_batch_present_but_no_volume_falls_through(monkeypatch):
    # Batch had the symbol but zero usable daily volume → per-symbol fallback.
    calls = {"n": 0}

    def _no_headers():
        calls["n"] += 1
        return None

    monkeypatch.setattr(sm, "_batch_bars", {"AAPL": {"daily": [], "intraday": []}})
    monkeypatch.setattr(sm, "_alpaca_headers", _no_headers)
    monkeypatch.setattr(sm, "_yf_fallback_enabled", lambda: False)
    assert sm._fetch_volumes("AAPL") == (None, None, None)
    assert calls["n"] == 1  # fell through to the per-symbol path


class _FakeResp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


def test_alpaca_multi_bars_paginates_and_groups(monkeypatch):
    import core.prices as prices
    import requests

    monkeypatch.setattr(sm, "_alpaca_headers", lambda: {"APCA-API-KEY-ID": "k", "APCA-API-SECRET-KEY": "s"})
    monkeypatch.setattr(prices, "_alpaca_bar_feeds", lambda: ("iex",))
    monkeypatch.setattr(prices, "_note_alpaca_feed_status", lambda *a, **k: None)

    pages = [
        {"bars": {"AAPL": [{"v": 1}], "MSFT": [{"v": 2}]}, "next_page_token": "TOK"},
        {"bars": {"AAPL": [{"v": 3}]}, "next_page_token": None},
    ]

    def _fake_get(url, headers=None, timeout=None):
        return _FakeResp(pages[1] if "page_token=TOK" in url else pages[0])

    monkeypatch.setattr(requests, "get", _fake_get)
    out = sm._alpaca_multi_bars(["AAPL", "MSFT"], timeframe="1Day", start="s", end="e")
    assert out["AAPL"] == [{"v": 1}, {"v": 3}]  # pages concatenated
    assert out["MSFT"] == [{"v": 2}]


def test_alpaca_multi_bars_empty_on_non_200(monkeypatch):
    import core.prices as prices
    import requests

    monkeypatch.setattr(sm, "_alpaca_headers", lambda: {"APCA-API-KEY-ID": "k", "APCA-API-SECRET-KEY": "s"})
    monkeypatch.setattr(prices, "_alpaca_bar_feeds", lambda: ("iex",))
    monkeypatch.setattr(prices, "_note_alpaca_feed_status", lambda *a, **k: None)

    class _Err:
        status_code = 429

        def json(self):
            return {}

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Err())
    assert sm._alpaca_multi_bars(["AAPL"], timeframe="1Day", start="s", end="e") == {}
