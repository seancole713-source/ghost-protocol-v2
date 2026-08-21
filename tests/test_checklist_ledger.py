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


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def commit(self):
        pass


class _FakeDbConn:
    """Context-manager factory bound to one cursor."""

    cur = None

    def __enter__(self):
        return _FakeConn(type(self).cur)

    def __exit__(self, *a):
        return False


def _with_fake_db(monkeypatch, cur):
    import core.db as core_db

    fake = type("_Bound", (_FakeDbConn,), {"cur": cur})
    monkeypatch.setattr(core_db, "db_conn", fake)
    return fake


def test_snapshot_open_predictions_snapshots_each_open_pick(monkeypatch):
    """The write half of the calibration loop: open predictions with no
    snapshot get one, linked by prediction_id, with direction normalized."""
    cur = _Cursor(fetchall_rows=[(7, "YMM", "LONG", 3.89, 6.34, 3.56, 1766000000)])
    _with_fake_db(monkeypatch, cur)

    import core.checklist_evidence as ce
    monkeypatch.setattr(ce, "collect_evidence", lambda sym, market_ctx=None: {"relative_volume": 3.0})

    stored = []
    monkeypatch.setattr(ck, "store_snapshot", lambda **kw: stored.append(kw) or 1)

    out = ck.snapshot_open_predictions()
    assert out == {"ok": True, "snapshotted": 1, "failed": 0, "scanned": 1}
    assert stored[0]["prediction_id"] == 7
    assert stored[0]["direction"] == "UP"  # LONG normalized
    assert stored[0]["deadline_ts"] == 1766000000
    # dedupe is in the SQL itself: only rows with no existing snapshot are selected
    assert "NOT EXISTS" in " ".join(cur.executed)


def test_snapshot_open_predictions_isolates_one_symbol_failure(monkeypatch):
    cur = _Cursor(fetchall_rows=[
        (1, "AAA", "UP", 1.0, 2.0, 0.5, None),
        (2, "BBB", "UP", 1.0, 2.0, 0.5, None),
    ])
    _with_fake_db(monkeypatch, cur)

    import core.checklist_evidence as ce

    def _boom_on_aaa(sym, market_ctx=None):
        if sym == "AAA":
            raise RuntimeError("provider down")
        return {}

    monkeypatch.setattr(ce, "collect_evidence", _boom_on_aaa)
    monkeypatch.setattr(ck, "store_snapshot", lambda **kw: 1)

    out = ck.snapshot_open_predictions()
    assert out["snapshotted"] == 1 and out["failed"] == 1


def test_resolve_open_snapshots_copies_outcomes_and_expired_is_nonwin(monkeypatch):
    """The resolver only copies what the TP/SL machinery already concluded --
    and EXPIRED stays in the denominator as a non-win."""
    cur = _Cursor(fetchall_rows=[(11, "WIN", 6.4), (12, "EXPIRED", None)])
    _with_fake_db(monkeypatch, cur)

    resolved = []
    monkeypatch.setattr(
        ck, "resolve_snapshot",
        lambda rid, *, outcome, resolved_price: resolved.append((rid, outcome, resolved_price)),
    )
    out = ck.resolve_open_snapshots()
    assert out == {"ok": True, "resolved": 2, "pending_checked": 2}
    assert (12, "EXPIRED", None) in resolved
    sql = " ".join(cur.executed)
    assert "'WIN', 'LOSS', 'EXPIRED'" in sql  # contract-70 denominator


def test_calibration_cache_served_then_busted_on_resolve(monkeypatch):
    """Repeated reads hit the cache; a resolve busts it so fresh outcomes
    appear without waiting out the TTL."""
    ck._bust_calibration_cache()
    cur1 = _Cursor(fetchall_rows=[(80.0, True)])
    _with_fake_db(monkeypatch, cur1)
    first = ck.resolved_samples_for_calibration()
    assert first == [{"score_pct": 80.0, "won": True}]

    # Second read: swap in a cursor that would error if actually queried.
    class _Exploding:
        def execute(self, *a):
            raise AssertionError("cache should have served this read")

    _with_fake_db(monkeypatch, _Exploding())
    assert ck.resolved_samples_for_calibration() == first  # cache hit

    ck._bust_calibration_cache()
    cur2 = _Cursor(fetchall_rows=[(80.0, True), (40.0, False)])
    _with_fake_db(monkeypatch, cur2)
    assert len(ck.resolved_samples_for_calibration()) == 2  # fresh read after bust
