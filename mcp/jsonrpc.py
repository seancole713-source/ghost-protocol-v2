"""MCP Streamable HTTP JSON-RPC dispatch (read-only tools, Phase 1.5)."""
from __future__ import annotations

import json
import secrets
from typing import Any, Dict, List, Optional, Union

from mcp.ghost_server import invoke_tool, list_tools

JsonRpcMessage = Dict[str, Any]
JsonRpcResponse = Dict[str, Any]

# In-process session ids returned on initialize (Streamable HTTP convention).
_sessions: set[str] = set()


def create_session_id() -> str:
    sid = secrets.token_urlsafe(16)
    _sessions.add(sid)
    return sid


def session_known(session_id: str | None) -> bool:
    if not session_id:
        return False
    return session_id in _sessions


def clear_sessions_for_tests() -> None:
    _sessions.clear()


def _ok(msg_id: Any, result: Any) -> JsonRpcResponse:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id: Any, code: int, message: str) -> JsonRpcResponse:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def dispatch_message(message: JsonRpcMessage) -> Optional[JsonRpcResponse]:
    """Handle one JSON-RPC request. Notifications return None (no response body)."""
    if not isinstance(message, dict):
        return _err(None, -32600, "Invalid Request")

    method = message.get("method")
    if not isinstance(method, str):
        return _err(message.get("id"), -32600, "Invalid Request")

    msg_id = message.get("id")
    is_notification = msg_id is None and method.startswith("notifications/")

    if is_notification:
        if method == "notifications/initialized":
            return None
        return None

    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    if method == "initialize":
        return _ok(
            msg_id,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "ghost-protocol-mcp", "version": "1.5.0"},
            },
        )

    if method == "ping":
        return _ok(msg_id, {})

    if method == "tools/list":
        return _ok(msg_id, {"tools": list_tools()})

    if method == "tools/call":
        name = params.get("name")
        if not name or not isinstance(name, str):
            return _err(msg_id, -32602, "params.name required")
        try:
            content = invoke_tool(name)
            text = json.dumps(content, default=str)
        except KeyError:
            return _err(msg_id, -32602, f"Unknown tool: {name}")
        except Exception as exc:
            return _err(msg_id, -32000, str(exc)[:200])
        return _ok(msg_id, {"content": [{"type": "text", "text": text}], "isError": False})

    return _err(msg_id, -32601, f"Method not found: {method}")


def process_jsonrpc_body(body: Any) -> tuple[Optional[Union[JsonRpcResponse, List[JsonRpcResponse]]], Optional[str]]:
    """Returns (response_payload, new_session_id_if_initialize)."""
    new_session: Optional[str] = None

    if isinstance(body, list):
        responses: List[JsonRpcResponse] = []
        for item in body:
            if not isinstance(item, dict):
                continue
            if item.get("method") == "initialize" and item.get("id") is not None:
                new_session = create_session_id()
            out = dispatch_message(item)
            if out is not None:
                responses.append(out)
        if not responses:
            return None, new_session
        return responses, new_session

    if not isinstance(body, dict):
        return _err(None, -32600, "Invalid Request"), None

    if body.get("method") == "initialize" and body.get("id") is not None:
        new_session = create_session_id()

    out = dispatch_message(body)
    return out, new_session
