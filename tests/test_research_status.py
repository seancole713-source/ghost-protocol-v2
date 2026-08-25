"""Canonical research-status behavior across API, MCP, and DB failures."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient


class _Cursor:
    def __init__(self, values):
        self.values = iter(values)
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        return None

    def fetchone(self):
        return (next(self.values),)


class _Connection:
    def __init__(self, values):
        self._cursor = _Cursor(values)

    def cursor(self):
        return self._cursor


class _Context:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, tb):
        return False


def test_research_mode_state_uses_consistent_counts(monkeypatch):
    import core.prediction as prediction

    context = _Context(_Connection([4, 2, 0, 0]))
    monkeypatch.setattr(prediction, "db_conn", lambda: context)

    state = prediction.research_mode_state(now_ts=2_000_000)

    assert state["resolved_picks"] == 4
    assert state["research_today"] == 2
    assert state["active_picks"] == 0
    assert state["recent_fires_24h"] == 0
    assert state["research_active"] is True
    assert state["research_reason"] == "cold_start"
    resolved_sql = context.connection._cursor.statements[0][0]
    assert "outcome IN ('WIN','LOSS')" in resolved_sql
    assert "outcome IS NOT NULL" not in resolved_sql


def test_research_status_api_returns_503_on_database_failure(monkeypatch):
    from api.routes_data import router

    monkeypatch.setattr(
        "core.prediction.research_mode_state",
        lambda: (_ for _ in ()).throw(RuntimeError("database secret")),
    )
    app = FastAPI()
    app.include_router(router)

    with TestClient(app) as client:
        response = client.get("/api/research/status")

    assert response.status_code == 503
    assert response.json() == {"ok": False, "error": "research_status_unavailable"}
    assert "secret" not in response.text


def test_research_status_api_delegates_to_canonical_state(monkeypatch):
    from api.routes_data import router

    state = {"research_enabled": True, "research_active": False,
             "resolved_picks": 20}
    monkeypatch.setattr("core.prediction.research_mode_state", lambda: state)
    app = FastAPI()
    app.include_router(router)

    with TestClient(app) as client:
        response = client.get("/api/research/status")

    assert response.status_code == 200
    assert response.json() == {"ok": True, **state}
