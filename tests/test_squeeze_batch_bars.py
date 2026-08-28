"""Multi-symbol Alpaca bar batching for the squeeze scanner.

Guards the breaker-cascade fix: one paginated multi-symbol request replaces
~2 per-symbol Alpaca calls, and _fetch_volumes reads the prewarmed bars
without touching the network, falling back to the per-symbol path on a miss.
"""

import asyncio
import time

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


def test_metrics_from_batch_bars_avoids_per_symbol_quote_path(monkeypatch):
    monkeypatch.setattr(
        sm,
        "_batch_bars",
        {
            "AAPL": {
                "daily": [
                    {"o": 98, "c": 100, "v": 1000},
                    {"o": 100, "c": 102, "v": 1200},
                ],
                "intraday": [
                    {"c": 104, "h": 105, "l": 103, "v": 100},
                    {"c": 106, "h": 107, "l": 105, "v": 200},
                ],
            }
        },
    )

    metrics = sm._metrics_from_batch_bars("aapl")

    assert metrics is not None
    assert metrics["price"] == 106
    assert metrics["prior_close"] == 100
    assert metrics["session_high"] == 107
    assert metrics["session_volume"] == 300
    assert metrics["current_move_pct"] == 6


def test_batch_preserves_symbol_with_no_premarket_print(monkeypatch):
    def fake_fetch(_symbols):
        sm._batch_bars.update({
            "AAPL": {
                "daily": [{"o": 98, "c": 100, "v": 1000}],
                "intraday": [],
            },
            "MSFT": {
                "daily": [{"o": 198, "c": 200, "v": 2000}],
                "intraday": [{"c": 201, "h": 202, "l": 200, "v": 100}],
            },
        })

    monkeypatch.setattr(sm, "_batch_fetch_bars", fake_fetch)

    metrics = sm.batched_market_metrics(["AAPL", "MSFT", "MISSING"])

    assert "AAPL" in metrics
    assert metrics["AAPL"] is None
    assert metrics["MSFT"] is not None
    assert "MISSING" not in metrics
    assert sm._batch_bars == {}


def test_cached_short_context_never_fetches(monkeypatch):
    monkeypatch.setattr(sm, "_short_cache", {})
    monkeypatch.setattr(
        sm,
        "_short_context",
        lambda _symbol: (_ for _ in ()).throw(AssertionError("network path called")),
    )

    assert sm._cached_short_context("AAPL")["squeeze_risk"] is None


def test_empty_short_cache_entry_expires_quickly(monkeypatch):
    monkeypatch.setattr(sm, "_SHORT_FAILURE_CACHE_TTL", 60)
    monkeypatch.setattr(
        sm,
        "_short_cache",
        {
            "AAPL": (
                time.time() - 61,
                {
                    "short_float_pct": None,
                    "days_to_cover": None,
                    "squeeze_risk": None,
                },
            )
        },
    )

    assert sm._cached_short_context("AAPL")["short_float_pct"] is None
    assert sm._short_cache_ttl(sm._short_cache["AAPL"][1]) == 60


def test_expired_empty_short_cache_entry_is_refetched(monkeypatch):
    monkeypatch.setattr(sm, "_SHORT_FAILURE_CACHE_TTL", 60)
    monkeypatch.setattr(
        sm,
        "_short_cache",
        {
            "AAPL": (
                time.time() - 61,
                {"short_float_pct": None, "days_to_cover": None},
            )
        },
    )
    monkeypatch.setattr(sm, "_yf_short_enabled", lambda: False)
    monkeypatch.setattr(
        sm,
        "_short_context_from_finviz",
        lambda _symbol: {
            "short_float_pct": 12.5,
            "days_to_cover": 2.0,
            "squeeze_risk": "medium",
        },
    )

    refreshed = sm._short_context("AAPL")

    assert refreshed["short_float_pct"] == 12.5
    assert sm._short_cache["AAPL"][1]["days_to_cover"] == 2.0


def test_useful_short_cache_entry_keeps_daily_ttl():
    assert sm._short_cache_ttl({"short_float_pct": 12.5}) == sm._SHORT_CACHE_TTL


def test_short_cache_maintenance_survives_closed_market_start(monkeypatch):
    import core.market_hours as market_hours

    market_states = iter((False, True))
    warmed = []
    sleeps = []

    monkeypatch.setattr(
        market_hours,
        "is_us_extended_hours",
        lambda: next(market_states),
    )
    monkeypatch.setattr(
        sm,
        "prewarm_short_cache",
        lambda: _record_async(warmed, "ran"),
    )

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def run():
        try:
            await sm.maintain_short_cache()
        except asyncio.CancelledError:
            pass

    asyncio.run(run())

    assert warmed == ["ran"]
    assert sleeps == [300, sm._SHORT_PREWARM_REFRESH_S]


async def _record_async(items, value):
    items.append(value)


def test_short_cache_prewarm_skips_a_stuck_symbol(monkeypatch):
    import config.symbols
    import core.market_hours as market_hours

    completed = []

    def short_context(symbol):
        if symbol == "A":
            time.sleep(3.2)
        completed.append(symbol)
        return {}

    monkeypatch.setattr(config.symbols, "get_edge_set", lambda: {"A", "B"})
    monkeypatch.setattr(market_hours, "is_us_extended_hours", lambda: True)
    monkeypatch.setattr(sm, "_short_context", short_context)
    monkeypatch.setenv("SQUEEZE_SHORT_PREWARM_DELAY_S", "0")
    monkeypatch.setenv("SQUEEZE_SHORT_PREWARM_TIMEOUT_S", "3")

    asyncio.run(sm.prewarm_short_cache())

    assert "B" in completed


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


def test_missing_session_volume_is_not_fabricated(monkeypatch):
    """Forensic MD-3/SQ-4: a missing session-volume read must become RVOL 0,
    never a fabricated avg_vol*0.4 spike."""
    monkeypatch.setattr(
        sm, "_batch_bars",
        {"AAPL": {"daily": [{"v": 100}, {"v": 300}], "intraday": []}},
    )
    avg_vol, session_vol, vwap = sm._fetch_volumes("aapl")
    assert avg_vol == 200.0
    assert session_vol == 0.0  # not 80.0 (the old avg_vol*0.4 fabrication)


def test_metrics_from_batch_bars_zero_volume_not_fabricated(monkeypatch):
    monkeypatch.setattr(
        sm,
        "_batch_bars",
        {
            "AAPL": {
                "daily": [{"o": 98, "c": 100, "v": 1000}, {"o": 100, "c": 102, "v": 1200}],
                "intraday": [{"c": 104, "h": 105, "l": 103, "v": 0}],
            }
        },
    )
    metrics = sm._metrics_from_batch_bars("aapl")
    assert metrics is not None
    assert metrics["session_volume"] == 0.0  # not avg_vol*0.4


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
