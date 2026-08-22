"""Tests for the point-in-time checklist snapshot store.

Two things are load-bearing here: a snapshot is never rewritten once issued
(no backfill), and only resolved rows ever reach calibration.
"""
from __future__ import annotations

from core import checklist_ledger as ck


_COHORT = {
    "checklist_version": "catalyst_checklist_v1",
    "hold_bars": 3,
    "outcome_contract": ck.DEFAULT_OUTCOME_CONTRACT,
    "direction": "UP",
}


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
    assert "CREATE INDEX IF NOT EXISTS idx_checklist_snapshots_score_outcome" in sql
    assert cur.params[3] == (ck.LEGACY_OUTCOME_CONTRACT,)


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
        rows = ck.resolved_samples_for_calibration(**_COHORT)
    finally:
        core_db.db_conn = saved

    assert rows == [{"score_pct": 80.0, "won": True}, {"score_pct": 40.0, "won": False}]
    executed_sql = " ".join(cur.executed)
    assert "outcome IS NOT NULL" in executed_sql
    assert "checklist_version=%s" in executed_sql
    assert "hold_bars=%s" in executed_sql
    assert "outcome_contract=%s" in executed_sql
    assert "direction=%s" in executed_sql


def test_historical_cutoff_requires_sample_and_outcome_before_decision(monkeypatch):
    ck._bust_calibration_cache()
    cur = _Cursor(fetchall_rows=[])
    _with_fake_db(monkeypatch, cur)

    ck.resolved_samples_for_calibration(**_COHORT, min_issued_before=1_766_000_123)

    sql = " ".join(cur.executed)
    assert "issued_at < %s" in sql
    assert "resolved_at < %s" in sql
    query_params = cur.params[-1]
    assert query_params[-2:] == (1_766_000_123, 1_766_000_123)


def test_read_helpers_are_select_only(monkeypatch):
    """GET/calibration paths must not run migrations or mutate ledger data."""
    cases = [
        (
            lambda: ck.resolved_samples_for_calibration(
                **_COHORT, min_issued_before=1_766_000_123,
            ),
            _Cursor(fetchall_rows=[]),
        ),
        (lambda: ck.snapshot_for_prediction(7), _Cursor(fetchone_rows=[None])),
        (lambda: ck.recent_resolved_across_symbols(25), _Cursor(fetchall_rows=[])),
        (lambda: ck.recent_snapshots("WOLF", 20), _Cursor(fetchall_rows=[])),
    ]
    for read, cur in cases:
        ck._bust_calibration_cache()
        _with_fake_db(monkeypatch, cur)
        read()
        assert cur.executed
        assert all(sql.strip().upper().startswith("SELECT") for sql in cur.executed)


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


def test_snapshot_open_predictions_is_retired_without_database_or_collection(monkeypatch):
    """Delayed current-state reconstruction must never masquerade as issue-time evidence."""
    import core.checklist_evidence as ce

    monkeypatch.setattr(
        ce,
        "collect_evidence",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not collect")),
    )
    out = ck.snapshot_open_predictions()
    assert out == {
        "ok": False,
        "retired": True,
        "snapshotted": 0,
        "failed": 0,
        "scanned": 0,
        "reason": "checklists_are_frozen_at_prediction_issuance",
    }


def test_store_snapshot_with_cursor_is_insert_only_and_preserves_issue_time(monkeypatch):
    cur = _Cursor(fetchone_rows=[(91,)])
    monkeypatch.setattr(ck, "validate_outcome_contract", lambda: None)
    report = {
        "checklist_version": "catalyst_checklist_v1",
        "hold_bars": 3,
        "score_pct": 40.0,
        "blocked": False,
    }

    row_id = ck.store_snapshot_with_cursor(
        cur,
        symbol="wolf",
        direction="up",
        report=report,
        evidence={"relative_volume": 3.0},
        issued_at=1_766_000_123,
        prediction_id=7,
    )

    assert row_id == 91
    sql = " ".join(cur.executed)
    assert "INSERT INTO ghost_checklist_snapshots" in sql
    assert "CREATE TABLE" not in sql
    assert "ALTER TABLE" not in sql
    params = cur.params[0]
    assert params[4] == 1_766_000_123
    assert params[15] == 1_766_000_123
    assert params[14] == 7


def test_outcome_contract_fails_closed_on_horizon_mismatch(monkeypatch):
    monkeypatch.setattr(ck, "label_hold_bars", lambda: ck.HOLD_BARS + 1)
    try:
        ck.validate_outcome_contract()
    except RuntimeError as exc:
        assert "contract mismatch" in str(exc)
    else:
        raise AssertionError("mismatched hold horizons must fail closed")


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
    first = ck.resolved_samples_for_calibration(**_COHORT)
    assert first == [{"score_pct": 80.0, "won": True}]

    # Second read: swap in a cursor that would error if actually queried.
    class _Exploding:
        def execute(self, *a):
            raise AssertionError("cache should have served this read")

    _with_fake_db(monkeypatch, _Exploding())
    assert ck.resolved_samples_for_calibration(**_COHORT) == first  # cache hit

    ck._bust_calibration_cache()
    cur2 = _Cursor(fetchall_rows=[(80.0, True), (40.0, False)])
    _with_fake_db(monkeypatch, cur2)
    assert len(ck.resolved_samples_for_calibration(**_COHORT)) == 2  # fresh read after bust
