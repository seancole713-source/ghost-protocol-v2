"""FastAPI routes for Ghost MCP (Phase 1, read-only)."""
from __future__ import annotations

import json
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from mcp.ghost_server import invoke_tool, list_tools
from mcp.security import require_mcp_auth

router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.get("")
async def mcp_info(request: Request):
    """Discovery — requires auth."""
    require_mcp_auth(request)
    return {
        "ok": True,
        "service": "ghost-protocol-mcp",
        "phase": 1,
        "transport": "http-jsonrpc-and-rest",
        "tools_path": "/mcp/tools/{name}",
        "jsonrpc_path": "/mcp",
    }


@router.get("/tools")
async def mcp_tools_list(request: Request):
    require_mcp_auth(request)
    return {"ok": True, "tools": list_tools()}


@router.get("/tools/{tool_name}")
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


@router.post("")
async def mcp_jsonrpc(request: Request):
    """Minimal MCP JSON-RPC: tools/list and tools/call only."""
    require_mcp_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON object required")

    req_id = body.get("id")
    method = body.get("method")
    params = body.get("params") if isinstance(body.get("params"), dict) else {}

    def _resp(result: Any) -> Dict[str, Any]:
        out: Dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "result": result}
        return out

    def _err(code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    if method == "tools/list":
        return _resp({"tools": list_tools()})

    if method == "tools/call":
        name = params.get("name")
        if not name or not isinstance(name, str):
            return JSONResponse(content=_err(-32602, "params.name required"))
        try:
            content = invoke_tool(name)
            if isinstance(content, JSONResponse):
                content = json.loads(content.body.decode())
            text = json.dumps(content, default=str)
        except KeyError:
            return JSONResponse(content=_err(-32602, f"Unknown tool: {name}"))
        except Exception as exc:
            return JSONResponse(content=_err(-32000, str(exc)[:200]))
        return _resp({"content": [{"type": "text", "text": text}]})

    if method == "initialize":
        return _resp(
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ghost-protocol-mcp", "version": "1.0.0"},
            }
        )

    return JSONResponse(content=_err(-32601, f"Method not found: {method}"))
