"""Tests for Ghost MCP Phase 1 (read-only, token auth, GET-only)."""
import json
import os

import pytest
from fastapi.testclient import TestClient

import wolf_app
from mcp.ghost_server import ALLOWED_HTTP_METHOD, GhostMcpGetClient, invoke_tool
from mcp.security import verify_mcp_token


def _client(monkeypatch, token: str = "test-mcp-secret"):
    monkeypatch.setenv("GHOST_TEST_MODE", "1")
    monkeypatch.setenv("GHOST_MCP_TOKEN", token)
    return TestClient(wolf_app.APP)


def test_get_only_client_rejects_post():
    client = GhostMcpGetClient()
    with pytest.raises(TypeError, match="GET-only"):
        client.post("/api/picks")


def test_get_only_client_rejects_delete():
    client = GhostMcpGetClient()
    with pytest.raises(TypeError, match="GET-only"):
        client.delete("/api/portfolio")


def test_verify_mcp_token_constant_time(monkeypatch):
    monkeypatch.setenv("GHOST_MCP_TOKEN", "abc123")
    assert verify_mcp_token("abc123") is True
    assert verify_mcp_token("wrong") is False
    assert verify_mcp_token("") is False


def test_verify_mcp_token_fails_closed_when_unset(monkeypatch):
    monkeypatch.delenv("GHOST_MCP_TOKEN", raising=False)
    assert verify_mcp_token("anything") is False


def test_mcp_tool_without_token_rejected(monkeypatch):
    monkeypatch.setenv("GHOST_MCP_TOKEN", "secret")
    with _client(monkeypatch) as client:
        r = client.get("/mcp/tools/ghost_score")
    assert r.status_code == 401


def test_mcp_tool_with_token_ok(monkeypatch):
    monkeypatch.setenv("GHOST_MCP_TOKEN", "secret")
    monkeypatch.setattr(
        "api.wolf_endpoints.ghost_score_payload_sync",
        lambda **kwargs: {"ok": True, "score": 65},
    )
    with _client(monkeypatch, token="secret") as client:
        r = client.get("/mcp/tools/ghost_score", headers={"X-Ghost-Mcp-Token": "secret"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["score"] == 65


def test_mcp_jsonrpc_tools_list(monkeypatch):
    monkeypatch.setenv("GHOST_MCP_TOKEN", "secret")
    with _client(monkeypatch, token="secret") as client:
        r = client.post(
            "/mcp",
            headers={"X-Ghost-Mcp-Token": "secret"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
    assert r.status_code == 200
    tools = r.json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert "ghost_context" in names
    assert "ghost_score" in names


def test_ask_context_unauthenticated_blocked(monkeypatch):
    monkeypatch.setenv("GHOST_MCP_TOKEN", "secret")
    with _client(monkeypatch, token="secret") as client:
        r = client.get("/api/wolf/ask/context")
    assert r.status_code == 401


def test_portfolio_unauthenticated_blocked(monkeypatch):
    monkeypatch.setenv("GHOST_MCP_TOKEN", "secret")
    with _client(monkeypatch, token="secret") as client:
        r = client.get("/api/portfolio")
    assert r.status_code == 401


def test_portfolio_with_mcp_token_ok(monkeypatch):
    monkeypatch.setenv("GHOST_MCP_TOKEN", "secret")
    monkeypatch.setattr(
        "core.portfolio_routes.build_portfolio_payload",
        lambda: {"ok": True, "positions": []},
    )
    with _client(monkeypatch, token="secret") as client:
        r = client.get("/api/portfolio", headers={"X-Ghost-Mcp-Token": "secret"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_require_https_rejects_http_when_not_test_mode(monkeypatch):
    from fastapi import HTTPException
    from mcp.security import require_https
    from starlette.requests import Request

    monkeypatch.delenv("GHOST_TEST_MODE", raising=False)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": "GET",
        "path": "/mcp/tools/ghost_score",
        "raw_path": b"/mcp/tools/ghost_score",
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }
    request = Request(scope)
    with pytest.raises(HTTPException) as exc:
        require_https(request)
    assert exc.value.status_code == 403
    assert exc.value.detail == "HTTPS required"


def test_allowed_http_method_is_get_only():
    assert ALLOWED_HTTP_METHOD == "GET"
