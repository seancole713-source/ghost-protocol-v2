"""Transaction-boundary regressions for final prediction issuance."""
from __future__ import annotations

from tests.test_prediction_dedup import _cycle_pick, _patch_cycle_inputs


def test_post_lock_safety_blocks_before_review_and_insert(monkeypatch):
    import core.prediction as pred
    import core.pick_review as review
    import core.risk_discipline as risk

    events = []

    class _Cur:
        def execute(self, sql, params=None):
            if "pg_advisory_xact_lock" in sql:
                events.append("lock")
            elif "INSERT INTO predictions" in sql:
                events.append("insert")

    class _Conn:
        def cursor(self):
            return _Cur()

    class _Db:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *args):
            return False

    pick = _cycle_pick()
    _patch_cycle_inputs(monkeypatch, pred, pick)
    monkeypatch.setattr(pred, "db_conn", lambda: _Db())
    monkeypatch.setattr(review, "open_pick_review_enabled", lambda: True)
    monkeypatch.setattr(review, "review_open_picks", lambda *a: events.append("review") or [])

    def block(cur):
        events.append("safety")
        return {"blocked": True, "unavailable": False, "reasons": ["paused"]}

    monkeypatch.setattr(risk, "strict_issuance_block", block)

    saved, diag = pred.run_prediction_cycle(with_diag=True)

    assert saved == []
    assert events[:2] == ["lock", "safety"]
    assert "review" not in events
    assert "insert" not in events
    assert diag["suppress_reason"] == "post_lock_safety"


def test_safety_uncertainty_blocks_before_insert(monkeypatch):
    import core.prediction as pred
    import core.risk_discipline as risk

    events = []

    class _Cur:
        def execute(self, sql, params=None):
            if "pg_advisory_xact_lock" in sql:
                events.append("lock")
            elif "INSERT INTO predictions" in sql:
                events.append("insert")

    class _Conn:
        def cursor(self):
            return _Cur()

    class _Db:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *args):
            return False

    _patch_cycle_inputs(monkeypatch, pred, _cycle_pick())
    monkeypatch.setattr(pred, "db_conn", lambda: _Db())
    monkeypatch.setattr(
        risk,
        "strict_issuance_block",
        lambda cur: {"blocked": True, "unavailable": True, "reasons": ["safety state unavailable"]},
    )

    saved, diag = pred.run_prediction_cycle(with_diag=True)

    assert saved == []
    assert events and events[0] == "lock"
    assert "insert" not in events
    assert diag["suppress_reason"] == "safety_unavailable"


def test_checklist_veto_prevents_prediction_insert(monkeypatch):
    import core.prediction as pred
    import core.risk_discipline as risk

    inserts = []

    class _Cur:
        def execute(self, sql, params=None):
            if "INSERT INTO predictions" in sql:
                inserts.append(params[0])
            self._row = None

        def fetchone(self):
            return self._row

    class _Conn:
        def cursor(self):
            return _Cur()

    class _Db:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *args):
            return False

    _patch_cycle_inputs(monkeypatch, pred, _cycle_pick())
    monkeypatch.setattr(pred, "db_conn", lambda: _Db())
    monkeypatch.setattr(risk, "strict_issuance_block", lambda cur: {"blocked": False, "reasons": []})
    monkeypatch.setattr(pred, "_symbol_has_open_pick", lambda *a: False)
    monkeypatch.setattr(
        pred,
        "_prepare_checklist_snapshot",
        lambda pick: {"issued_at": pick["predicted_at"], "evidence": {}, "report": {"blocked": True, "block_reason": "veto"}},
    )

    saved, diag = pred.run_prediction_cycle(with_diag=True)

    assert saved == []
    assert inserts == []
    assert diag["checklist_vetoed"] == 1
    assert diag["skip_counts"]["checklist_veto"] == 1


def test_snapshot_failure_rolls_back_prediction_candidate(monkeypatch):
    import core.prediction as pred
    import core.risk_discipline as risk

    sql_log = []

    class _Cur:
        def execute(self, sql, params=None):
            sql_log.append(sql)
            if sql.strip().startswith("INSERT INTO predictions"):
                self._row = (99,)
            elif sql.strip().startswith("SELECT 1 FROM predictions"):
                self._row = None

        def fetchone(self):
            return getattr(self, "_row", None)

    class _Conn:
        def cursor(self):
            return _Cur()

    class _Db:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *args):
            return False

    pick = _cycle_pick()
    _patch_cycle_inputs(monkeypatch, pred, pick)
    monkeypatch.setattr(pred, "db_conn", lambda: _Db())
    monkeypatch.setattr(risk, "strict_issuance_block", lambda cur: {"blocked": False, "reasons": []})
    monkeypatch.setattr(
        pred,
        "_prepare_checklist_snapshot",
        lambda candidate: {"issued_at": candidate["predicted_at"], "evidence": {}, "report": {"blocked": False}},
    )
    monkeypatch.setattr(
        pred,
        "_persist_checklist_snapshot",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ledger unavailable")),
    )

    saved, _ = pred.run_prediction_cycle(with_diag=True)

    assert saved == []
    assert "INSERT INTO predictions" in " ".join(sql_log)
    assert "ROLLBACK TO SAVEPOINT prediction_candidate" in sql_log
    assert "RELEASE SAVEPOINT prediction_candidate" in sql_log
