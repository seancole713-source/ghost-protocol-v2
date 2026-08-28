"""Tests for unified US equity session clock (Central Time)."""
import datetime as dt

from core.market_hours import (
    in_daily_model_issuance_window,
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


def test_market_holiday_blocks_all_sessions():
    """Forensic MD-9: a NYSE full-day holiday must not report any live session."""
    from core.market_hours import is_market_holiday

    # 2026-07-03 is Independence Day (observed) — a Friday.
    assert is_market_holiday(dt.date(2026, 7, 3)) is True
    t = _ct(2026, 7, 3, 10, 0)  # 10 AM CT on the holiday
    assert is_us_rth(t) is False
    assert is_us_premarket(t) is False
    assert is_us_after_hours(t) is False
    assert is_us_extended_hours(t) is False
    assert market_session_label(t) == "Market Closed"


def test_weekend_is_holiday():
    from core.market_hours import is_market_holiday
    assert is_market_holiday(dt.date(2026, 6, 13)) is True  # Saturday
    assert is_market_holiday(dt.date(2026, 6, 14)) is True  # Sunday


def test_half_day_closes_early():
    """Forensic MD-9: half-days close at 12:00 PM CT, not 3:00 PM."""
    # 2026-11-27 is the day after Thanksgiving (early close).
    t_before = _ct(2026, 11, 27, 11, 0)   # 11 AM CT — still RTH
    t_after = _ct(2026, 11, 27, 13, 0)    # 1 PM CT — after early close
    assert is_us_rth(t_before) is True
    assert is_us_rth(t_after) is False
    assert is_us_after_hours(t_after) is True


def test_daily_model_issuance_window_tracks_normal_and_early_close():
    assert in_daily_model_issuance_window(_ct(2026, 6, 10, 15, 4)) is False
    assert in_daily_model_issuance_window(_ct(2026, 6, 10, 15, 5)) is True
    assert in_daily_model_issuance_window(_ct(2026, 6, 10, 15, 59)) is True
    assert in_daily_model_issuance_window(_ct(2026, 6, 10, 16, 0)) is False
    assert in_daily_model_issuance_window(_ct(2026, 11, 27, 12, 5)) is True
    assert in_daily_model_issuance_window(_ct(2026, 11, 27, 13, 0)) is False


def test_holiday_env_override():
    from core.market_hours import is_market_holiday
    import os
    os.environ["GHOST_HOLIDAYS"] = "2026-08-21"
    try:
        assert is_market_holiday(dt.date(2026, 8, 21)) is True
    finally:
        os.environ.pop("GHOST_HOLIDAYS", None)
