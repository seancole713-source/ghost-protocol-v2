"""tests/test_expired_honesty.py — review findings P3-1 + P3-2 (2026-07-17).

P3-1: genuine full-term EXPIRED picks (reconciler-written, pnl_pct set) must
count as non-wins in every gate-facing win-rate denominator; administrative
voids (dupe cleaner / portfolio purge, pnl_pct NULL) stay excluded.
P3-2: a failed (stored=0) options-snapshot run must not burn the daily claim.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import core.options_snapshots as osnap
from core.prediction_filters import RESOLVED_FOR_WINRATE_WHERE


# ── P3-1: the discriminator and its application ──────────────────────

class TestWinrateWhere:
    def test_fragment_semantics(self):
        # WIN/LOSS always in; EXPIRED only with pnl (reconciler); voids out.
        assert "outcome IN ('WIN','LOSS')" in RESOLVED_FOR_WINRATE_WHERE
        assert "outcome='EXPIRED' AND pnl_pct IS NOT NULL" in RESOLVED_FOR_WINRATE_WHERE

    def test_prediction_gate_sites_use_fragment(self):
        import os
        path = os.path.join(os.path.dirname(__file__), "..", "core", "prediction.py")
        src = open(path).read()
        # Objective-gate bootstrap stats + last-50 direction stats + the
        # win-rate window queries all use the shared fragment.
        assert src.count("RESOLVED_FOR_WINRATE_WHERE") >= 7
        # The consecutive-LOSS streak detector intentionally keeps WIN/LOSS:
        # an expiry is not a loss and must not feed the loss-streak cooldown.
        assert "SELECT outcome, resolved_at FROM predictions WHERE outcome IN ('WIN','LOSS') " in src

    def test_ghost_ask_uses_fragment(self):
        import os
        src = open(os.path.join(os.path.dirname(__file__), "..", "core",
                                "ghost_ask.py")).read()
        assert src.count("RESOLVED_FOR_WINRATE_WHERE") >= 2

    def test_shadow_recalibration_counts_expired(self):
        import os
        src = open(os.path.join(os.path.dirname(__file__), "..", "core",
                                "live_recalibration.py")).read()
        assert "outcome IN ('WIN','LOSS','EXPIRED')" in src

    def test_fragment_math(self):
        """Simulate the denominator against a mixed population."""
        rows = [
            {"outcome": "WIN", "pnl_pct": 2.0},       # counted, win
            {"outcome": "LOSS", "pnl_pct": -1.3},     # counted
            {"outcome": "EXPIRED", "pnl_pct": 0.4},   # genuine: counted, non-win
            {"outcome": "EXPIRED", "pnl_pct": None},  # admin void: excluded
            {"outcome": "WITHDRAWN", "pnl_pct": 1.4}, # excluded
        ]
        def matches(r):
            return (r["outcome"] in ("WIN", "LOSS")
                    or (r["outcome"] == "EXPIRED" and r["pnl_pct"] is not None))
        counted = [r for r in rows if matches(r)]
        wins = sum(1 for r in counted if r["outcome"] == "WIN")
        assert len(counted) == 3 and wins == 1
        # WIN/LOSS-only math would claim 1/2 = 50%; honest math: 1/3.


# ── P3-2: re-claimable snapshot day ──────────────────────────────────

def _at_window(monkeypatch):
    fake = datetime(2026, 7, 17, 14, 0, tzinfo=ZoneInfo("America/Chicago"))
    monkeypatch.setattr(osnap, "_ct_now", lambda: fake)
    monkeypatch.setattr(osnap, "_ct_today", lambda: "2026-07-17")


class _Cur:
    def __init__(self, row, log): self._row, self._log = row, log
    def execute(self, sql, params=None): self._log.append((sql, params))
    def fetchone(self): return self._row


class _Conn:
    def __init__(self, row, log): self._row, self._log = row, log
    def cursor(self): return _Cur(self._row, self._log)
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _patch_state(monkeypatch, claim):
    import core.db as db
    log = []
    row = (json.dumps(claim),) if isinstance(claim, dict) else (
        (claim,) if claim else None)
    monkeypatch.setattr(db, "db_conn", lambda: _Conn(row, log))
    return log


class TestSnapshotReclaim:
    def test_failed_day_is_retried(self, monkeypatch):
        _at_window(monkeypatch)
        _patch_state(monkeypatch, {"date": "2026-07-17", "status": "done",
                                   "stored": 0, "ts": int(time.time())})
        ran = []
        monkeypatch.setattr(osnap, "record_snapshots",
                            lambda: ran.append(1) or {"ok": True, "stored": 5})
        out = osnap.run_options_snapshot_job()
        assert ran, "stored=0 day must be re-claimable"
        assert out["stored"] == 5

    def test_successful_day_skips(self, monkeypatch):
        _at_window(monkeypatch)
        _patch_state(monkeypatch, {"date": "2026-07-17", "status": "done",
                                   "stored": 42, "ts": int(time.time())})
        assert osnap.run_options_snapshot_job()["skipped"] == "already_ran_today"

    def test_fresh_running_claim_skips(self, monkeypatch):
        _at_window(monkeypatch)
        _patch_state(monkeypatch, {"date": "2026-07-17", "status": "running",
                                   "stored": 0, "ts": int(time.time()) - 60})
        assert osnap.run_options_snapshot_job()["skipped"] == "run_in_progress"

    def test_stale_running_claim_is_retried(self, monkeypatch):
        _at_window(monkeypatch)
        _patch_state(monkeypatch, {"date": "2026-07-17", "status": "running",
                                   "stored": 0, "ts": int(time.time()) - 3600})
        monkeypatch.setattr(osnap, "record_snapshots",
                            lambda: {"ok": True, "stored": 3})
        assert osnap.run_options_snapshot_job()["stored"] == 3

    def test_legacy_plain_date_claim_skips(self, monkeypatch):
        _at_window(monkeypatch)
        _patch_state(monkeypatch, "2026-07-17")
        assert osnap.run_options_snapshot_job()["skipped"] == "already_ran_today"

    def test_done_claim_written_after_run(self, monkeypatch):
        _at_window(monkeypatch)
        log = _patch_state(monkeypatch, None)
        monkeypatch.setattr(osnap, "record_snapshots",
                            lambda: {"ok": True, "stored": 7})
        osnap.run_options_snapshot_job()
        writes = [p for s, p in log if "INSERT INTO ghost_state" in s]
        assert len(writes) == 2  # running claim, then done claim
        done = json.loads(writes[-1][1])
        assert done["status"] == "done" and done["stored"] == 7
