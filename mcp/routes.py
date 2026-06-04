"""FastAPI routes for Ghost MCP — Streamable HTTP + token-in-path (Phase 1.5)."""
from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from mcp.ghost_server import invoke_tool, list_tools
from mcp.jsonrpc import process_jsonrpc_body
from mcp.security import require_mcp_auth

router = APIRouter(tags=["mcp"])


async def _read_json_body(request: Request) -> Any:
    try:
        return await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")


async def _handle_streamable_post(request: Request, path_token: Optional[str]) -> Response:
    require_mcp_auth(request, path_token=path_token)
    body = await _read_json_body(request)
    payload, new_session = process_jsonrpc_body(body)

    # Notification-only (e.g. notifications/initialized) → 202 Accepted
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
    return await _handle_streamable_post(request, path_token=None)


@router.post("/mcp/{path_token}")
async def mcp_post_token(path_token: str, request: Request):
    return await _handle_streamable_post(request, path_token=path_token)


@router.get("/mcp")
async def mcp_get_root(request: Request):
    require_mcp_auth(request, path_token=None)
    return {
        "ok": True,
        "service": "ghost-protocol-mcp",
        "phase": "1.5",
        "transport": "streamable-http",
        "jsonrpc_post": "/mcp",
    }


@router.get("/mcp/{path_token}")
async def mcp_get_token(path_token: str, request: Request):
    require_mcp_auth(request, path_token=path_token)
    return {
        "ok": True,
        "service": "ghost-protocol-mcp",
        "phase": "1.5",
        "transport": "streamable-http",
        "jsonrpc_post": f"/mcp/{path_token}",
    }


# Legacy REST tool routes (header auth or /mcp/{token} prefix via separate mount — header/path on POST only)
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
