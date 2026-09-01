"""Tests for wolf_app._coverage_maintenance_job's low-yield backoff threshold.

Regression fixture for a self-perpetuating stall found live in production
(2026-09-01): the fleet's genuine tier=proven rate is ~17% (44/255 model
slots), structurally below the old 0.25 low_yield_ratio floor. Every
full-batch retrain therefore re-triggered the 12h backoff regardless of
outcome, so coverage could never climb no matter how many times it retried.
The fix lowers the ratio to 0.05 -- still a real floor against genuine
pipeline breakage, not a floor against this fleet's normal heterogeneous
yield.
"""
from __future__ import annotations

from pathlib import Path

import wolf_app

ROOT = Path(__file__).resolve().parents[1]


class _FakeCursor:
    def __init__(self, *, last_retrain_ts=0, low_yield_until_ts=0):
        self.last_retrain_ts = last_retrain_ts
        self.low_yield_until_ts = low_yield_until_ts
        self.executed = []
        self._last_sql = ""

    def execute(self, sql, params=None):
        self._last_sql = sql
        self.executed.append((sql, params))

    def fetchone(self):
        if "last_coverage_low_yield_until_ts" in self._last_sql:
            return (str(self.low_yield_until_ts),) if self.low_yield_until_ts else None
        if "last_coverage_retrain_ts" in self._last_sql:
            return (str(self.last_retrain_ts),) if self.last_retrain_ts else None
        return None

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur


class _FakeDbCtx:
    def __init__(self, cur):
        self._conn = _FakeConn(cur)

    def __enter__(self):
        return self._conn

    def __exit__(self, *a):
        return False


def _run_job(monkeypatch, *, acc_ratio, now_ts=2_000_000):
    """Drive _coverage_maintenance_job end to end with a controlled acc_ratio,
    and return the low-yield backoff write (or None if none was written)."""
    cur = _FakeCursor()
    monkeypatch.setattr(wolf_app, "db_conn", lambda: _FakeDbCtx(cur))
    monkeypatch.setattr(wolf_app.time, "time", lambda: now_ts)
    monkeypatch.setattr(wolf_app, "_APP_BOOT_TS", now_ts - 10_000)
    monkeypatch.setattr(wolf_app, "_COVERAGE_RETRAIN_RUNNING", False)
    monkeypatch.setattr(
        "core.signal_engine.get_model_status",
        lambda: {"trained": True, "models": 1, "symbols": {}},
    )
    monkeypatch.setattr(
        wolf_app, "_watchlist_missing_symbol_pairs",
        lambda: [("AAPL", "stock"), ("ABCL", "stock")],
    )
    monkeypatch.setattr(
        "core.signal_engine.train_and_validate",
        lambda syms: (None, acc_ratio, True),
    )
    monkeypatch.setattr(wolf_app, "_auto_purge_bad_models", lambda: 0)
    monkeypatch.setattr(wolf_app, "_purge_v3_stale_or_weak", lambda: 0)
    monkeypatch.setattr(wolf_app, "_bump_cockpit_db_cache", lambda: None)

    wolf_app._coverage_maintenance_job()

    for sql, params in cur.executed:
        if "last_coverage_low_yield_until_ts" in sql and params and params[0] not in (None,) and "SELECT" not in sql.upper():
            return int(params[0])
    return None


def test_default_low_yield_ratio_is_005(monkeypatch):
    monkeypatch.delenv("COVERAGE_LOW_YIELD_RATIO", raising=False)
    # 0.17 matches the real fleet's proven rate (44/255) -- must NOT trigger
    # backoff under the new default, though it would have under the old 0.25.
    backoff = _run_job(monkeypatch, acc_ratio=0.17)
    assert backoff is None


def test_old_threshold_would_have_blocked_the_real_fleet_rate(monkeypatch):
    """Documents the bug this fixes: under the old 0.25 default, the fleet's
    own genuine ~17% proven rate reads as 'low yield' and stalls forever."""
    monkeypatch.setenv("COVERAGE_LOW_YIELD_RATIO", "0.25")
    backoff = _run_job(monkeypatch, acc_ratio=0.17, now_ts=2_000_000)
    assert backoff == 2_000_000 + 43200


def test_genuine_pipeline_failure_still_backs_off(monkeypatch):
    """The safety valve must still catch real breakage (e.g. a bug that
    zeroes out every symbol), not just be disabled outright."""
    monkeypatch.delenv("COVERAGE_LOW_YIELD_RATIO", raising=False)
    backoff = _run_job(monkeypatch, acc_ratio=0.0, now_ts=2_000_000)
    assert backoff == 2_000_000 + 43200


def test_env_override_still_wins(monkeypatch):
    monkeypatch.setenv("COVERAGE_LOW_YIELD_RATIO", "0.5")
    backoff = _run_job(monkeypatch, acc_ratio=0.17, now_ts=2_000_000)
    assert backoff == 2_000_000 + 43200  # 0.17 < 0.5 override -> still backs off


def test_coverage_maintenance_has_an_explicit_timeout():
    """Regression: this job previously had no timeout_s override and silently
    inherited the scheduler's 120s default, logging a false TIMEOUT error on
    every run despite the real (shielded, non-cancelled) work continuing to
    completion -- confirmed in production logs."""
    source = (ROOT / "wolf_app.py").read_text(encoding="utf-8")
    idx = source.index('scheduler.register(\n            "coverage_maintenance"')
    registration = source[idx:idx + 500]
    assert "timeout_s=" in registration
