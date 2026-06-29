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
