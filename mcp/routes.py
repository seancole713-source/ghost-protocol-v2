"""FastAPI routes for Ghost MCP — Streamable HTTP + OAuth lazy auth (Phase 1.6)."""
from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from mcp.ghost_server import invoke_tool, list_tools
from mcp.jsonrpc import process_jsonrpc_body
from mcp.oauth_server import public_base_url, www_authenticate_header
from mcp.security import (
    is_mcp_authenticated,
    jsonrpc_calls_protected_tool,
    require_https,
    require_mcp_auth,
)

router = APIRouter(tags=["mcp"])


async def _read_json_body(request: Request) -> Any:
    try:
        return await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")


async def _handle_streamable_post(
    request: Request,
    path_token: Optional[str],
    *,
    lazy_oauth: bool,
) -> Response:
    require_https(request)
    body = await _read_json_body(request)

    if lazy_oauth:
        authed = is_mcp_authenticated(request, path_token=None)
        if jsonrpc_calls_protected_tool(body) and not authed:
            base = public_base_url(request)
            return JSONResponse(
                status_code=401,
                content={
                    "error": "invalid_token",
                    "error_description": "Authentication required for this tool",
                },
                headers={"WWW-Authenticate": www_authenticate_header(base)},
            )
    else:
        require_mcp_auth(request, path_token=path_token)

    payload, new_session = process_jsonrpc_body(body)

    if payload is None:
        resp = Response(status_code=202, content="")
        if new_session:
            resp.headers["Mcp-Session-Id"] = new_session
        return resp

    resp = JSONResponse(content=payload)
    if new_session:
        resp.headers["Mcp-Session-Id"] = new_session
    return resp


@router.post("/mcp")
async def mcp_post_root(request: Request):
    return await _handle_streamable_post(request, path_token=None, lazy_oauth=True)


@router.post("/mcp/{path_token}")
async def mcp_post_token(path_token: str, request: Request):
    return await _handle_streamable_post(request, path_token=path_token, lazy_oauth=False)


@router.get("/mcp")
async def mcp_get_root(request: Request):
    return {
        "ok": True,
        "service": "ghost-protocol-mcp",
        "phase": "1.6",
        "transport": "streamable-http",
        "oauth": True,
        "jsonrpc_post": "/mcp",
    }


@router.get("/mcp/{path_token}")
async def mcp_get_token(path_token: str, request: Request):
    require_mcp_auth(request, path_token=path_token)
    return {
        "ok": True,
        "service": "ghost-protocol-mcp",
        "phase": "1.6",
        "transport": "streamable-http",
        "jsonrpc_post": f"/mcp/{path_token}",
    }


@router.get("/mcp/tools")
async def mcp_tools_list(request: Request):
    require_mcp_auth(request)
    return {"ok": True, "tools": list_tools()}


@router.get("/mcp/tools/{tool_name}")
async def mcp_tool_call(tool_name: str, request: Request):
    require_mcp_auth(request)
    try:
        payload = invoke_tool(tool_name)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown tool")
    if isinstance(payload, JSONResponse):
        body = payload.body
        try:
            payload = json.loads(body.decode())
        except Exception:
            payload = {"ok": False, "error": "non-json response"}
    return JSONResponse(content=payload)
