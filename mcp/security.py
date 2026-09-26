"""GHOST MCP authentication and transport checks."""
from __future__ import annotations

import hmac
import json
import os
from typing import Any, Optional

from fastapi import HTTPException, Request

MCP_TOKEN_HEADER = "x-ghost-mcp-token"


def _expected_token_bytes() -> bytes | None:
    raw = os.getenv("GHOST_MCP_TOKEN", "").strip()
    if not raw:
        return None
    return raw.encode("utf-8")


def extract_bearer(request: Request) -> str:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def extract_mcp_token(request: Request) -> str:
    """Static MCP token from X-Ghost-Mcp-Token or Bearer (if not a JWT)."""
    direct = (request.headers.get(MCP_TOKEN_HEADER) or "").strip()
    if direct:
        return direct
    bearer = extract_bearer(request)
    if bearer and bearer.count(".") != 2:
        return bearer
    return ""


def verify_mcp_token(provided: str) -> bool:
    """Constant-time compare against GHOST_MCP_TOKEN. Fails closed if unset."""
    expected = _expected_token_bytes()
    if expected is None:
        return False
    got = (provided or "").encode("utf-8")
    if not got:
        return False
    return hmac.compare_digest(got, expected)


def verify_mcp_path_token(path_token: str | None) -> bool:
    if not path_token:
        return False
    return verify_mcp_token(path_token.strip())


def verify_oauth_bearer(request: Request) -> bool:
    bearer = extract_bearer(request)
    if not bearer or bearer.count(".") != 2:
        return False
    from mcp.oauth_server import public_base_url, verify_access_token

    return verify_access_token(bearer, public_base_url(request))


def _check_lockout(request: Request) -> str:
    """Refuse (429) a client locked out for wrong credentials; return its key."""
    from shared.request_guard import CREDENTIAL_LOCKOUT, client_ip

    ip = client_ip(request)
    retry = CREDENTIAL_LOCKOUT.retry_after(ip)
    if retry:
        raise HTTPException(
            status_code=429, detail="Too many failed attempts",
            headers={"Retry-After": str(retry)},
        )
    return ip


def is_mcp_authenticated(request: Request, *, path_token: str | None = None) -> bool:
    """True if any credential is valid.

    U18: a WRONG static token (path segment, X-Ghost-Mcp-Token, or non-JWT
    Bearer) counts toward the shared failed-credential lockout, and a locked
    client is refused with 429 before any compare. JWTs are not counted: an
    expired access token is the normal OAuth refresh trigger, and HS256
    signatures are not guessable online.
    """
    ip = _check_lockout(request)
    presented_static = bool((path_token or "").strip()) or bool(extract_mcp_token(request))
    if verify_mcp_path_token(path_token):
        return True
    if verify_mcp_token(extract_mcp_token(request)):
        return True
    if verify_oauth_bearer(request):
        return True
    if presented_static:
        from shared.request_guard import CREDENTIAL_LOCKOUT

        CREDENTIAL_LOCKOUT.record_failure(ip, kind="mcp_token")
    return False


def require_https(request: Request) -> None:
    if os.getenv("GHOST_TEST_MODE", "0").strip().lower() in ("1", "true", "yes", "on"):
        return
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "").lower()
    if proto != "https":
        raise HTTPException(status_code=403, detail="HTTPS required")


def require_mcp_auth(request: Request, *, path_token: str | None = None) -> None:
    """TLS + static MCP token via header OR path segment."""
    require_https(request)
    if is_mcp_authenticated(request, path_token=path_token):
        return
    raise HTTPException(status_code=401, detail="Unauthorized")


def _admin_session_valid(request: Request) -> bool:
    try:
        import wolf_app

        token = request.cookies.get(wolf_app._ADMIN_COOKIE, "")
        return wolf_app._admin_token_valid(token)
    except Exception:
        return False


def require_portfolio_auth(request: Request) -> None:
    require_https(request)
    ip = _check_lockout(request)
    if verify_mcp_token(extract_mcp_token(request)):
        return
    if verify_oauth_bearer(request):
        return
    if _admin_session_valid(request):
        return
    if extract_mcp_token(request):
        from shared.request_guard import CREDENTIAL_LOCKOUT

        CREDENTIAL_LOCKOUT.record_failure(ip, kind="mcp_token")
    raise HTTPException(status_code=401, detail="Unauthorized")


def jsonrpc_calls_protected_tool(body: Any) -> bool:
    """True if body is tools/call (requires auth under lazy OAuth)."""
    messages = body if isinstance(body, list) else [body]
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("method") == "tools/call":
            return True
    return False


def jsonrpc_is_public_method(body: Any) -> bool:
    """Methods Claude may call before OAuth completes."""
    messages = body if isinstance(body, list) else [body]
    public = {
        "initialize",
        "notifications/initialized",
        "tools/list",
        "ping",
    }
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        method = msg.get("method")
        if method and method not in public and not (
            isinstance(method, str) and method.startswith("notifications/")
        ):
            if method == "tools/call":
                continue
            return False
    return True
