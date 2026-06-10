"""Tests for unified US equity session clock (ET)."""
import datetime as dt

from core.market_hours import (
    is_us_after_hours,
    is_us_extended_hours,
    is_us_premarket,
    is_us_rth,
    market_session_label,
    now_et_iso,
)


def _et(y, m, d, h, mi):
    try:
        from zoneinfo import ZoneInfo

        return dt.datetime(y, m, d, h, mi, tzinfo=ZoneInfo("America/New_York"))
    except Exception:
        return dt.datetime(y, m, d, h, mi) - dt.timedelta(hours=5)


def test_premarket_false_five_minutes_after_open():
    """Bug regression: 9:35 ET must not be premarket."""
    t = _et(2026, 6, 10, 9, 35)
    assert is_us_rth(t) is True
    assert is_us_premarket(t) is False
    assert market_session_label(t) == "Market Open"


def test_premarket_true_before_open():
    t = _et(2026, 6, 10, 8, 0)
    assert is_us_premarket(t) is True
    assert is_us_rth(t) is False
    assert market_session_label(t) == "Pre-Market"


def test_after_hours_at_322_pm_et_is_not_rth():
    """3:22 PM ET is still regular session; 4:22 PM ET is after hours."""
    t_open = _et(2026, 6, 10, 15, 22)
    assert is_us_rth(t_open) is True
    assert is_us_after_hours(t_open) is False
    assert market_session_label(t_open) == "Market Open"

    t_ah = _et(2026, 6, 10, 16, 22)
    assert is_us_rth(t_ah) is False
    assert is_us_after_hours(t_ah) is True
    assert is_us_extended_hours(t_ah) is True
    assert market_session_label(t_ah) == "After Hours"


def test_now_et_iso_contains_et():
    t = _et(2026, 6, 10, 16, 22)
    assert "ET" in now_et_iso(t)
