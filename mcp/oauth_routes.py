"""OAuth discovery + authorize/token routes for Claude MCP registration."""
from __future__ import annotations

import secrets
import urllib.parse
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from mcp.oauth_server import (
    authorization_server_metadata,
    fetch_cimd_client,
    issue_access_token,
    oauth_configured,
    operator_secret_ok,
    pop_auth_code,
    protected_resource_metadata,
    public_base_url,
    redirect_uri_allowed,
    refresh_token_valid,
    store_auth_code,
    store_refresh_token,
    verify_access_token,
    _pkce_valid,
)

router = APIRouter(tags=["oauth"])


@router.get("/.well-known/oauth-protected-resource")
async def prm_root(request: Request):
    base = public_base_url(request)
    return JSONResponse(content=protected_resource_metadata(base))


@router.get("/.well-known/oauth-protected-resource/mcp")
async def prm_mcp(request: Request):
    base = public_base_url(request)
    return JSONResponse(content=protected_resource_metadata(base))


@router.get("/.well-known/oauth-authorization-server")
async def as_metadata(request: Request):
    base = public_base_url(request)
    return JSONResponse(content=authorization_server_metadata(base))


@router.get("/oauth/authorize")
async def oauth_authorize(
    request: Request,
    response_type: str = "",
    client_id: str = "",
    redirect_uri: str = "",
    scope: str = "",
    state: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "",
):
    if not oauth_configured():
        raise HTTPException(status_code=503, detail="OAuth not configured on server")
    if response_type != "code":
        raise HTTPException(status_code=400, detail="unsupported response_type")
    if code_challenge_method and code_challenge_method != "S256":
        raise HTTPException(status_code=400, detail="unsupported code_challenge_method")
    if not code_challenge:
        raise HTTPException(status_code=400, detail="code_challenge required")

    cimd = fetch_cimd_client(client_id)
    if not cimd:
        raise HTTPException(status_code=400, detail="invalid client_id (CIMD)")
    if not redirect_uri_allowed(redirect_uri, cimd.get("redirect_uris") or []):
        raise HTTPException(status_code=400, detail="invalid redirect_uri")

    client_host = urllib.parse.urlparse(client_id).netloc or "unknown client"
    q = urllib.parse.urlencode({
        "response_type": response_type,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method or "S256",
    })
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Ghost MCP — Authorize</title>
<style>body{{background:#0a0a0a;color:#fff;font-family:-apple-system,BlinkMacSystemFont,sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh}}
.box{{background:#111;border:1px solid #1e1e1e;border-radius:14px;padding:32px;width:380px;max-width:92vw}}
h1{{font-size:18px;margin-bottom:8px}}p{{color:#888;font-size:13px;line-height:1.5;margin-bottom:16px}}
input{{width:100%;background:#0a0a0a;border:1px solid #2a2a2a;color:#fff;padding:11px 12px;
border-radius:8px;font-size:14px;margin-bottom:12px}}
button{{width:100%;background:#7c3aed;color:#fff;border:none;padding:11px;border-radius:8px;
font-weight:700;cursor:pointer}}.err{{color:#ff3b3b;font-size:12px;min-height:16px}}</style></head>
<body><div class="box"><h1>Authorize Ghost MCP</h1>
<p><b>{client_host}</b> is requesting read-only access to Ghost Protocol state via MCP.</p>
<form method="post" action="/oauth/authorize">
<input type="hidden" name="oauth_query" value="{q}">
<input type="password" name="secret" placeholder="Operator secret (CRON_SECRET)" autocomplete="off" autofocus>
<button type="submit">Allow access</button>
<div class="err"></div></form></div></body></html>"""
    return HTMLResponse(html)


@router.post("/oauth/authorize")
async def oauth_authorize_post(
    request: Request,
    oauth_query: str = Form(...),
    secret: str = Form(""),
):
    if not operator_secret_ok(secret):
        raise HTTPException(status_code=401, detail="Invalid operator secret")
    params = dict(urllib.parse.parse_qsl(oauth_query, keep_blank_values=True))
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    scope = params.get("scope", "")
    state = params.get("state", "")
    code_challenge = params.get("code_challenge", "")

    cimd = fetch_cimd_client(client_id)
    if not cimd or not redirect_uri_allowed(redirect_uri, cimd.get("redirect_uris") or []):
        raise HTTPException(status_code=400, detail="invalid client")

    code = secrets.token_urlsafe(32)
    store_auth_code(
        code,
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        scope=scope,
    )
    sep = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{sep}{urllib.parse.urlencode({'code': code, 'state': state})}"
    return RedirectResponse(url=location, status_code=302)


@router.post("/oauth/token")
async def oauth_token(
    request: Request,
    grant_type: str = Form(...),
    code: Optional[str] = Form(None),
    redirect_uri: Optional[str] = Form(None),
    client_id: Optional[str] = Form(None),
    code_verifier: Optional[str] = Form(None),
    refresh_token: Optional[str] = Form(None),
):
    base = public_base_url(request)
    if not oauth_configured():
        raise HTTPException(status_code=503, detail="OAuth not configured")

    if grant_type == "refresh_token":
        if not refresh_token or not refresh_token_valid(refresh_token):
            raise HTTPException(status_code=400, detail="invalid refresh_token")
        try:
            access = issue_access_token(base)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        return JSONResponse(content={
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": refresh_token,
        })

    if grant_type != "authorization_code":
        raise HTTPException(status_code=400, detail="unsupported grant_type")

    stored = pop_auth_code(code or "")
    if not stored:
        raise HTTPException(status_code=400, detail="invalid code")
    if client_id and stored.get("client_id") != client_id:
        raise HTTPException(status_code=400, detail="client_id mismatch")
    if redirect_uri and stored.get("redirect_uri") != redirect_uri:
        raise HTTPException(status_code=400, detail="redirect_uri mismatch")
    if not _pkce_valid(code_verifier or "", stored.get("code_challenge") or ""):
        raise HTTPException(status_code=400, detail="invalid code_verifier")

    try:
        access = issue_access_token(base)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    rt = secrets.token_urlsafe(32)
    store_refresh_token(rt)
    return JSONResponse(content={
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": rt,
        "scope": stored.get("scope") or "",
    })
