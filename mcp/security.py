"""GHOST MCP authentication and transport checks.

GHOST_MCP_TOKEN is read from the environment only. It must never be logged,
printed, or given a default value in source code.
"""
from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request

MCP_TOKEN_HEADER = "x-ghost-mcp-token"


def _expected_token_bytes() -> bytes | None:
    raw = os.getenv("GHOST_MCP_TOKEN", "").strip()
    if not raw:
        return None
    return raw.encode("utf-8")


def extract_mcp_token(request: Request) -> str:
    """Token from X-Ghost-Mcp-Token or Authorization: Bearer …"""
    direct = (request.headers.get(MCP_TOKEN_HEADER) or "").strip()
    if direct:
        return direct
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def verify_mcp_token(provided: str) -> bool:
    """Constant-time compare against Railway env token. Fails closed if unset."""
    expected = _expected_token_bytes()
    if expected is None:
        return False
    got = (provided or "").encode("utf-8")
    if not got:
        return False
    return hmac.compare_digest(got, expected)


def verify_mcp_path_token(path_token: str | None) -> bool:
    """Verify URL path segment against GHOST_MCP_TOKEN (token-in-path for claude.ai)."""
    if not path_token:
        return False
    return verify_mcp_token(path_token.strip())


def require_https(request: Request) -> None:
    """Reject non-TLS requests (Railway public traffic uses X-Forwarded-Proto)."""
    if os.getenv("GHOST_TEST_MODE", "0").strip().lower() in ("1", "true", "yes", "on"):
        return
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "").lower()
    if proto != "https":
        raise HTTPException(status_code=403, detail="HTTPS required")


def require_mcp_auth(request: Request, *, path_token: str | None = None) -> None:
    """TLS + MCP token via header OR path segment (either passes)."""
    require_https(request)
    header_ok = verify_mcp_token(extract_mcp_token(request))
    path_ok = verify_mcp_path_token(path_token)
    if header_ok or path_ok:
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
    """Portfolio: MCP token (header) OR valid /admin session cookie; anonymous → 401."""
    require_https(request)
    if verify_mcp_token(extract_mcp_token(request)):
        return
    if _admin_session_valid(request):
        return
    raise HTTPException(status_code=401, detail="Unauthorized")
