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


def test_rate_limit_returns_429_over_limit(monkeypatch):
    """A public /api/ path 429s once an IP exceeds RATE_LIMIT_RPM in the window."""
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("RATE_LIMIT_RPM", "2")
    wolf_app._RL_HITS.clear()
    with _client_with_test_mode(monkeypatch) as client:
        r1 = client.get("/api/coverage")
        r2 = client.get("/api/coverage")
        r3 = client.get("/api/coverage")
    assert r1.status_code != 429
    assert r2.status_code != 429
    assert r3.status_code == 429
    body = r3.json()
    assert body["error"] == "rate_limited"
    assert r3.headers.get("Retry-After") is not None


def test_rate_limit_exempts_health(monkeypatch):
    """/api/health is exempt so uptime monitors are never throttled."""
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("RATE_LIMIT_RPM", "1")
    wolf_app._RL_HITS.clear()
    monkeypatch.setattr(wolf_app, "health", lambda: {"status": "healthy", "score": 100, "issues": []})
    last = None
    with _client_with_test_mode(monkeypatch) as client:
        for _ in range(5):
            last = client.get("/api/health")
    assert last.status_code == 200


def test_rate_limit_disabled_passes_through(monkeypatch):
    """RATE_LIMIT_ENABLED=0 disables throttling entirely."""
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "0")
    monkeypatch.setenv("RATE_LIMIT_RPM", "1")
    wolf_app._RL_HITS.clear()
    with _client_with_test_mode(monkeypatch) as client:
        codes = [client.get("/api/coverage").status_code for _ in range(4)]
    assert all(c != 429 for c in codes)


def test_version_and_seo_routes(monkeypatch):
    """audit v2 #1/#2/#3: /version, /robots.txt, /sitemap.xml exist (were 404)."""
    with _client_with_test_mode(monkeypatch) as client:
        v = client.get("/version")
        assert v.status_code == 200 and v.json().get("app_version")
        r = client.get("/robots.txt")
        assert r.status_code == 200 and "Disallow: /admin" in r.text
        s = client.get("/sitemap.xml")
        assert s.status_code == 200 and "<urlset" in s.text


def test_security_headers_present(monkeypatch):
    """audit v2 #6/#7: every response carries security headers incl. CSP."""
    monkeypatch.setattr(wolf_app, "health", lambda: {"status": "healthy", "score": 100, "issues": []})
    with _client_with_test_mode(monkeypatch) as client:
        r = client.get("/api/health")
    assert "Content-Security-Policy" in r.headers
    assert "cdn.jsdelivr.net" in r.headers["Content-Security-Policy"]
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("Referrer-Policy")
    assert r.headers.get("Permissions-Policy")
    assert r.headers.get("Strict-Transport-Security")


def test_health_public_is_slim(monkeypatch):
    """audit v2 #10: public /health and /api/health expose liveness only."""
    monkeypatch.setattr(wolf_app, "health", lambda: {
        "status": "healthy", "score": 90, "telegram_configured": True,
        "price_feeds": {"x": 1}, "tasks": [1, 2], "confidence_floor": 0.8,
        "dedup_blocked": 3, "predictions_freshness_min": 12})
    with _client_with_test_mode(monkeypatch) as client:
        for path in ("/health", "/api/health"):
            b = client.get(path).json()
            assert b["status"] == "healthy" and b["score"] == 90
            for leaked in ("telegram_configured", "price_feeds", "tasks",
                           "confidence_floor", "dedup_blocked", "predictions_freshness_min"):
                assert leaked not in b, path + " leaked " + leaked


def test_admin_health_gated(monkeypatch):
    """audit v2 #10: full health detail only behind the admin cookie (404 else)."""
    monkeypatch.setenv("CRON_SECRET", "testsecret")
    monkeypatch.setattr(wolf_app, "health", lambda: {
        "status": "healthy", "score": 90, "telegram_configured": True})
    with _client_with_test_mode(monkeypatch) as client:
        assert client.get("/admin/health").status_code == 404
        client.cookies.set(wolf_app._ADMIN_COOKIE, wolf_app._admin_mint_token())
        r = client.get("/admin/health")
    assert r.status_code == 200 and r.json().get("telegram_configured") is True


def test_v1_ghost_score_route_registered():
    """audit v2 #9: /api/v1/ghost-score alias exists (was 404)."""
    paths = [getattr(r, "path", None) for r in wolf_app.APP.routes]
    assert "/api/v1/ghost-score" in paths


def test_diagnostics_route_is_registered_but_hidden():
    """The 404 above is the auth wall, not a missing route: the path IS
    registered, yet hidden from the OpenAPI schema (include_in_schema=False)."""
    paths = [getattr(r, "path", None) for r in wolf_app.APP.routes]
    assert "/api/diagnostics" in paths
    diag = next(r for r in wolf_app.APP.routes if getattr(r, "path", None) == "/api/diagnostics")
    assert diag.include_in_schema is False
