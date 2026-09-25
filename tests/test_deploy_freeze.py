"""F29: market-hours deploy freeze in Railway's pre-deploy step."""
from __future__ import annotations

import io
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import deploy_freeze as df
from scripts import runtime_preflight as preflight

ROOT = Path(__file__).resolve().parents[1]


def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


@pytest.mark.parametrize("now,frozen", [
    # Thu 2026-09-24 (EDT, UTC-4)
    (_utc(2026, 9, 24, 11, 59), False),   # 07:59 ET
    (_utc(2026, 9, 24, 12, 0), True),     # 08:00 ET: freeze starts
    (_utc(2026, 9, 24, 17, 0), True),     # 13:00 ET
    (_utc(2026, 9, 24, 20, 29), True),    # 16:29 ET
    (_utc(2026, 9, 24, 20, 30), False),   # 16:30 ET: freeze ends
    (_utc(2026, 9, 25, 2, 0), False),     # 22:00 ET the night before
    # Wed 2026-12-02 (EST, UTC-5): DST handled by the tz, not a fixed offset
    (_utc(2026, 12, 2, 12, 30), False),   # 07:30 ET
    (_utc(2026, 12, 2, 13, 0), True),     # 08:00 ET
    (_utc(2026, 12, 2, 21, 29), True),    # 16:29 ET
    (_utc(2026, 12, 2, 21, 30), False),   # 16:30 ET
    # Weekend and NYSE holidays from edge/calendar.py
    (_utc(2026, 9, 26, 15, 0), False),    # Saturday
    (_utc(2026, 11, 26, 15, 0), False),   # Thanksgiving
    (_utc(2026, 12, 25, 15, 0), False),   # Christmas
    (_utc(2026, 11, 27, 15, 0), True),    # early-close day still trades
])
def test_freeze_window_uses_new_york_time_and_market_calendar(now, frozen):
    assert df.in_freeze_window(now) is frozen


def test_blocked_inside_window_with_clear_message():
    out = io.StringIO()
    assert df.check(_utc(2026, 9, 24, 14, 0), env={}, out=out) == 1
    msg = out.getvalue()
    assert "BLOCKED" in msg and "DEPLOY_FREEZE_OVERRIDE=1" in msg
    assert "previous deployment" in msg


def test_override_allows_deploy_inside_window():
    out = io.StringIO()
    assert df.check(_utc(2026, 9, 24, 14, 0), env={"DEPLOY_FREEZE_OVERRIDE": "1"}, out=out) == 0
    assert "DEPLOY_FREEZE_OVERRIDE=1 is set" in out.getvalue()


def test_override_other_values_do_not_unfreeze():
    assert df.check(_utc(2026, 9, 24, 14, 0), env={"DEPLOY_FREEZE_OVERRIDE": "0"}, out=io.StringIO()) == 1


def test_allowed_outside_window():
    out = io.StringIO()
    assert df.check(_utc(2026, 9, 24, 21, 0), env={}, out=out) == 0
    assert "deploy allowed" in out.getvalue()


def test_unexpected_error_allows_deploy_with_warning():
    def broken_calendar(day):
        raise RuntimeError("calendar exploded")

    out = io.StringIO()
    assert df.check(_utc(2026, 9, 24, 14, 0), env={}, is_trading_day=broken_calendar, out=out) == 0
    assert "WARNING" in out.getvalue() and "allowing deploy" in out.getvalue()


def test_timezone_failure_allows_deploy(monkeypatch):
    def no_tz():
        raise LookupError("no tz database")

    monkeypatch.setattr(df, "_new_york_tz", no_tz)
    out = io.StringIO()
    assert df.check(_utc(2026, 9, 24, 14, 0), env={}, out=out) == 0
    assert "WARNING" in out.getvalue()


def test_preflight_runs_freeze_only_with_pre_deploy_flag(monkeypatch, capsys):
    monkeypatch.setattr(preflight, "run_checks", lambda: {"status": "ok"})
    calls = []
    monkeypatch.setattr(df, "check", lambda: calls.append(1) or 1)
    # Boot (Procfile) and CI: no flag -> no freeze, a mid-session restart boots.
    assert preflight.main([]) == 0
    assert calls == []
    # Railway pre-deploy: the freeze decides.
    assert preflight.main(["--pre-deploy"]) == 1
    assert calls == [1]


def test_preflight_native_failure_still_wins_over_freeze(monkeypatch):
    def fail():
        raise ImportError("GLIBC")

    monkeypatch.setattr(preflight, "run_checks", fail)
    monkeypatch.setattr(df, "check", lambda: 0)
    assert preflight.main(["--pre-deploy"]) == 1


def test_preflight_freeze_crash_allows_deploy(monkeypatch, capsys):
    def boom():
        raise RuntimeError("unexpected")

    monkeypatch.setattr(df, "check", boom)
    assert preflight.deploy_freeze_check() == 0
    assert "allowing deploy" in capsys.readouterr().out


def test_script_runs_standalone_with_override():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "deploy_freeze.py")],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "DEPLOY_FREEZE_OVERRIDE": "1"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[DEPLOY_FREEZE]" in proc.stdout
    assert "WARNING" not in proc.stdout  # calendar + tz load from a script run
