"""Coverage-maintenance scheduler regression tests."""
from __future__ import annotations

import wolf_app


def test_coverage_maintenance_schedule_defaults(monkeypatch):
    monkeypatch.delenv("COVERAGE_CHECK_INTERVAL_SEC", raising=False)
    monkeypatch.delenv("COVERAGE_MAINTENANCE_TIMEOUT_SEC", raising=False)

    assert wolf_app._coverage_maintenance_schedule() == (3600, 21600)


def test_coverage_maintenance_timeout_override(monkeypatch):
    monkeypatch.setenv("COVERAGE_CHECK_INTERVAL_SEC", "3600")
    monkeypatch.setenv("COVERAGE_MAINTENANCE_TIMEOUT_SEC", "9000")

    assert wolf_app._coverage_maintenance_schedule() == (3600, 9000)


def test_coverage_maintenance_timeout_never_precedes_next_tick(monkeypatch):
    monkeypatch.setenv("COVERAGE_CHECK_INTERVAL_SEC", "10800")
    monkeypatch.setenv("COVERAGE_MAINTENANCE_TIMEOUT_SEC", "7200")

    assert wolf_app._coverage_maintenance_schedule() == (10800, 10800)
