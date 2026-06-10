"""Tests for unified US equity session clock (Central Time)."""
import datetime as dt

from core.market_hours import (
    is_us_after_hours,
    is_us_extended_hours,
    is_us_premarket,
    is_us_rth,
    market_session_label,
    now_ct_iso,
    now_et_iso,
)


def _ct(y, m, d, h, mi):
    try:
        from zoneinfo import ZoneInfo

        return dt.datetime(y, m, d, h, mi, tzinfo=ZoneInfo("America/Chicago"))
    except Exception:
        return dt.datetime(y, m, d, h, mi) - dt.timedelta(hours=6)


def test_premarket_false_five_minutes_after_open():
    """Bug regression: 8:35 AM CT must not be premarket."""
    t = _ct(2026, 6, 10, 8, 35)
    assert is_us_rth(t) is True
    assert is_us_premarket(t) is False
    assert market_session_label(t) == "Market Open"


def test_premarket_true_before_open():
    t = _ct(2026, 6, 10, 7, 0)
    assert is_us_premarket(t) is True
    assert is_us_rth(t) is False
    assert market_session_label(t) == "Pre-Market"


def test_after_hours_at_222_pm_ct_is_not_rth():
    """2:22 PM CT is still regular session; 3:22 PM CT is after hours."""
    t_open = _ct(2026, 6, 10, 14, 22)
    assert is_us_rth(t_open) is True
    assert is_us_after_hours(t_open) is False
    assert market_session_label(t_open) == "Market Open"

    t_ah = _ct(2026, 6, 10, 15, 22)
    assert is_us_rth(t_ah) is False
    assert is_us_after_hours(t_ah) is True
    assert is_us_extended_hours(t_ah) is True
    assert market_session_label(t_ah) == "After Hours"


def test_now_ct_iso_contains_ct():
    t = _ct(2026, 6, 10, 15, 22)
    assert "CT" in now_ct_iso(t)
    assert "CT" in now_et_iso(t)
