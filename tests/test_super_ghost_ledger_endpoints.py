"""Endpoint tests for the Super Ghost ledger routes (PR #84).

These avoid the app lifespan (no DB) by using TestClient directly and mocking
the ledger functions, mirroring the existing test_super_ghost.py endpoint test.
"""
from fastapi.testclient import TestClient

import wolf_app


def _client():
    return TestClient(wolf_app.APP)


def test_super_ghost_log_param_triggers_ledger(monkeypatch):
    from api import wolf_endpoints

    wolf_endpoints._CACHE.clear()
    calls = {}

    def fake_build(symbol, ai=False):
        return {"ok": True, "symbol": symbol, "engine": "test", "prediction": {"direction": "UP"}, "checklist": []}

    def fake_log(report):
        calls["logged"] = report.get("symbol")
        return 42

    monkeypatch.setattr("core.super_ghost.build_super_ghost", fake_build)
    monkeypatch.setattr("core.super_ghost_ledger.log_prediction", fake_log)
    r = _client().get("/api/wolf/super-ghost?symbol=WOLF&log=1")
    assert r.status_code == 200
    body = r.json()
    assert body["ledger_id"] == 42
    assert calls["logged"] == "WOLF"


def test_super_ghost_no_log_param_does_not_log(monkeypatch):
    from api import wolf_endpoints

    wolf_endpoints._CACHE.clear()
    calls = {"logged": False}

    monkeypatch.setattr("core.super_ghost.build_super_ghost",
                        lambda symbol, ai=False: {"ok": True, "symbol": symbol, "prediction": {"direction": "UP"}, "checklist": []})
    monkeypatch.setattr("core.super_ghost_ledger.log_prediction",
                        lambda report: calls.__setitem__("logged", True))
    r = _client().get("/api/wolf/super-ghost?symbol=WOLF")
    assert r.status_code == 200
    assert "ledger_id" not in r.json()
    assert calls["logged"] is False


def test_post_super_ghost_log_endpoint(monkeypatch):
    monkeypatch.setattr("core.super_ghost.build_super_ghost",
                        lambda symbol: {"ok": True, "symbol": symbol, "prediction": {"direction": "UP"}, "checklist": []})
    monkeypatch.setattr("core.super_ghost_ledger.log_prediction", lambda report: 7)
    r = _client().post("/api/wolf/super-ghost/log", json={"symbol": "ABCD"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["ledger_id"] == 7
    assert body["symbol"] == "ABCD"


def test_post_super_ghost_log_rejects_bad_build(monkeypatch):
    monkeypatch.setattr("core.super_ghost.build_super_ghost",
                        lambda symbol: {"ok": False, "error": "boom"})
    r = _client().post("/api/wolf/super-ghost/log", json={"symbol": "ZZZ"})
    assert r.status_code == 400


def test_history_endpoint(monkeypatch):
    monkeypatch.setattr("core.super_ghost_ledger.get_history",
                        lambda symbol=None, limit=100, include_payload=False: {"ok": True, "rows": [{"id": 1}], "count": 1})
    r = _client().get("/api/wolf/super-ghost/history?symbol=WOLF&limit=5")
    assert r.status_code == 200
    assert r.json()["count"] == 1


def test_accuracy_endpoint(monkeypatch):
    monkeypatch.setattr("core.super_ghost_ledger.get_accuracy",
                        lambda symbol=None, horizon=5: {"ok": True, "overall": {"n": 3, "win_rate": 0.66}})
    r = _client().get("/api/wolf/super-ghost/accuracy?horizon=5")
    assert r.status_code == 200
    assert r.json()["overall"]["n"] == 3


def test_if_followed_endpoint(monkeypatch):
    monkeypatch.setattr("core.super_ghost_ledger.get_if_followed",
                        lambda symbol=None, horizon=5: {"ok": True, "profit_factor": 2.1})
    r = _client().get("/api/wolf/super-ghost/if-followed")
    assert r.status_code == 200
    assert r.json()["profit_factor"] == 2.1


def test_resolve_endpoint_requires_auth(monkeypatch):
    # Production sets CRON_SECRET which disables dev-mode admin bypass.
    monkeypatch.setenv("CRON_SECRET", "prod-like")
    monkeypatch.setenv("GHOST_MCP_TOKEN", "secret")
    monkeypatch.setenv("GHOST_TEST_MODE", "1")  # bypass HTTPS
    r = _client().post("/api/wolf/super-ghost/resolve")
    assert r.status_code == 401


def test_resolve_endpoint_runs_with_token(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "prod-like")
    monkeypatch.setenv("GHOST_MCP_TOKEN", "secret")
    monkeypatch.setenv("GHOST_TEST_MODE", "1")
    monkeypatch.setattr("core.super_ghost_ledger.resolve_predictions",
                        lambda limit=500: {"ok": True, "updated": 3})
    r = _client().post("/api/wolf/super-ghost/resolve", headers={"X-Ghost-Mcp-Token": "secret"})
    assert r.status_code == 200
    assert r.json()["updated"] == 3
