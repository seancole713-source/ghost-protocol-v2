"""Read-only Ghost MCP tools.

The HTTP client layer is structurally GET-only: ``GhostMcpGetClient`` exposes
only ``get()``; there are no post/put/delete methods.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, FrozenSet, Mapping

ALLOWED_HTTP_METHOD = "GET"

TOOL_TO_PATH: Mapping[str, str] = {
    "ghost_context": "/api/wolf/ask/context",
    "ghost_score": "/api/wolf/ghost-score",
    "ghost_kill_status": "/api/wolf/kill-status",
    "ghost_gate_status": "/api/wolf/gate-status",
    "ghost_stats_v32": "/api/stats/v32",
    "ghost_portfolio": "/api/portfolio",
    "ghost_picks": "/api/picks",
    "ghost_symbol_universe": "/api/admin/symbol-universe",
    "ghost_shadow_stats": "/api/shadow-stats",
}

ALLOWED_GET_PATHS: FrozenSet[str] = frozenset(TOOL_TO_PATH.values())


class GhostMcpGetClient:
    """In-process GET-only client for allowlisted Ghost API paths."""

    def get(self, path: str) -> Any:
        return self._request(ALLOWED_HTTP_METHOD, path)

    def _request(self, method: str, path: str) -> Any:
        if method != ALLOWED_HTTP_METHOD:
            raise TypeError(
                f"Ghost MCP HTTP client is GET-only; refused {method!r} for {path!r}"
            )
        if path not in ALLOWED_GET_PATHS:
            raise ValueError(f"Path not in MCP allowlist: {path!r}")
        handler = _PATH_HANDLERS.get(path)
        if handler is None:
            raise ValueError(f"No handler registered for {path!r}")
        return handler()


def _handler_ghost_context() -> Dict[str, Any]:
    from core.ghost_ask import build_ask_context

    return {"ok": True, "context": build_ask_context(include_portfolio=True)}


def _handler_ghost_score() -> Dict[str, Any]:
    from api.wolf_endpoints import ghost_score_payload_sync

    return ghost_score_payload_sync()


def _handler_ghost_kill_status() -> Dict[str, Any]:
    import wolf_app

    return wolf_app.wolf_kill_status()


def _handler_ghost_gate_status() -> Dict[str, Any]:
    import wolf_app

    return wolf_app.wolf_gate_status()


def _handler_ghost_stats_v32() -> Dict[str, Any]:
    import wolf_app

    return wolf_app.get_stats_v32()


def _handler_ghost_portfolio() -> Dict[str, Any]:
    from core.portfolio_routes import build_portfolio_payload

    return build_portfolio_payload()


def _handler_ghost_picks() -> Dict[str, Any]:
    import wolf_app

    return wolf_app.get_picks()


def _handler_ghost_symbol_universe() -> Dict[str, Any]:
    import wolf_app

    return wolf_app._build_symbol_universe_payload()


def _handler_ghost_shadow_stats() -> Dict[str, Any]:
    from core.shadow_outcomes import shadow_stats

    return shadow_stats()


_PATH_HANDLERS: Mapping[str, Callable[[], Any]] = {
    "/api/wolf/ask/context": _handler_ghost_context,
    "/api/wolf/ghost-score": _handler_ghost_score,
    "/api/wolf/kill-status": _handler_ghost_kill_status,
    "/api/wolf/gate-status": _handler_ghost_gate_status,
    "/api/stats/v32": _handler_ghost_stats_v32,
    "/api/portfolio": _handler_ghost_portfolio,
    "/api/picks": _handler_ghost_picks,
    "/api/admin/symbol-universe": _handler_ghost_symbol_universe,
    "/api/shadow-stats": _handler_ghost_shadow_stats,
}

_CLIENT = GhostMcpGetClient()


def list_tools() -> list[Dict[str, str]]:
    return [
        {
            "name": name,
            "description": f"GET {path} — read-only Ghost state",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        }
        for name, path in TOOL_TO_PATH.items()
    ]


def invoke_tool(name: str) -> Any:
    if name not in TOOL_TO_PATH:
        raise KeyError(f"Unknown MCP tool: {name!r}")
    return _CLIENT.get(TOOL_TO_PATH[name])
