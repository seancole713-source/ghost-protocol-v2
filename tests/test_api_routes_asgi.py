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


# ── security hardening (audit) ──────────────────────────────────────────

def test_docs_disabled_by_default(monkeypatch):
    """DOCS_ENABLED is unset in CI, so Swagger UI / ReDoc / OpenAPI schema are
    all 404 — destructive admin endpoints aren't browsable or callable."""
    with _client_with_test_mode(monkeypatch) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_diagnostics_404_without_admin_cookie(monkeypatch):
    """/api/diagnostics leaks internals, so it is gated behind the admin cookie
    and returns 404 (undiscoverable) when unauthenticated."""
    monkeypatch.setenv("CRON_SECRET", "testsecret")   # activate the gate
    with _client_with_test_mode(monkeypatch) as client:
        r = client.get("/api/diagnostics")
    assert r.status_code == 404


def test_diagnostics_route_is_registered_but_hidden():
    """The 404 above is the auth wall, not a missing route: the path IS
    registered, yet hidden from the OpenAPI schema (include_in_schema=False)."""
    paths = [getattr(r, "path", None) for r in wolf_app.APP.routes]
    assert "/api/diagnostics" in paths
    diag = next(r for r in wolf_app.APP.routes if getattr(r, "path", None) == "/api/diagnostics")
    assert diag.include_in_schema is False
