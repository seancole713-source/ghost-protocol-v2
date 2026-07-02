"""RTH intraday OHLC aggregation from Alpaca-style bars."""
from core.market_hours import RTH_CLOSE_MIN, RTH_OPEN_MIN, SESSION_TZ
from core.prices import _ohlc_from_bars


def _bar(hour, minute, o, h, low, c):
    return {"t": f"2026-06-08T{hour:02d}:{minute:02d}:00-04:00", "o": o, "h": h, "l": low, "c": c}


def test_ohlc_from_bars_rth_window():
    from zoneinfo import ZoneInfo

    ct = ZoneInfo(SESSION_TZ)
    bars = [
        _bar(4, 0, 4.20, 4.25, 4.18, 4.22),   # 3:00 AM CT premarket — excluded from RTH
        _bar(9, 30, 4.55, 4.56, 4.50, 4.54),  # 8:30 AM CT RTH open
        _bar(10, 0, 4.54, 4.56, 4.13, 4.40),
        _bar(15, 55, 4.40, 4.42, 4.38, 4.39),
    ]
    ext_o, ext_h, ext_l = _ohlc_from_bars(bars, ct)
    assert ext_o == 4.20
    assert ext_h == 4.56
    assert ext_l == 4.13

    rth_o, rth_h, rth_l = _ohlc_from_bars(
        bars, ct, start_min=RTH_OPEN_MIN, end_min=RTH_CLOSE_MIN,
    )
    assert rth_o == 4.55
    assert rth_h == 4.56
    assert rth_l == 4.13


def test_intraday_cache_skips_when_ohlc_missing(monkeypatch):
    import time as _time
    from core import prices as px

    px._intraday_cache.clear()
    px._intraday_cache["WOLF"] = (
        _time.time(),
        {"symbol": "WOLF", "price": 43.46, "today_open": None, "today_high": None, "today_low": None},
    )
    calls = {"n": 0}

    def _fake_full(sym):
        calls["n"] += 1
        return {
            "symbol": sym,
            "price": 43.46,
            "today_open": 47.10,
            "today_high": 48.40,
            "today_low": 43.04,
            "previous_close": 44.0,
            "session": "afterhours",
        }

    monkeypatch.setattr(px, "_alpaca", lambda s: 43.46)
    monkeypatch.setattr(px, "get_intraday_session", _fake_full)
    cached = px._intraday_cache.get("WOLF")
    out = dict(cached[1])
    assert out.get("today_open") is None
    assert not (out.get("today_open") is not None and out.get("today_high") is not None)


class TestIntradayBreakoutPct:
    """Force-refresh must key on range BREAKOUT, not distance from high/low.

    Regression: the original P2-6 check used max(distance-from-high,
    distance-from-low), which is >= half the day's range by construction — so
    every volatile symbol busted the cache on every call, hammering Alpaca
    into its rate-limit breaker (50 calls/60s -> OPEN 300s) and starving the
    squeeze scan.
    """

    def test_inside_range_is_never_stale(self):
        from core.prices import _intraday_breakout_pct

        # Stock down 8% on the day: price sits far below the high all session.
        assert _intraday_breakout_pct(9.20, 10.00, 9.15) == 0.0
        # Mid-range.
        assert _intraday_breakout_pct(9.50, 10.00, 9.00) == 0.0
        # Exactly at the boundaries.
        assert _intraday_breakout_pct(10.00, 10.00, 9.00) == 0.0
        assert _intraday_breakout_pct(9.00, 10.00, 9.00) == 0.0

    def test_breakout_above_high(self):
        from core.prices import _intraday_breakout_pct

        pct = _intraday_breakout_pct(10.50, 10.00, 9.00)
        assert abs(pct - 5.0) < 1e-9

    def test_breakout_below_low(self):
        from core.prices import _intraday_breakout_pct

        pct = _intraday_breakout_pct(8.55, 10.00, 9.00)
        assert abs(pct - 5.0) < 1e-9

    def test_garbage_inputs_are_not_stale(self):
        from core.prices import _intraday_breakout_pct

        assert _intraday_breakout_pct(None, 10.0, 9.0) == 0.0
        assert _intraday_breakout_pct("x", 10.0, 9.0) == 0.0
        assert _intraday_breakout_pct(9.5, 0, 9.0) == 0.0
        assert _intraday_breakout_pct(9.5, 10.0, -1) == 0.0

    def test_cache_survives_big_red_day(self, monkeypatch):
        """Price 8% below the cached high but inside the range: the cache-hit
        path must return cached OHLC without deleting the entry."""
        import time as _time

        from core import prices as px

        px._intraday_cache.clear()
        px._intraday_cache["DJT"] = (
            _time.time(),
            {
                "symbol": "DJT",
                "price": 20.0,
                "today_open": 21.5,
                "today_high": 21.8,
                "today_low": 19.9,
                "previous_close": 21.7,
                "session": "rth",
            },
        )
        monkeypatch.setattr(px, "_alpaca", lambda s: 20.05)  # inside [19.9, 21.8]
        out = px.get_intraday_session("DJT")
        assert out["today_high"] == 21.8
        assert "DJT" in px._intraday_cache
        px._intraday_cache.clear()
