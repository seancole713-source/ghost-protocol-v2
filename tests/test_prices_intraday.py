"""RTH intraday OHLC aggregation from Alpaca-style bars."""
from core.prices import _ohlc_from_bars, _RTH_OPEN_MIN, _RTH_CLOSE_MIN


def _bar(hour, minute, o, h, l, c):
    return {"t": f"2026-06-08T{hour:02d}:{minute:02d}:00-04:00", "o": o, "h": h, "l": l, "c": c}


def test_ohlc_from_bars_rth_window():
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    bars = [
        _bar(4, 0, 4.20, 4.25, 4.18, 4.22),   # premarket — excluded from RTH
        _bar(9, 30, 4.55, 4.56, 4.50, 4.54),  # RTH open
        _bar(10, 0, 4.54, 4.56, 4.13, 4.40),
        _bar(15, 55, 4.40, 4.42, 4.38, 4.39),
    ]
    ext_o, ext_h, ext_l = _ohlc_from_bars(bars, et)
    assert ext_o == 4.20
    assert ext_h == 4.56
    assert ext_l == 4.13

    rth_o, rth_h, rth_l = _ohlc_from_bars(
        bars, et, start_min=_RTH_OPEN_MIN, end_min=_RTH_CLOSE_MIN,
    )
    assert rth_o == 4.55
    assert rth_h == 4.56
    assert rth_l == 4.13
