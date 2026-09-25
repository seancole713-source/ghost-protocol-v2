"""F28: public /health is a cheap liveness probe; heavy checks are cached.

Railway's healthcheck and every open cockpit poll the health surface. The full
health() probes four market-data providers (check_feeds) and touches circuit
breakers; run per request, one deploy produced ~73 probes in 70 s and tripped
the Alpaca breaker. Public probes must make ZERO provider calls.
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

import wolf_app


class _PingCursor:
    def __init__(self, log):
        self._log = log

    def execute(self, sql, params=None):
        self._log.append(sql)

    def fetchone(self):
        return (1,)


class _PingConn:
    def __init__(self, log, fail=False):
        self._log = log
        self._fail = fail

    def __enter__(self):
        if self._fail:
            raise RuntimeError("db down")
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _PingCursor(self._log)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GHOST_TEST_MODE", "1")
    with TestClient(wolf_app.APP) as c:
        yield c


@pytest.fixture
def db_log(monkeypatch):
    log: list = []
    monkeypatch.setattr(wolf_app, "db_conn", lambda: _PingConn(log))
    return log


def _forbid_outbound(monkeypatch):
    """Count every outbound path a provider probe could take."""
    calls: list = []

    def _count(name):
        def _fn(*a, **k):
            calls.append(name)
            raise AssertionError(f"outbound call from public health: {name}")
        return _fn

    import core.prices
    import requests

    monkeypatch.setattr(core.prices, "check_feeds", _count("check_feeds"))
    monkeypatch.setattr(requests.Session, "request", _count("requests"))
    try:
        import httpx

        monkeypatch.setattr(httpx.Client, "send", _count("httpx"))
        monkeypatch.setattr(httpx.AsyncClient, "send", _count("httpx_async"))
    except ImportError:
        pass
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _count("urllib"))
    monkeypatch.setattr(wolf_app, "health", _count("full_health"))
    return calls


def test_100_public_health_calls_make_zero_provider_calls(monkeypatch, client, db_log):
    calls = _forbid_outbound(monkeypatch)
    for i in range(100):
        path = "/health" if i % 2 == 0 else "/api/health"
        r = client.get(path)
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"
    assert calls == []
    # One pooled SELECT 1 (with a short statement timeout) per probe, nothing else.
    assert db_log.count("SELECT 1") == 100
    assert all(("SELECT 1" in q or "statement_timeout" in q) for q in db_log)


def test_public_health_shape_is_backward_compatible(client, db_log):
    body = client.get("/health").json()
    # Fields existing consumers read (status/score/ts; console reads .ok only).
    assert body["status"] == "healthy" and body["score"] == 100
    assert isinstance(body["ts"], int)
    # Mirrors the scripts/go-no-go.sh public contract: no internal keys.
    assert not {"db", "issues", "warnings", "price_feeds", "tasks"} & set(body)
    assert set(body["scheduler"]) >= {"running", "tasks", "last_tick_ago_s"}
    assert "leader" in body


def test_public_health_reports_db_down_without_failing_the_probe(monkeypatch, client):
    monkeypatch.setattr(wolf_app, "db_conn", lambda: _PingConn([], fail=True))
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded" and body["score"] == 80


def test_public_health_folds_in_cached_full_status_without_internals(monkeypatch, client, db_log):
    monkeypatch.setattr(wolf_app, "health", lambda: {
        "status": "critical", "score": 35, "price_feeds": {"summary": "0/2"},
        "issues": ["x"], "telegram_configured": False})
    wolf_app.health_cached()
    body = client.get("/health").json()
    assert body["status"] == "critical" and body["score"] == 35
    assert body["detail_age_s"] is not None
    assert "price_feeds" not in body and "issues" not in body


def test_health_cached_serves_one_full_run_per_ttl(monkeypatch):
    runs = []
    monkeypatch.setattr(wolf_app, "health", lambda: runs.append(1) or {"status": "healthy", "score": 100})
    for _ in range(50):
        wolf_app.health_cached()
    assert len(runs) == 1
    # Expire the cache: the next call recomputes once.
    wolf_app._HEALTH_FULL_CACHE["t"] = time.time() - 61
    wolf_app.health_cached()
    assert len(runs) == 2


def test_health_cached_is_single_flight(monkeypatch):
    runs = []
    gate = threading.Event()

    def slow_health():
        runs.append(1)
        gate.wait(2)
        return {"status": "healthy", "score": 100}

    monkeypatch.setattr(wolf_app, "health", slow_health)
    threads = [threading.Thread(target=wolf_app.health_cached) for _ in range(8)]
    for t in threads:
        t.start()
    time.sleep(0.05)
    gate.set()
    for t in threads:
        t.join(5)
    assert len(runs) == 1


def test_admin_health_serves_cached_full_detail(monkeypatch, client):
    monkeypatch.setenv("CRON_SECRET", "testsecret")
    runs = []
    monkeypatch.setattr(wolf_app, "health", lambda: runs.append(1) or {
        "status": "healthy", "score": 95, "price_feeds": {"summary": "2/2"}})
    assert client.get("/admin/health").status_code == 404
    assert runs == []  # unauthenticated requests never run the heavy check
    client.cookies.set(wolf_app._ADMIN_COOKIE, wolf_app._admin_mint_token())
    for _ in range(5):
        r = client.get("/admin/health")
        assert r.status_code == 200
        assert r.json()["price_feeds"]["summary"] == "2/2"
    assert runs == [1]


def test_cockpit_context_uses_cached_full_health(monkeypatch):
    runs = []
    monkeypatch.setattr(wolf_app, "health", lambda: runs.append(1) or {"status": "healthy", "score": 100})
    monkeypatch.setattr(
        wolf_app, "_cockpit_cached_db_payload",
        lambda: ({"wins": 1, "losses": 0}, {}, {"trained": False}, {}),
    )
    for _ in range(3):
        out = wolf_app.cockpit_context()
        assert out["health"]["status"] == "healthy"
    assert runs == [1]


def test_full_health_dedup_expiry_write_is_leader_only():
    import inspect

    src = inspect.getsource(wolf_app.health)
    assert "if dedup_blocked and _hl_is_leader():" in src
