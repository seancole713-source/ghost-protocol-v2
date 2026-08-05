"""Research issuance clock and completed-session identity tests."""

from datetime import datetime
from zoneinfo import ZoneInfo

from core.research_runner import (
    _symbols_in_artifact_scope,
    completed_session_evaluation_date,
    research_scoring_window_open,
)


CT = ZoneInfo("America/Chicago")


def _ct(hour: int, minute: int = 0, *, day: int = 4) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=CT)


def test_scoring_window_requires_one_hour_after_close():
    assert research_scoring_window_open(_ct(15, 59)) is False
    assert research_scoring_window_open(_ct(16, 0)) is True
    assert research_scoring_window_open(_ct(18, 59)) is True
    assert research_scoring_window_open(_ct(19, 0)) is False


def test_scoring_window_rejects_weekends():
    assert research_scoring_window_open(_ct(16, 0, day=8)) is False


def test_evaluation_date_requires_current_completed_bar():
    now = _ct(16, 15)
    assert completed_session_evaluation_date("2026-08-04T00:00:00Z", now) == "2026-08-04"
    assert completed_session_evaluation_date("2026-08-03T00:00:00Z", now) is None


def test_evaluation_date_rejects_bar_before_window():
    assert completed_session_evaluation_date(
        "2026-08-04T00:00:00Z", _ct(15, 30),
    ) is None


def test_pooled_artifact_scope_expands_to_cycle_symbols():
    assert _symbols_in_artifact_scope(
        ["WOLF", "NVDA"], ("__UNIVERSE__",),
    ) == ["WOLF", "NVDA"]


def test_explicit_artifact_scope_intersects_case_insensitively():
    assert _symbols_in_artifact_scope(["WOLF", "NVDA"], ("wolf",)) == ["WOLF"]


def test_ambiguous_artifact_scopes_fail_closed():
    assert _symbols_in_artifact_scope(["WOLF"], ()) == []
    assert _symbols_in_artifact_scope(
        ["WOLF"], ("__UNIVERSE__", "WOLF"),
    ) == []