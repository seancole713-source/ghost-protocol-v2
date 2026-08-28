"""Ghost MCP research and operations tools.

The HTTP client layer is structurally GET-only: ``GhostMcpGetClient`` exposes
only ``get()``; there are no post/put/delete methods.

Phase 2 adds research-platform tools with typed input schemas and argument
support so agents can inspect contracts, artifacts, proof status, activation
history, and platform health.

The agent-workflow tools are the only scoped mutations: connected agents may
claim durable advisory tasks, renew leases, and submit source-backed evidence.
They cannot issue predictions, change gates, clear pauses, or place orders.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, FrozenSet, Mapping, Optional

ALLOWED_HTTP_METHOD = "GET"

# ── Phase 1: operational tools (parameterless) ─────────────────────────────

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

# ── Phase 2: research platform tools (with typed arguments) ────────────────

RESEARCH_TOOLS: Mapping[str, Dict[str, Any]] = {
    "ghost_research_contracts": {
        "description": "List all registered research contracts with their proof targets, output domains, and lifecycle status.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "ghost_research_contract": {
        "description": "Get one research contract by name and version, including full outcome domain, allowed sources, and proof configuration.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Contract name (e.g. tp_sl_swing)"},
                "version": {"type": "string", "description": "Contract version (e.g. v1)", "default": "v1"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    "ghost_research_artifacts": {
        "description": "List research artifacts (trained model packages), optionally filtered by contract_id and status. Each artifact includes its SHA-256 identity, training manifest, calibration proof, and gate proof.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "contract_id": {"type": "string", "description": "Filter by contract ID", "default": ""},
                "status": {"type": "string", "description": "Filter by status (ACTIVE, SUPERSEDED, RETIRED)", "default": "ACTIVE"},
                "limit": {"type": "integer", "description": "Max results", "default": 50},
            },
            "additionalProperties": False,
        },
    },
    "ghost_research_artifact": {
        "description": "Get one research artifact by its SHA-256 hash, including full lifecycle event history.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifact_sha": {"type": "string", "description": "SHA-256 hash of the artifact"},
            },
            "required": ["artifact_sha"],
            "additionalProperties": False,
        },
    },
    "ghost_research_proof": {
        "description": "Get the forward proof status for a contract/artifact pair. Returns the fixed-50 confirmatory protocol results: actionable predictions, wins, losses, Wilson lower bound, secondary gate status, and whether the 70% precision threshold is met.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "contract_id": {"type": "string", "description": "Contract ID (e.g. tp_sl_swing_v1)"},
                "artifact_sha": {"type": "string", "description": "SHA-256 hash of the artifact"},
            },
            "required": ["contract_id", "artifact_sha"],
            "additionalProperties": False,
        },
    },
    "ghost_research_predictions": {
        "description": "List research predictions from the ledger, optionally filtered by contract, artifact, and resolution status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "contract_id": {"type": "string", "description": "Filter by contract ID", "default": ""},
                "artifact_sha": {"type": "string", "description": "Filter by artifact SHA", "default": ""},
                "resolved": {"type": "boolean", "description": "Show resolved (true) or pending (false)", "default": True},
                "limit": {"type": "integer", "description": "Max results", "default": 100},
            },
            "additionalProperties": False,
        },
    },
    "ghost_research_activation_history": {
        "description": "Get activation event history — when artifacts were activated, rolled back, or superseded. Optionally filtered by symbol and direction.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Filter by symbol (e.g. WOLF)", "default": ""},
                "direction": {"type": "string", "description": "Filter by direction (UP/DOWN)", "default": ""},
                "limit": {"type": "integer", "description": "Max results", "default": 50},
            },
            "additionalProperties": False,
        },
    },
    "ghost_research_evidence_lease": {
        "description": "Get the current evidence lease status for an artifact — whether old proven research can temporarily serve predictions while a new model is being validated.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifact_sha": {"type": "string", "description": "SHA-256 hash of the artifact"},
                "symbol": {"type": "string", "description": "Symbol (e.g. WOLF)", "default": "WOLF"},
                "direction": {"type": "string", "description": "Direction (UP/DOWN)", "default": "UP"},
            },
            "required": ["artifact_sha"],
            "additionalProperties": False,
        },
    },
    "ghost_research_health": {
        "description": "Research platform health check — verifies all research tables exist, reports pending/invalid prediction rates, and lease state.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "ghost_research_status": {
        "description": "Current research mode status — whether research picks are enabled, how many have been resolved, daily cap, stall status, and confidence floor.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
}

# ── Two-way advisory agent workflow ────────────────────────────────────────

AGENT_WORKFLOW_TOOLS: Mapping[str, Dict[str, Any]] = {
    "ghost_agent_tasks": {
        "description": "List durable advisory research tasks waiting for connected agents. Agent evidence is never directly trade-eligible.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "default": "PENDING"},
                "task_type": {"type": "string", "default": ""},
                "symbol": {"type": "string", "default": ""},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
            },
            "additionalProperties": False,
        },
    },
    "ghost_agent_task": {
        "description": "Read one agent task with its append-only event history and submitted evidence.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    "ghost_agent_claim_task": {
        "description": "Claim the highest-priority advisory task. The response includes the exact submission schema, a valid example, repair rules, and a time-limited lease.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Stable connected-agent identity"},
                "lease_seconds": {"type": "integer", "default": 600, "minimum": 60, "maximum": 3600},
                "task_types": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["agent_id"],
            "additionalProperties": False,
        },
    },
    "ghost_agent_heartbeat": {
        "description": "Renew an active advisory task lease while research is in progress.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "lease_token": {"type": "string"},
                "lease_seconds": {"type": "integer", "default": 600, "minimum": 60, "maximum": 3600},
            },
            "required": ["task_id", "agent_id", "lease_token"],
            "additionalProperties": False,
        },
    },
    "ghost_agent_submit_evidence": {
        "description": "Submit structured, source-backed advisory evidence. Repairable failures retain the lease and return categorized machine-readable corrections; evidence can never fire a prediction or trade.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "lease_token": {"type": "string"},
                "agent_provider": {"type": "string", "description": "e.g. anthropic or openai"},
                "model_name": {"type": "string"},
                "prompt_version": {"type": "string"},
                "summary": {"type": "string"},
                "claims": {"type": "object"},
                "source_refs": {"type": "array", "items": {"type": "object"}},
                "agent_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "raw_response": {},
                "repair_of_evidence_id": {
                    "type": "string",
                    "description": "Quarantined evidence ID being corrected during the active lease",
                },
            },
            "required": [
                "task_id", "agent_id", "lease_token", "agent_provider",
                "model_name", "prompt_version", "summary", "claims", "source_refs"
            ],
            "additionalProperties": False,
        },
    },
    "ghost_agent_release_task": {
        "description": "Release a claimed advisory task without submitting evidence.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "lease_token": {"type": "string"},
                "reason": {"type": "string", "default": "agent_released"},
            },
            "required": ["task_id", "agent_id", "lease_token"],
            "additionalProperties": False,
        },
    },
    "ghost_agent_workflow_health": {
        "description": "Inspect queue, lease, evidence-validation, and dead-letter health for the advisory agent workflow.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
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


# ── Phase 2: research tool handlers ────────────────────────────────────────

def _research_contracts(_args: Dict[str, Any]) -> Dict[str, Any]:
    from core.research_contracts import list_contracts as _list
    contracts = _list()
    return {
        "ok": True,
        "contracts": [
            {
                "name": c.name,
                "version": c.version,
                "contract_id": c.contract_id(),
                "description": c.description,
                "output_domain": sorted(c.output_domain),
                "horizon_bars": c.horizon_bars,
                "live_eligible": c.live_eligible,
                "lifecycle": c.lifecycle,
                "resolver_id": c.resolver_id,
                "proof": {
                    "target_wilson_low": c.proof.target_wilson_low,
                    "min_support": c.proof.min_support,
                    "precision_applicable": c.proof.precision_applicable,
                },
            }
            for c in contracts
        ],
    }


def _research_contract(args: Dict[str, Any]) -> Dict[str, Any]:
    from core.research_contracts import get_contract
    name = args["name"]
    version = args.get("version", "v1")
    c = get_contract(name, version)
    if not c:
        return {"ok": False, "error": f"Contract not found: {name}/{version}"}
    return {
        "ok": True,
        "contract": {
            "name": c.name,
            "version": c.version,
            "contract_id": c.contract_id(),
            "description": c.description,
            "output_domain": sorted(c.output_domain),
            "horizon_bars": c.horizon_bars,
            "live_eligible": c.live_eligible,
            "lifecycle": c.lifecycle,
            "resolver_id": c.resolver_id,
            "proof": {
                "target_wilson_low": c.proof.target_wilson_low,
                "min_support": c.proof.min_support,
                "precision_applicable": c.proof.precision_applicable,
            },
        },
    }


def _research_artifacts(args: Dict[str, Any]) -> Dict[str, Any]:
    from core.research_artifacts import list_artifacts
    contract_id = args.get("contract_id") or None
    status = args.get("status", "ACTIVE")
    limit = int(args.get("limit", 50))
    artifacts = list_artifacts(contract_id=contract_id, status=status)
    return {
        "ok": True,
        "artifacts": [
            {k: v for k, v in a.items() if k != "payload_bytes"}
            for a in artifacts[:limit]
        ],
        "count": min(len(artifacts), limit),
        "total": len(artifacts),
    }


def _research_artifact(args: Dict[str, Any]) -> Dict[str, Any]:
    from core.research_artifacts import get_artifact, get_lifecycle_events
    artifact_sha = args["artifact_sha"]
    artifact = get_artifact(artifact_sha)
    if not artifact:
        return {"ok": False, "error": "Artifact not found"}
    events = get_lifecycle_events(artifact_sha)
    return {
        "ok": True,
        "artifact": {k: v for k, v in artifact.items() if k != "payload_bytes"},
        "lifecycle_events": events,
    }


def _research_proof(args: Dict[str, Any]) -> Dict[str, Any]:
    from core.research_forward import get_active_registrations, evaluate_forward_proof
    from core.binomial_stats import V2_CONFIRMATORY_N, V2_MIN_WINS
    contract_id = args["contract_id"]
    artifact_sha = args["artifact_sha"]
    registrations = get_active_registrations(status=None)
    matching_reg = None
    for reg in registrations:
        if reg.get("contract_id") == contract_id and reg.get("artifact_sha") == artifact_sha:
            matching_reg = reg
            break
    if not matching_reg:
        return {
            "ok": True,
            "proof": None,
            "note": "No forward registration found for this contract/artifact pair. Register a forward experiment first.",
            "threshold": {
                "confirmatory_n": V2_CONFIRMATORY_N,
                "min_wins": V2_MIN_WINS,
                "target_wilson_low": 0.70,
            },
        }
    proof = evaluate_forward_proof(matching_reg["registration_id"])
    return {
        "ok": True,
        "proof": proof,
        "threshold": {
            "confirmatory_n": V2_CONFIRMATORY_N,
            "min_wins": V2_MIN_WINS,
            "target_wilson_low": 0.70,
        },
    }


def _research_predictions(args: Dict[str, Any]) -> Dict[str, Any]:
    from core.research_ledger import get_resolved_predictions, get_pending_predictions
    contract_id = args.get("contract_id") or None
    artifact_sha = args.get("artifact_sha") or None
    resolved = args.get("resolved", True)
    limit = int(args.get("limit", 100))
    if resolved:
        rows = get_resolved_predictions(
            contract_id=contract_id, artifact_sha=artifact_sha, limit=limit,
        )
    else:
        rows = get_pending_predictions(contract_id=contract_id, limit=limit)
    return {"ok": True, "predictions": rows, "count": len(rows), "resolved": resolved}


def _research_activation_history(args: Dict[str, Any]) -> Dict[str, Any]:
    from core.research_activation import get_activation_history
    symbol = args.get("symbol") or None
    direction = args.get("direction") or None
    limit = int(args.get("limit", 50))
    history = get_activation_history(symbol=symbol, direction=direction, limit=limit)
    return {"ok": True, "events": history, "count": len(history)}


def _research_evidence_lease(args: Dict[str, Any]) -> Dict[str, Any]:
    from core.research_activation import compute_evidence_lease
    artifact_sha = args["artifact_sha"]
    symbol = args.get("symbol", "WOLF").upper()
    direction = args.get("direction", "UP").upper()
    lease = compute_evidence_lease(
        artifact_sha=artifact_sha, symbol=symbol, direction=direction,
    )
    return {"ok": True, "lease": lease}


def _research_health(_args: Dict[str, Any]) -> Dict[str, Any]:
    from core.db import db_conn
    health: Dict[str, Any] = {"ok": False, "tables": {}, "stats": {}}
    all_tables_present = True
    with db_conn() as conn:
        cur = conn.cursor()
        for table in (
            "ghost_research_artifacts",
            "ghost_research_predictions",
            "ghost_research_resolutions",
            "ghost_research_registrations",
            "ghost_research_activation_log",
        ):
            cur.execute(
                "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name=%s)",
                (table,),
            )
            row = cur.fetchone()
            present = bool(row and row[0])
            health["tables"][table] = present
            if not present:
                all_tables_present = False
        try:
            cur.execute(
                "SELECT COUNT(*) FROM ghost_research_predictions p "
                "LEFT JOIN ghost_research_resolutions r ON r.prediction_id = p.id "
                "WHERE r.prediction_id IS NULL"
            )
            row = cur.fetchone()
            health["stats"]["pending_predictions"] = int(row[0]) if row else 0
        except Exception:
            health["stats"]["pending_predictions"] = "table_not_ready"
            all_tables_present = False
        try:
            cur.execute("SELECT COUNT(*) FROM ghost_research_resolutions")
            row = cur.fetchone()
            health["stats"]["total_resolved"] = int(row[0]) if row else 0
        except Exception:
            health["stats"]["total_resolved"] = "table_not_ready"
        try:
            cur.execute(
                "SELECT COUNT(*) FROM ghost_research_artifacts WHERE status='ACTIVE'"
            )
            row = cur.fetchone()
            health["stats"]["active_artifacts"] = int(row[0]) if row else 0
        except Exception:
            health["stats"]["active_artifacts"] = "table_not_ready"
    health["ok"] = all_tables_present
    return health


def _research_status(_args: Dict[str, Any]) -> Dict[str, Any]:
    from core.prediction import research_mode_state
    state = research_mode_state()
    return {"ok": True, **state}


_RESEARCH_HANDLERS: Mapping[str, Callable[[Dict[str, Any]], Any]] = {
    "ghost_research_contracts": _research_contracts,
    "ghost_research_contract": _research_contract,
    "ghost_research_artifacts": _research_artifacts,
    "ghost_research_artifact": _research_artifact,
    "ghost_research_proof": _research_proof,
    "ghost_research_predictions": _research_predictions,
    "ghost_research_activation_history": _research_activation_history,
    "ghost_research_evidence_lease": _research_evidence_lease,
    "ghost_research_health": _research_health,
    "ghost_research_status": _research_status,
}

# ── Market data: live per-symbol price (any symbol, not just WOLF) ────────
#
# ghost_score is Ghost's own single-symbol cockpit read — parameterless,
# hardcoded to WOLF. It is not a per-symbol lookup and never was. This tool
# fills that gap: a raw live-price read for any symbol, official-watchlist
# or not, reusing the same Alpaca-first/yfinance-fallback extended-session
# pricing Ghost's own premarket scans use internally. It returns a price and
# a session label, never a score or a signal — model coverage stays gated by
# ghost_symbol_universe/ghost_score exactly as before.

MARKET_DATA_TOOLS: Mapping[str, Dict[str, Any]] = {
    "ghost_symbol_quote": {
        "description": (
            "Live price quote for any symbol, not limited to WOLF or the official "
            "watchlist. Alpaca live trade first, yfinance premarket/after-hours "
            "fallback — the same feed Ghost's own premarket scans use. Returns "
            "session label (premarket/rth/afterhours/closed), live price, previous "
            "close, and gap %. This is a raw price read, not a model score."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Ticker symbol, e.g. PYPL"},
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
    },
}


def _symbol_quote(args: Dict[str, Any]) -> Dict[str, Any]:
    from core.prices import get_extended_session
    from config.symbols import V3_WHITELIST_STOCKS

    symbol = str(args.get("symbol") or "").strip().upper()
    if not symbol:
        return {"ok": False, "error": "symbol is required"}
    quote = get_extended_session(symbol)
    if not quote:
        return {"ok": False, "symbol": symbol, "error": "no_price_available"}
    return {
        "ok": True,
        **quote,
        "in_official_watchlist": symbol in V3_WHITELIST_STOCKS,
    }


_MARKET_DATA_HANDLERS: Mapping[str, Callable[[Dict[str, Any]], Any]] = {
    "ghost_symbol_quote": _symbol_quote,
}


def _agent_workflow_call(function_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Invoke one bounded workflow operation with stable MCP error payloads."""
    from core import agent_workflow

    function = getattr(agent_workflow, function_name)
    try:
        return function(**arguments)
    except agent_workflow.AgentWorkflowError as exc:
        return {"ok": False, "error": "invalid_agent_workflow_request", "detail": str(exc)}


def _agent_tasks(args: Dict[str, Any]) -> Dict[str, Any]:
    return _agent_workflow_call(
        "list_tasks",
        {
            "status": args.get("status", "PENDING") or None,
            "task_type": args.get("task_type") or None,
            "symbol": args.get("symbol") or None,
            "limit": int(args.get("limit", 50)),
        },
    )


def _agent_task(args: Dict[str, Any]) -> Dict[str, Any]:
    return _agent_workflow_call("get_task", {"task_id": args.get("task_id", "")})


def _agent_claim_task(args: Dict[str, Any]) -> Dict[str, Any]:
    return _agent_workflow_call(
        "claim_task",
        {
            "agent_id": args.get("agent_id", ""),
            "lease_seconds": int(args.get("lease_seconds", 600)),
            "task_types": args.get("task_types"),
        },
    )


def _agent_heartbeat(args: Dict[str, Any]) -> Dict[str, Any]:
    return _agent_workflow_call(
        "heartbeat_task",
        {
            "task_id": args.get("task_id", ""),
            "agent_id": args.get("agent_id", ""),
            "lease_token": args.get("lease_token", ""),
            "lease_seconds": int(args.get("lease_seconds", 600)),
        },
    )


def _agent_submit_evidence(args: Dict[str, Any]) -> Dict[str, Any]:
    return _agent_workflow_call(
        "submit_evidence",
        {
            "task_id": args.get("task_id", ""),
            "agent_id": args.get("agent_id", ""),
            "lease_token": args.get("lease_token", ""),
            "agent_provider": args.get("agent_provider", ""),
            "model_name": args.get("model_name", ""),
            "prompt_version": args.get("prompt_version", ""),
            "summary": args.get("summary", ""),
            "claims": args.get("claims", {}),
            "source_refs": args.get("source_refs", []),
            "agent_confidence": args.get("agent_confidence"),
            "raw_response": args.get("raw_response"),
            "repair_of_evidence_id": args.get("repair_of_evidence_id"),
        },
    )


def _agent_release_task(args: Dict[str, Any]) -> Dict[str, Any]:
    return _agent_workflow_call(
        "release_task",
        {
            "task_id": args.get("task_id", ""),
            "agent_id": args.get("agent_id", ""),
            "lease_token": args.get("lease_token", ""),
            "reason": args.get("reason", "agent_released"),
        },
    )


def _agent_workflow_health(_args: Dict[str, Any]) -> Dict[str, Any]:
    return _agent_workflow_call("workflow_health", {})


_AGENT_WORKFLOW_HANDLERS: Mapping[str, Callable[[Dict[str, Any]], Any]] = {
    "ghost_agent_tasks": _agent_tasks,
    "ghost_agent_task": _agent_task,
    "ghost_agent_claim_task": _agent_claim_task,
    "ghost_agent_heartbeat": _agent_heartbeat,
    "ghost_agent_submit_evidence": _agent_submit_evidence,
    "ghost_agent_release_task": _agent_release_task,
    "ghost_agent_workflow_health": _agent_workflow_health,
}


# ── tool listing & invocation ───────────────────────────────────────────────


def list_tools() -> list[Dict[str, Any]]:
    tools: list[Dict[str, Any]] = []
    for name, path in TOOL_TO_PATH.items():
        tools.append({
            "name": name,
            "description": f"GET {path} — read-only Ghost state",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        })
    for name, meta in RESEARCH_TOOLS.items():
        tools.append({
            "name": name,
            "description": meta["description"],
            "inputSchema": meta["inputSchema"],
        })
    for name, meta in MARKET_DATA_TOOLS.items():
        tools.append({
            "name": name,
            "description": meta["description"],
            "inputSchema": meta["inputSchema"],
        })
    for name, meta in AGENT_WORKFLOW_TOOLS.items():
        tools.append({
            "name": name,
            "description": meta["description"],
            "inputSchema": meta["inputSchema"],
        })
    return tools


def invoke_tool(name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
    if name in TOOL_TO_PATH:
        return _CLIENT.get(TOOL_TO_PATH[name])
    if name in _RESEARCH_HANDLERS:
        return _RESEARCH_HANDLERS[name](arguments or {})
    if name in _MARKET_DATA_HANDLERS:
        return _MARKET_DATA_HANDLERS[name](arguments or {})
    if name in _AGENT_WORKFLOW_HANDLERS:
        return _AGENT_WORKFLOW_HANDLERS[name](arguments or {})
    raise KeyError(f"Unknown MCP tool: {name!r}")
