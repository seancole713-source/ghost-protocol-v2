"""Minimal OAuth 2.1 authorization server for Claude MCP connector (single-user).

Supports CIMD (Client ID Metadata Documents) per Anthropic lazy-auth guidance.
Access tokens are signed JWTs; authorization codes live in ghost_state.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import urllib.parse
from typing import Any, Dict, Optional, Tuple

import requests

LOGGER = logging.getLogger("ghost.mcp.oauth")

SCOPE_GHOST_READ = "ghost:read"
CODE_TTL_S = 300
ACCESS_TTL_S = 3600
REFRESH_TTL_S = 86400 * 30


def public_base_url(request=None) -> str:
    env = os.getenv("GHOST_PUBLIC_URL", "").strip().rstrip("/")
    if env:
        return env
    if request is not None:
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
        host = request.headers.get("host") or request.url.netloc
        if host:
            return f"{proto}://{host}".rstrip("/")
    return "https://ghost-protocol-v2-production.up.railway.app"


def protected_resource_metadata(base: str) -> Dict[str, Any]:
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "scopes_supported": [SCOPE_GHOST_READ],
    }


def authorization_server_metadata(base: str) -> Dict[str, Any]:
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "scopes_supported": [SCOPE_GHOST_READ],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "client_id_metadata_document_supported": True,
    }


def _signing_key() -> bytes:
    secret = (
        os.getenv("GHOST_OAUTH_SIGNING_KEY", "").strip()
        or os.getenv("GHOST_OAUTH_SECRET", "").strip()
    )
    if not secret:
        return b""
    return secret.encode("utf-8")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def issue_access_token(base: str, *, sub: str = "operator") -> str:
    key = _signing_key()
    if not key:
        raise RuntimeError("OAuth signing key not configured")
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": base,
        "sub": sub,
        "aud": f"{base}/mcp",
        "scope": SCOPE_GHOST_READ,
        "iat": now,
        "exp": now + ACCESS_TTL_S,
    }
    h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(key, f"{h}.{p}".encode(), hashlib.sha256).digest()
    return f"{h}.{p}.{_b64url(sig)}"


def verify_access_token(token: str, base: str) -> bool:
    key = _signing_key()
    if not key or not token or token.count(".") != 2:
        return False
    try:
        h, p, s = token.split(".", 2)
        expected = hmac.new(key, f"{h}.{p}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64url(expected), s):
            return False
        payload = json.loads(_b64url_decode(p))
        if payload.get("iss") != base:
            return False
        if payload.get("aud") != f"{base}/mcp":
            return False
        if int(payload.get("exp") or 0) < int(time.time()):
            return False
        scope = str(payload.get("scope") or "")
        if SCOPE_GHOST_READ not in scope.split():
            return False
        return True
    except Exception:
        return False


def _pkce_valid(code_verifier: str, challenge: str) -> bool:
    if not code_verifier or not challenge:
        return False
    digest = hashlib.sha256(code_verifier.encode()).digest()
    computed = _b64url(digest)
    return hmac.compare_digest(computed, challenge)


def _loopback_match(requested: str, allowed: str) -> bool:
    """RFC 8252 loopback redirect comparison (port ignored for 127.0.0.1/localhost)."""
    try:
        r = urllib.parse.urlparse(requested)
        a = urllib.parse.urlparse(allowed)
    except Exception:
        return False
    if r.scheme != a.scheme:
        return False
    if r.scheme not in ("http", "https"):
        return False
    r_host = (r.hostname or "").lower()
    a_host = (a.hostname or "").lower()
    if r_host in ("127.0.0.1", "localhost", "[::1]") and a_host in ("127.0.0.1", "localhost", "[::1]"):
        return r.path == a.path and (r.query or "") == (a.query or "")
    return requested == allowed


def redirect_uri_allowed(requested: str, allowed_list: list) -> bool:
    for allowed in allowed_list or []:
        if not isinstance(allowed, str):
            continue
        if _loopback_match(requested, allowed):
            return True
        if requested == allowed:
            return True
    return False


def fetch_cimd_client(client_id: str) -> Optional[Dict[str, Any]]:
    """Fetch and validate Client ID Metadata Document (HTTPS URL client_id)."""
    if not client_id.startswith("https://"):
        return None
    try:
        resp = requests.get(client_id, timeout=10, headers={"Accept": "application/json"})
        if resp.status_code != 200:
            return None
        doc = resp.json()
        if not isinstance(doc, dict):
            return None
        if doc.get("client_id") != client_id:
            return None
        return doc
    except Exception as exc:
        LOGGER.warning("CIMD fetch failed: %s", str(exc)[:80])
        return None


def _state_key(prefix: str, val: str) -> str:
    return f"oauth_{prefix}_{val}"


def store_auth_code(
    code: str,
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    scope: str,
) -> None:
    from core.db import db_conn

    payload = json.dumps({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "scope": scope,
        "exp": int(time.time()) + CODE_TTL_S,
    })
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
        cur.execute(
            "INSERT INTO ghost_state(key,val) VALUES(%s,%s) "
            "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
            (_state_key("code", code), payload),
        )


def pop_auth_code(code: str) -> Optional[Dict[str, Any]]:
    from core.db import db_conn

    key = _state_key("code", code)
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT val FROM ghost_state WHERE key=%s", (key,))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute("DELETE FROM ghost_state WHERE key=%s", (key,))
    try:
        data = json.loads(row[0])
        if int(data.get("exp") or 0) < int(time.time()):
            return None
        return data
    except Exception:
        return None


def store_refresh_token(token: str) -> None:
    from core.db import db_conn

    exp = int(time.time()) + REFRESH_TTL_S
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
        cur.execute(
            "INSERT INTO ghost_state(key,val) VALUES(%s,%s) "
            "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
            (_state_key("refresh", token), str(exp)),
        )


def refresh_token_valid(token: str) -> bool:
    from core.db import db_conn

    key = _state_key("refresh", token)
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT val FROM ghost_state WHERE key=%s", (key,))
        row = cur.fetchone()
    if not row:
        return False
    try:
        return int(row[0]) >= int(time.time())
    except Exception:
        return False


def operator_secret_ok(provided: str) -> bool:
    """Connector approval — GHOST_OAUTH_SECRET only (not CRON_SECRET / MCP token)."""
    expected = os.getenv("GHOST_OAUTH_SECRET", "").strip()
    if not expected:
        return False
    return hmac.compare_digest((provided or "").encode(), expected.encode())


def oauth_configured() -> bool:
    return bool(_signing_key())


def www_authenticate_header(base: str) -> str:
    meta = f"{base}/.well-known/oauth-protected-resource/mcp"
    return (
        f'Bearer error="invalid_token", '
        f'resource_metadata="{meta}", '
        f'scope="{SCOPE_GHOST_READ}"'
    )
