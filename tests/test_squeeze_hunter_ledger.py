"""Tests for core/squeeze_hunter_ledger.py — point-in-time audit trail."""
from core import squeeze_hunter_ledger as hl


class _FakeCursor:
    """Minimal cursor that records executed SQL and returns canned rows."""

    def __init__(self, fetchone_result=None, fetchall_result=None):
        self._fetchone = fetchone_result
        self._fetchall = fetchall_result or []
        self.executed = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.executed.append(sql)

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return self._fetchall


def test_log_hunter_evaluation_inserts(monkeypatch):
    cur = _FakeCursor(fetchone_result=(1,))
    rid = hl.log_hunter_evaluation(
        symbol="HTZ",
        report={"fuel_score": 70.0, "trigger_score": 80.0, "confirmation_score": 60.0,
                "squeeze_pressure_score": 72.0, "pressure_band": "high", "stage": "squeeze",
                "explosion_score": 88.0, "factors": {}, "projection": {}},
        short_ctx={"short_float_pct": 34.0},
        trigger_ctx={"catalyst_score": 90.0},
        confirm_ctx={"breakout_pct": 5.0},
        issued_ts=1000,
        cur=cur,
    )
    assert rid == 1
    assert any("INSERT INTO ghost_squeeze_hunter_evaluations" in s for s in cur.executed)


def test_log_hunter_evaluation_idempotent(monkeypatch):
    # ON CONFLICT DO NOTHING returns no row → None (idempotent, no duplicate).
    cur = _FakeCursor(fetchone_result=None)
    rid = hl.log_hunter_evaluation(
        symbol="HTZ",
        report={"fuel_score": 70.0},
        issued_ts=1000,
        cur=cur,
    )
    assert rid is None


def test_log_hunter_evaluation_never_raises(monkeypatch):
    # A cursor that raises on execute must not propagate.
    class Boom:
        def execute(self, sql, params=None):
            raise RuntimeError("db down")

    rid = hl.log_hunter_evaluation(symbol="HTZ", report={"fuel_score": 1.0}, cur=Boom())
    assert rid is None


def test_resolve_hunter_evaluation_inserts(monkeypatch):
    cur = _FakeCursor()
    cur.rowcount = 1
    inserted = hl.resolve_hunter_evaluation(
        evaluation_id=1,
        return_14d_pct=25.0,
        hit_plus_20=True,
        hit_minus_20=False,
        cur=cur,
    )
    assert inserted is True
    assert any("INSERT INTO ghost_squeeze_hunter_resolutions" in s for s in cur.executed)


def test_resolve_hunter_evaluation_idempotent(monkeypatch):
    cur = _FakeCursor()
    cur.rowcount = 0  # ON CONFLICT DO NOTHING → no row inserted
    inserted = hl.resolve_hunter_evaluation(evaluation_id=1, cur=cur)
    assert inserted is False


def test_scoring_version_is_stable():
    assert hl.HUNTER_SCORING_VERSION == "1"
    assert hl.HUNTER_HORIZONS == (1, 5, 14)
