from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes_data import router
from core.research_forecasts import research_forecasts


def mock_db(monkeypatch):
    queries = []

    class Cursor:
        def execute(self, query, params):
            assert query.lstrip().startswith("SELECT")
            queries.append((query, params))

        def fetchone(self):
            return (2, 1)

        def fetchall(self):
            return [(123, "AAA", "2026-09-15", 200, "UP", 100, 104, 97, 500,
                     .8, 5, "a" * 64, "v3_research_tier")]

    @contextmanager
    def connect():
        class Connection:
            def cursor(self):
                return Cursor()
        yield Connection()

    monkeypatch.setattr("core.db.db_conn", connect)
    return queries


def test_preserves_issued_references_and_separates_overdue(monkeypatch):
    queries = mock_db(monkeypatch)
    out = research_forecasts(limit=9999, now=300)
    assert out["total_open"] == 2
    assert out["awaiting_resolution"] == 1
    assert out["has_more"] is True
    row = out["forecasts"][0]
    assert row["id"] == 123
    assert row["issued_at"] == 200  # reading never changes issuance time
    assert row["entry_reference"] == 100
    assert row["trading_eligible"] is False
    assert out["accuracy_proven"] is False
    assert "expires_at > %s" in queries[1][0]
    assert queries[1][1] == (300, 200)


def test_public_forecast_route_uses_real_reader(monkeypatch):
    mock_db(monkeypatch)
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get("/api/forecasts/research?limit=1")
    assert response.status_code == 200
    assert response.json()["forecasts"][0]["id"] == 123
    assert response.json()["trading_eligible"] is False


def test_db_failure_is_not_an_empty_success(monkeypatch):
    def unavailable():
        raise RuntimeError("private connection details")

    monkeypatch.setattr("core.db.db_conn", unavailable)
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get("/api/forecasts/research")
    assert response.status_code == 503
    assert response.json()["ok"] is False
    assert "private" not in response.text
