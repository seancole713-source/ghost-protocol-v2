import json

from fastapi.responses import JSONResponse

import wolf_app


class FakeCursor:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.last_sql = ""
        self.executed = []

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.executed.append((sql, params))

    def fetchone(self):
        if "last_coverage_retrain_ts" in self.last_sql:
            return self.rows[0] if self.rows else None
        return None

    def fetchall(self):
        return []


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class FakeDbCtx:
    def __init__(self, cursor):
        self._conn = FakeConn(cursor)

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        return False


def test_coverage_status_reports_floor_and_eligibility(monkeypatch):
    monkeypatch.setenv("AUTO_COVERAGE_RETRAIN_ENABLED", "1")
    monkeypatch.setenv("MODEL_COVERAGE_MIN_MODELS", "3")
    monkeypatch.setenv("COVERAGE_RETRAIN_COOLDOWN_SEC", "21600")
    monkeypatch.setenv("COVERAGE_CHECK_INTERVAL_SEC", "3600")
    monkeypatch.setattr(wolf_app.time, "time", lambda: 1_000_000)
    monkeypatch.setattr("core.signal_engine.get_model_status", lambda: {"trained": True, "models": 2, "symbols": {}})
    monkeypatch.setattr(wolf_app, "db_conn", lambda: FakeDbCtx(FakeCursor(rows=[("970000",)])))
    monkeypatch.setattr(wolf_app, "_COVERAGE_RETRAIN_RUNNING", False)

    out = wolf_app.coverage_status()

    assert out["ok"] is True
    assert out["coverage"]["loaded_models"] == 2
    assert out["coverage"]["below_floor"] is True
    assert out["maintenance"]["cooldown_remaining_s"] == 0
    assert out["maintenance"]["eligible_now"] is True
    assert out["maintenance"]["last_retrain_ts"] == 970000


def test_coverage_status_handles_model_status_error(monkeypatch):
    monkeypatch.setattr(wolf_app.time, "time", lambda: 1_000_000)
    monkeypatch.setattr("core.signal_engine.get_model_status", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(wolf_app, "db_conn", lambda: FakeDbCtx(FakeCursor(rows=[])))

    out = wolf_app.coverage_status()

    assert out["model_status"]["trained"] is False
    assert "status_error: boom" in out["model_status"]["reason"]
    assert out["coverage"]["loaded_models"] == 0


def test_cockpit_context_success_path(monkeypatch):
    monkeypatch.setattr(
        wolf_app,
        "_cockpit_cached_db_payload",
        lambda: (
            {"ok": True, "total": 1},
            {"ok": True, "by_direction": {"BUY": {"wins": 1, "losses": 0}}},
            {"trained": True, "models": 3},
            {"open_predictions": 0},
        ),
    )
    monkeypatch.setattr(wolf_app, "health", lambda: {"status": "healthy", "score": 100})
    monkeypatch.setattr("core.prediction._check_regime", lambda: {"block_crypto_buys": False, "reason": "", "btc_24h_pct": 0.0})

    out = wolf_app.cockpit_context()

    assert out["ok"] is True
    assert out["health"]["status"] == "healthy"
    assert out["stats"]["total"] == 1
    assert out["v3"]["models"] == 3
    assert out["regime"]["ok"] is True
    assert out["activity"]["open_predictions"] == 0


def test_cockpit_context_returns_json_error_response(monkeypatch):
    monkeypatch.setattr(wolf_app, "_cockpit_cached_db_payload", lambda: (_ for _ in ()).throw(RuntimeError("db down")))

    out = wolf_app.cockpit_context()

    assert isinstance(out, JSONResponse)
    assert out.status_code == 500
    body = out.body.tobytes() if isinstance(out.body, memoryview) else out.body
    payload = json.loads(body.decode("utf-8"))
    assert payload["ok"] is False
    assert "db down" in payload["error"]
