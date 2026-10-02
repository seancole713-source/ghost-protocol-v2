"""Streaming: protocol handling, bar building, and never fighting for the connection."""
from __future__ import annotations

import json

from edge import stream as ST


def frame(*msgs):
    return json.dumps(list(msgs))


def test_connect_triggers_auth_and_auth_is_recorded():
    st = ST.StreamState()
    assert st.handle(frame({"T": "success", "msg": "connected"})) == ["auth"]
    st.handle(frame({"T": "success", "msg": "authenticated"}))
    assert st.authenticated


def test_trades_build_minute_bars_and_a_fresh_price():
    st = ST.StreamState()
    st.handle(frame({"T": "t", "S": "SHOP", "p": 146.0, "s": 100, "t": "2026-09-23T13:31:05Z"},
                    {"T": "t", "S": "SHOP", "p": 147.5, "s": 50, "t": "2026-09-23T13:31:40Z"},
                    {"T": "t", "S": "SHOP", "p": 145.8, "s": 25, "t": "2026-09-23T13:31:59Z"}))
    (minute, bar), = st.minute_bars["SHOP"].items()
    assert bar == [146.0, 147.5, 145.8, 145.8, 175.0]
    now = ST._epoch("2026-09-23T13:32:30Z")
    assert st.price("shop", now=now)["price"] == 145.8
    assert st.price("SHOP", now=now + 600) is None          # stale beyond 2 min


def test_connection_limit_stops_the_stream_instead_of_fighting():
    st = ST.StreamState()
    assert st.handle(frame({"T": "error", "code": 406, "msg": "connection limit exceeded"})) == ["stop"]
    assert st.stopped_reason.startswith("406")


def test_backoff_is_capped():
    s = ST.Streamer(ST.StreamState(), key="k", secret="s")
    assert s.backoff(0) == 1.0 and s.backoff(20) == 256.0 and s.backoff(20) <= 300


def test_off_unless_enabled(monkeypatch):
    monkeypatch.delenv("EDGE_STREAM_ENABLED", raising=False)
    assert ST.maybe_start() is None


def test_garbage_frames_are_ignored():
    st = ST.StreamState()
    assert st.handle("not json") == [] and st.handle(frame({"T": "t", "S": "X"})) == []


def test_a_new_session_drops_the_prior_sessions_bars_and_trades():
    st = ST.StreamState()
    st.handle(frame({"T": "b", "S": "SHOP", "o": 1, "h": 1, "l": 1, "c": 1, "v": 10, "t": "2026-09-23T19:59:00Z"},
                    {"T": "t", "S": "OLD", "p": 5.0, "s": 1, "t": "2026-09-23T19:59:30Z"}))
    assert set(st.minute_bars) == {"SHOP", "OLD"}
    st.handle(frame({"T": "t", "S": "SHOP", "p": 2.0, "s": 1, "t": "2026-09-24T13:31:00Z"}))
    assert set(st.minute_bars) == {"SHOP"} and len(st.minute_bars["SHOP"]) == 1
    assert "OLD" not in st.last_trade
    # a late frame from the prior session is not kept
    st.handle(frame({"T": "b", "S": "OLD", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "t": "2026-09-23T19:58:00Z"}))
    assert "OLD" not in st.minute_bars


def test_minute_bars_are_capped_per_symbol():
    from datetime import datetime, timezone
    st = ST.StreamState()
    base = ST._epoch("2026-09-23T08:00:00Z")     # 04:00 ET
    msgs = [{"T": "b", "S": "SHOP", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1,
             "t": datetime.fromtimestamp(base + 60 * i, tz=timezone.utc).isoformat()}
            for i in range(ST.MAX_BARS_PER_SYMBOL + 25)]
    st.handle(frame(*msgs))
    bars = st.minute_bars["SHOP"]
    assert len(bars) == ST.MAX_BARS_PER_SYMBOL
    assert min(bars) == int(base) + 60 * 25          # the oldest were dropped, the newest kept
