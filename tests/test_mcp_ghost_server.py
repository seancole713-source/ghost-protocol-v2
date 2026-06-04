"""Tests for Ghost MCP Phase 1.5 (path token, handshake, portfolio admin)."""
import json

import pytest
from fastapi.testclient import TestClient

import wolf_app
from mcp.ghost_server import ALLOWED_HTTP_METHOD, GhostMcpGetClient
from mcp.jsonrpc import clear_sessions_for_tests
from mcp.security import verify_mcp_path_token, verify_mcp_token


def _client(monkeypatch, token: str = "test-mcp-secret"):
    monkeypatch.setenv("GHOST_TEST_MODE", "1")
    monkeypatch.setenv("GHOST_MCP_TOKEN", token)
    clear_sessions_for_tests()
    return TestClient(wolf_app.APP)


def test_get_only_client_has_no_write_methods():
    client = GhostMcpGetClient()
    assert hasattr(client, "get")
    assert not hasattr(client, "post")
    assert not hasattr(client, "put")
    assert not hasattr(client, "delete")


def test_verify_mcp_path_token(monkeypatch):
    monkeypatch.setenv("GHOST_MCP_TOKEN", "path-secret-xyz")
    assert verify_mcp_path_token("path-secret-xyz") is True
    assert verify_mcp_path_token("wrong") is False
    assert verify_mcp_path_token(None) is False


def test_unauthenticated_mcp_root_401(monkeypatch):
    monkeypatch.setenv("GHOST_MCP_TOKEN", "secret")
    with _client(monkeypatch) as client:
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert r.status_code == 401


def test_wrong_path_token_401(monkeypatch):
    monkeypatch.setenv("GHOST_MCP_TOKEN", "secret")
    with _client(monkeypatch) as client:
        r = client.post(
            "/mcp/wrong-token",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
    assert r.status_code == 401


def test_mcp_handshake_path_token(monkeypatch):
    monkeypatch.setattr(
        "api.wolf_endpoints.ghost_score_payload_sync",
        lambda **kwargs: {"ok": True, "score": 65},
    )
    with _client(monkeypatch, token="secret") as client:
        r1 = client.post(
            "/mcp/secret",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
        )
        assert r1.status_code == 200
        init = r1.json()
        assert init["result"]["protocolVersion"] == "2024-11-05"
        assert "Mcp-Session-Id" in r1.headers

        r2 = client.post(
            "/mcp/secret",
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )
        assert r2.status_code == 202

        r3 = client.post(
            "/mcp/secret",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert r3.status_code == 200
        names = {t["name"] for t in r3.json()["result"]["tools"]}
        assert len(names) == 7

        r4 = client.post(
            "/mcp/secret",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "ghost_score", "arguments": {}},
            },
        )
        assert r4.status_code == 200
        text = r4.json()["result"]["content"][0]["text"]
        assert json.loads(text)["score"] == 65


def test_header_auth_still_works(monkeypatch):
    with _client(monkeypatch, token="secret") as client:
        r = client.post(
            "/mcp",
            headers={"X-Ghost-Mcp-Token": "secret"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
    assert r.status_code == 200


def test_portfolio_admin_cookie_ok(monkeypatch):
    monkeypatch.setenv("GHOST_MCP_TOKEN", "secret")
    monkeypatch.setenv("CRON_SECRET", "cron-test")
    monkeypatch.setattr(
        "core.portfolio_routes.build_portfolio_payload",
        lambda: {"ok": True, "positions": []},
    )
    with _client(monkeypatch) as client:
        client.cookies.set(wolf_app._ADMIN_COOKIE, wolf_app._admin_mint_token())
        r = client.get("/api/portfolio")
    assert r.status_code == 200


def test_portfolio_anonymous_401(monkeypatch):
    monkeypatch.setenv("GHOST_MCP_TOKEN", "secret")
    monkeypatch.setenv("CRON_SECRET", "cron-test")
    with _client(monkeypatch) as client:
        r = client.get("/api/portfolio")
    assert r.status_code == 401


def test_allowed_http_method_is_get_only():
    assert ALLOWED_HTTP_METHOD == "GET"
