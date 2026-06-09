"""Tests for Ghost MCP Phase 1.6 (OAuth lazy auth, path token, portfolio admin)."""
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
    monkeypatch.setenv("GHOST_OAUTH_SECRET", "oauth-test-secret")
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


def test_unauthenticated_mcp_root_initialize_ok(monkeypatch):
    monkeypatch.setenv("GHOST_MCP_TOKEN", "secret")
    with _client(monkeypatch) as client:
        r = client.post(
            "/mcp",
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
    assert r.status_code == 200
    assert r.json()["result"]["protocolVersion"] == "2024-11-05"


def test_unauthenticated_tools_call_401_with_www_authenticate(monkeypatch):
    monkeypatch.setenv("GHOST_MCP_TOKEN", "secret")
    with _client(monkeypatch) as client:
        r = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "ghost_score", "arguments": {}},
            },
        )
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers
    assert "oauth-protected-resource" in r.headers["WWW-Authenticate"]


def test_oauth_discovery_endpoints(monkeypatch):
    with _client(monkeypatch) as client:
        prm = client.get("/.well-known/oauth-protected-resource/mcp")
        assert prm.status_code == 200
        assert prm.json()["resource"].endswith("/mcp")
        assert prm.json()["authorization_servers"]

        asm = client.get("/.well-known/oauth-authorization-server")
        assert asm.status_code == 200
        assert asm.json()["authorization_endpoint"].endswith("/oauth/authorize")
        assert asm.json()["client_id_metadata_document_supported"] is True


def test_oauth_token_form_body(monkeypatch):
    monkeypatch.setenv("GHOST_PUBLIC_URL", "https://ghost.test")
    from mcp.oauth_server import verify_access_token, _b64url
    import hashlib

    verifier = "test-verifier-12345"
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    monkeypatch.setattr(
        "mcp.oauth_routes.pop_auth_code",
        lambda code: {
            "client_id": "https://claude.ai/client",
            "redirect_uri": "http://127.0.0.1/cb",
            "code_challenge": challenge,
            "scope": "ghost:read",
            "exp": 9999999999,
        }
        if code == "code-abc"
        else None,
    )
    monkeypatch.setattr("mcp.oauth_routes.store_refresh_token", lambda _t: None)
    with _client(monkeypatch) as client:
        r = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": "code-abc",
                "redirect_uri": "http://127.0.0.1/cb",
                "client_id": "https://claude.ai/client",
                "code_verifier": verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    assert r.status_code == 200
    assert r.json()["token_type"] == "Bearer"
    assert verify_access_token(r.json()["access_token"], "https://ghost.test")


def test_oauth_bearer_tools_call(monkeypatch):
    monkeypatch.setenv("GHOST_PUBLIC_URL", "https://ghost.test")
    monkeypatch.setenv("GHOST_OAUTH_SECRET", "oauth-test-secret")
    monkeypatch.setattr(
        "api.wolf_endpoints.ghost_score_payload_sync",
        lambda **kwargs: {"ok": True, "score": 42},
    )
    from mcp.oauth_server import issue_access_token

    token = issue_access_token("https://ghost.test")
    with _client(monkeypatch) as client:
        r = client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "ghost_score", "arguments": {}},
            },
        )
    assert r.status_code == 200
    text = r.json()["result"]["content"][0]["text"]
    assert json.loads(text)["score"] == 42


def test_operator_secret_does_not_accept_cron_secret(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-test")
    monkeypatch.delenv("GHOST_OAUTH_SECRET", raising=False)
    from mcp.oauth_server import operator_secret_ok

    assert operator_secret_ok("cron-test") is False


def test_operator_secret_accepts_oauth_secret(monkeypatch):
    monkeypatch.setenv("GHOST_OAUTH_SECRET", "oauth-only")
    from mcp.oauth_server import operator_secret_ok

    assert operator_secret_ok("oauth-only") is True
    assert operator_secret_ok("wrong") is False


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
        assert len(names) == 9
        assert "ghost_shadow_stats" in names

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


def test_portfolio_write_requires_auth(monkeypatch):
    monkeypatch.setenv("GHOST_MCP_TOKEN", "secret")
    monkeypatch.setenv("CRON_SECRET", "cron-test")
    with _client(monkeypatch) as client:
        r = client.post(
            "/api/portfolio",
            json={"symbol": "AAPL", "asset_type": "stock", "quantity": 1, "buy_price": 1.0},
        )
    assert r.status_code == 401


def test_portfolio_delete_requires_auth(monkeypatch):
    monkeypatch.setenv("GHOST_MCP_TOKEN", "secret")
    monkeypatch.setenv("CRON_SECRET", "cron-test")
    with _client(monkeypatch) as client:
        r = client.delete("/api/portfolio/123")
    assert r.status_code == 401


def test_allowed_http_method_is_get_only():
    assert ALLOWED_HTTP_METHOD == "GET"
