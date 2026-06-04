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


def require_https(request: Request) -> None:
    """Reject non-TLS requests (Railway public traffic uses X-Forwarded-Proto)."""
    if os.getenv("GHOST_TEST_MODE", "0").strip().lower() in ("1", "true", "yes", "on"):
        return
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "").lower()
    if proto != "https":
        raise HTTPException(status_code=403, detail="HTTPS required")


def require_mcp_auth(request: Request) -> None:
    """TLS + token required before any sensitive read."""
    require_https(request)
    if not verify_mcp_token(extract_mcp_token(request)):
        raise HTTPException(status_code=401, detail="Unauthorized")
