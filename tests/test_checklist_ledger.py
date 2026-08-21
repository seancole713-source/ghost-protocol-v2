"""Tests for the point-in-time checklist snapshot store.

Two things are load-bearing here: a snapshot is never rewritten once issued
(no backfill), and only resolved rows ever reach calibration.
"""
from __future__ import annotations

from core import checklist_ledger as ck


class _Cursor:
    def __init__(self, *, fetchall_rows=None, fetchone_rows=None):
        self.fetchall_rows = list(fetchall_rows or [])
        self.fetchone_rows = list(fetchone_rows or [])
        self.executed = []
        self.params = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        self.params.append(params)

    def fetchall(self):
        return self.fetchall_rows

    def fetchone(self):
        if self.fetchone_rows:
            return self.fetchone_rows.pop(0)
        return None


def test_ensure_tables_creates_snapshot_ledger():
    cur = _Cursor()
    ck.ensure_checklist_tables(cur)
    sql = " ".join(cur.executed)
    assert "CREATE TABLE IF NOT EXISTS ghost_checklist_snapshots" in sql
    assert "outcome VARCHAR(16)" in sql
    assert "won BOOLEAN" in sql


def test_resolved_samples_only_returns_rows_with_a_known_outcome():
    """The SQL WHERE clause is the enforcement point for no-backfill-as-guess."""
    cur = _Cursor(fetchall_rows=[(80.0, True), (40.0, False)])

    class _FakeConn:
        def cursor(self):
            return cur
        def commit(self):
            pass

    class _FakeDbConn:
        def __enter__(self):
            return _FakeConn()
        def __exit__(self, *a):
            return False

    import core.checklist_ledger as mod
    orig = mod.__dict__.get("db_conn")
    import core.db as core_db
    saved = core_db.db_conn
    core_db.db_conn = _FakeDbConn
    try:
        rows = ck.resolved_samples_for_calibration()
    finally:
        core_db.db_conn = saved

    assert rows == [{"score_pct": 80.0, "won": True}, {"score_pct": 40.0, "won": False}]
    executed_sql = " ".join(cur.executed)
    assert "outcome IS NOT NULL" in executed_sql


def test_resolve_snapshot_only_updates_unresolved_rows():
    """WHERE outcome IS NULL is the no-backfill guard: a resolved row is final."""
    cur = _Cursor()

    class _FakeConn:
        def cursor(self):
            return cur
        def commit(self):
            pass

    class _FakeDbConn:
        def __enter__(self):
            return _FakeConn()
        def __exit__(self, *a):
            return False

    import core.db as core_db
    saved = core_db.db_conn
    core_db.db_conn = _FakeDbConn
    try:
        ck.resolve_snapshot(1, outcome="WIN", resolved_price=6.34)
    finally:
        core_db.db_conn = saved

    update_sql = cur.executed[-1]
    assert "outcome IS NULL" in update_sql
    assert cur.params[-1][0] == "WIN"
