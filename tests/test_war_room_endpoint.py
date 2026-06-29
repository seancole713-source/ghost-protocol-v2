"""Tests for the War Room endpoint (POST /api/wolf/war-room).

WHY THIS TEST EXISTS
--------------------
``core/war_room.py`` shipped with ``run_war_room()`` but no route was ever
registered, so production returned ``404 Not Found`` for the documented
``POST /api/wolf/war-room`` endpoint while PROJECT_STATE claimed it existed.
These tests pin the route's existence, its auth gating, and its success/empty
payload contract (Claude is mocked — no network).
"""
import core.war_room as war_room
import wolf_app


def _client(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("GHOST_TEST_MODE", "1")  # bypass HTTPS requirement
    return TestClient(wolf_app.APP)


def test_war_room_route_registered():
    paths = {getattr(r, "path", "") for r in wolf_app.APP.routes}
    assert "/api/wolf/war-room" in paths
    methods = set()
    for r in wolf_app.APP.routes:
        if getattr(r, "path", "") == "/api/wolf/war-room":
            methods |= set(getattr(r, "methods", set()) or set())
    assert "POST" in methods


def test_war_room_requires_auth(monkeypatch):
    # Production sets CRON_SECRET, which disables the dev-mode admin bypass.
    monkeypatch.setenv("CRON_SECRET", "prod-like-secret")
    monkeypatch.setenv("GHOST_MCP_TOKEN", "secret")
    with _client(monkeypatch) as client:
        r = client.post("/api/wolf/war-room", json={"symbol": "WOLF"})
    assert r.status_code == 401


def test_war_room_rejects_bad_token(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "prod-like-secret")
    monkeypatch.setenv("GHOST_MCP_TOKEN", "secret")
    with _client(monkeypatch) as client:
        r = client.post(
            "/api/wolf/war-room",
            json={"symbol": "WOLF"},
            headers={"X-Ghost-Mcp-Token": "wrong"},
        )
    assert r.status_code == 401


def test_war_room_authed_requires_symbol(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "prod-like-secret")
    monkeypatch.setenv("GHOST_MCP_TOKEN", "secret")
    with _client(monkeypatch) as client:
        r = client.post(
            "/api/wolf/war-room",
            json={},
            headers={"X-Ghost-Mcp-Token": "secret"},
        )
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert "symbol" in body["error"].lower()


def test_war_room_success_mock_claude(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "prod-like-secret")
    monkeypatch.setenv("GHOST_MCP_TOKEN", "secret")
    monkeypatch.setattr(war_room, "ANTHROPIC_KEY", "test-key")
    monkeypatch.setattr(war_room, "_check_daily_limit", lambda: None)

    class FakeResp:
        status_code = 200

        def json(self):
            return {"content": [{"type": "text", "text": "ANALYST: business overview ..."}]}

    monkeypatch.setattr(war_room.requests, "post", lambda *a, **k: FakeResp())

    with _client(monkeypatch) as client:
        r = client.post(
            "/api/wolf/war-room",
            json={"symbol": "wolf"},
            headers={"X-Ghost-Mcp-Token": "secret"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["symbol"] == "WOLF"  # normalized to upper
    assert "ANALYST" in body["analysis"]
