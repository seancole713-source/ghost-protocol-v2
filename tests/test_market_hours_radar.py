"""Market hours — next radar resume label."""

from datetime import datetime
from zoneinfo import ZoneInfo

from core.market_hours import next_radar_resume_label, is_us_extended_hours


def test_next_radar_resume_weekday_evening():
    ct = ZoneInfo("America/Chicago")
    tue_730pm = datetime(2026, 6, 10, 19, 30, tzinfo=ct)
    assert next_radar_resume_label(tue_730pm) == "3:00 AM CT"
    assert is_us_extended_hours(tue_730pm) is False


def test_next_radar_resume_friday_evening():
    ct = ZoneInfo("America/Chicago")
    fri_730pm = datetime(2026, 6, 12, 19, 30, tzinfo=ct)
    assert next_radar_resume_label(fri_730pm) == "Mon 3:00 AM CT"


def test_next_radar_resume_live_during_premarket():
    ct = ZoneInfo("America/Chicago")
    pre = datetime(2026, 6, 11, 4, 0, tzinfo=ct)
    assert next_radar_resume_label(pre) == "now (live)"
    assert is_us_extended_hours(pre) is True
