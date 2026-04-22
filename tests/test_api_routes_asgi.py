from fastapi.testclient import TestClient

import wolf_app


def _client_with_test_mode(monkeypatch):
    monkeypatch.setenv("GHOST_TEST_MODE", "1")
    return TestClient(wolf_app.APP)


def test_api_health_route_returns_health_payload(monkeypatch):
    monkeypatch.setattr(wolf_app, "health", lambda: {"status": "healthy", "score": 100, "issues": []})
    with _client_with_test_mode(monkeypatch) as client:
        r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert body["score"] == 100


def test_api_coverage_route_success(monkeypatch):
    class FakeCursor:
        def __init__(self):
            self.last_sql = ""

        def execute(self, sql, params=None):
            self.last_sql = sql

        def fetchone(self):
            if "last_coverage_retrain_ts" in self.last_sql:
                return ("970000",)
            return None

    class FakeConn:
        def cursor(self):
            return FakeCursor()

    class FakeDbCtx:
        def __enter__(self):
            return FakeConn()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(wolf_app.time, "time", lambda: 1_000_000)
    monkeypatch.setattr("core.signal_engine.get_model_status", lambda: {"trained": True, "models": 2})
    monkeypatch.setattr(wolf_app, "db_conn", lambda: FakeDbCtx())
    monkeypatch.setenv("MODEL_COVERAGE_MIN_MODELS", "3")

    with _client_with_test_mode(monkeypatch) as client:
        r = client.get("/api/coverage")

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["coverage"]["loaded_models"] == 2
    assert body["coverage"]["below_floor"] is True


def test_api_cockpit_context_route_success(monkeypatch):
    monkeypatch.setattr(
        wolf_app,
        "_cockpit_cached_db_payload",
        lambda: (
            {"ok": True, "total": 10},
            {"ok": True, "by_direction": {"BUY": {"wins": 6, "losses": 4}}},
            {"trained": True, "models": 3},
            {"open_predictions": 1, "resolved_24h": 2, "weekly_outcomes": {"WIN": 2}},
        ),
    )
    monkeypatch.setattr(wolf_app, "health", lambda: {"status": "healthy", "score": 100})
    monkeypatch.setattr("core.prediction._check_regime", lambda: {"block_crypto_buys": False, "reason": "", "btc_24h_pct": 0.0})

    with _client_with_test_mode(monkeypatch) as client:
        r = client.get("/api/cockpit/context")

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["health"]["status"] == "healthy"
    assert body["stats"]["total"] == 10
    assert body["v3"]["models"] == 3
    assert body["activity"]["open_predictions"] == 1


def test_api_cockpit_context_route_error(monkeypatch):
    monkeypatch.setattr(wolf_app, "_cockpit_cached_db_payload", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    with _client_with_test_mode(monkeypatch) as client:
        r = client.get("/api/cockpit/context")

    assert r.status_code == 500
    body = r.json()
    assert body["ok"] is False
    assert "boom" in body["error"]
