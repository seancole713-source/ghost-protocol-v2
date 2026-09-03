"""The weekly retrain could never fire.

Found 2026-09-03 while looking for a way to trigger a retrain.

core/scheduler.register() sets `next_run_at = time.time() + interval_s` and
that field is in-memory only — nothing persists it across process restarts. So
registering weekly_retrain at interval_s=604800 pushed its next run seven days
out on EVERY container start. Railway redeploys this service on every merge and
every env change; it went out eight times on 2026-09-03 alone. A job needing
seven uninterrupted days therefore never ran, and models only ever refreshed
when someone POSTed /api/v3/train by hand.

The job already had the right mechanism for cadence: it checks
last_weekly_retrain_ts in ghost_state against WEEKLY_RETRAIN_MIN_INTERVAL_SEC
(default 604800). That IS durable across restarts. The scheduler interval only
needs to be short enough to ask the question — so poll hourly and let the DB
gate decide.
"""
from __future__ import annotations

import re
from pathlib import Path

import core.scheduler as sched


ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------- the scheduler premise --

def test_register_resets_the_next_run_to_a_full_interval(monkeypatch):
    """The behaviour that made a 7-day interval unreachable.

    Not a bug in the scheduler — it is why a long interval cannot be used as a
    cadence on a frequently-redeployed service.
    """
    sched._tasks.pop("probe", None)
    sched.register("probe", lambda: None, interval_s=604800)
    task = sched._tasks["probe"]

    import time
    assert task.next_run_at - time.time() > 604000, (
        "register no longer defers by a full interval — if it now persists or "
        "runs immediately, the weekly_retrain fix below can be revisited"
    )
    sched._tasks.pop("probe", None)


def test_next_run_at_is_not_persisted():
    """Nothing writes next_run_at anywhere durable, so a restart always
    restarts the clock. That is the whole reason a 7-day interval was
    unreachable, so if the scheduler ever gains persistence this analysis
    should be revisited."""
    src = (ROOT / "core" / "scheduler.py").read_text(encoding="utf-8")

    for token in ("ghost_state", "INSERT INTO", "pickle", "json.dump", "shelve"):
        assert token not in src, f"scheduler now persists state via {token}"

    # Every assignment is from the in-process clock, never a stored value.
    for assignment in re.findall(r"next_run_at\s*=\s*(.+)", src):
        assert ("time.time()" in assignment or "now" in assignment
                or "field(" in assignment), assignment


# ------------------------------------------------------ the registration --

def test_weekly_retrain_polls_often_enough_to_actually_fire():
    """Regression: registered at 604800 it could never run on a service that
    redeploys more than weekly."""
    src = (ROOT / "wolf_app.py").read_text(encoding="utf-8")

    match = re.search(
        r'scheduler\.register\(\s*\n?\s*"weekly_retrain",\s*_weekly_retrain,\s*\n?'
        r'\s*interval_s=(\d+)',
        src,
    )
    assert match, "weekly_retrain registration not found in the expected form"

    interval = int(match.group(1))
    assert interval <= 86400, (
        f"weekly_retrain polls every {interval}s; next_run_at resets on every "
        f"restart, so an interval longer than the deploy cadence means it "
        f"never fires"
    )


def test_weekly_retrain_carries_a_timeout_for_full_fleet_runs():
    """A five-year full-fleet run exceeded three hours (PR #169). The default
    task timeout would mark it failed while the shielded work kept going."""
    src = (ROOT / "wolf_app.py").read_text(encoding="utf-8")

    match = re.search(
        r'"weekly_retrain", _weekly_retrain,\s*\n?\s*interval_s=\d+,\s*'
        r'timeout_s=(\w+)',
        src,
    )
    assert match, "weekly_retrain must register an explicit timeout_s"
    assert match.group(1) != "None"


def test_cadence_is_still_enforced_by_the_durable_db_gate():
    """Polling hourly must not mean retraining hourly — the ghost_state
    timestamp check is what keeps it weekly, and it survives restarts."""
    src = (ROOT / "wolf_app.py").read_text(encoding="utf-8")

    assert "WEEKLY_RETRAIN_MIN_INTERVAL_SEC" in src
    assert "last_weekly_retrain_ts" in src
    assert 'if last_ts and (now_ts - last_ts) < min_interval_s:' in src, (
        "the durable cadence gate is gone — with an hourly poll and no DB "
        "check this would retrain every hour"
    )
