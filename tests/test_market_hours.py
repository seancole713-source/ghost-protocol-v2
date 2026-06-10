"""Tests for unified US equity session clock (ET)."""
import datetime as dt

from core.market_hours import (
    is_us_premarket,
    is_us_rth,
    market_session_label,
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
