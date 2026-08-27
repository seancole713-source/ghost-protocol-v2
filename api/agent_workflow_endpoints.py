"""Authenticated REST surface for the advisory Ghost-agent workflow."""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from core.agent_workflow import AgentWorkflowError
from mcp.security import require_mcp_auth

LOGGER = logging.getLogger("ghost.agent_workflow_endpoints")

router = APIRouter(prefix="/api/agent-workflow", tags=["agent-workflow"])


def _protected(request: Request) -> None:
    require_mcp_auth(request)


def _payload_error(exc: AgentWorkflowError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/health")
async def agent_workflow_health(request: Request):
    _protected(request)
    try:
        from core.agent_workflow import workflow_health

        return JSONResponse(content=workflow_health())
    except Exception:
        LOGGER.exception("agent workflow health failed")
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": "agent_workflow_unavailable"},
        )


@router.get("/tasks")
async def agent_workflow_tasks(
    request: Request,
    status: str = Query(default="PENDING"),
    task_type: str = Query(default=""),
    symbol: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
):
    _protected(request)
    try:
        from core.agent_workflow import list_tasks

        return JSONResponse(
            content=list_tasks(
                status=status or None,
                task_type=task_type or None,
                symbol=symbol or None,
                limit=limit,
            )
        )
    except AgentWorkflowError as exc:
        raise _payload_error(exc)
    except Exception:
        LOGGER.exception("agent workflow task listing failed")
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": "agent_workflow_unavailable"},
        )


@router.get("/tasks/{task_id}")
async def agent_workflow_task(task_id: str, request: Request):
    _protected(request)
    try:
        from core.agent_workflow import get_task

        result = get_task(task_id)
        return JSONResponse(status_code=200 if result.get("ok") else 404, content=result)
    except AgentWorkflowError as exc:
        raise _payload_error(exc)
    except Exception:
        LOGGER.exception("agent workflow task read failed task_id=%s", task_id)
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": "agent_workflow_unavailable"},
        )


@router.post("/tasks")
async def agent_workflow_create_task(payload: Dict[str, Any], request: Request):
    """Protected operator/Ghost hook. Connected agents normally claim, not create."""
    _protected(request)
    try:
        from core.agent_workflow import create_task

        result = create_task(
            task_type=payload.get("task_type", ""),
            requested_by=payload.get("requested_by", "ghost.operator"),
            request_payload=payload.get("request_payload", {}),
            symbol=payload.get("symbol"),
            priority=payload.get("priority", 50),
            available_at=payload.get("available_at"),
            deadline_at=payload.get("deadline_at"),
            required_response_schema=payload.get("required_response_schema"),
            required_submissions=payload.get("required_submissions", 1),
            max_attempts=payload.get("max_attempts", 3),
            idempotency_key=payload.get("idempotency_key"),
        )
        return JSONResponse(status_code=201 if result.get("created") else 200, content=result)
    except AgentWorkflowError as exc:
        raise _payload_error(exc)
    except Exception:
        LOGGER.exception("agent workflow task creation failed")
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": "agent_workflow_unavailable"},
        )


@router.post("/claim")
async def agent_workflow_claim(payload: Dict[str, Any], request: Request):
    _protected(request)
    try:
        from core.agent_workflow import claim_task

        return JSONResponse(
            content=claim_task(
                agent_id=payload.get("agent_id", ""),
                lease_seconds=payload.get("lease_seconds", 600),
                task_types=payload.get("task_types"),
            )
        )
    except AgentWorkflowError as exc:
        raise _payload_error(exc)
    except Exception:
        LOGGER.exception("agent workflow claim failed")
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": "agent_workflow_unavailable"},
        )


@router.post("/tasks/{task_id}/heartbeat")
async def agent_workflow_heartbeat(task_id: str, payload: Dict[str, Any], request: Request):
    _protected(request)
    try:
        from core.agent_workflow import heartbeat_task

        return JSONResponse(
            content=heartbeat_task(
                task_id=task_id,
                agent_id=payload.get("agent_id", ""),
                lease_token=payload.get("lease_token", ""),
                lease_seconds=payload.get("lease_seconds", 600),
            )
        )
    except AgentWorkflowError as exc:
        raise _payload_error(exc)
    except Exception:
        LOGGER.exception("agent workflow heartbeat failed task_id=%s", task_id)
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": "agent_workflow_unavailable"},
        )


@router.post("/tasks/{task_id}/evidence")
async def agent_workflow_submit(task_id: str, payload: Dict[str, Any], request: Request):
    _protected(request)
    try:
        from core.agent_workflow import submit_evidence

        result = submit_evidence(
            task_id=task_id,
            agent_id=payload.get("agent_id", ""),
            lease_token=payload.get("lease_token", ""),
            agent_provider=payload.get("agent_provider", ""),
            model_name=payload.get("model_name", ""),
            prompt_version=payload.get("prompt_version", ""),
            summary=payload.get("summary", ""),
            claims=payload.get("claims", {}),
            source_refs=payload.get("source_refs", []),
            agent_confidence=payload.get("agent_confidence"),
            raw_response=payload.get("raw_response"),
            repair_of_evidence_id=payload.get("repair_of_evidence_id"),
        )
        return JSONResponse(status_code=200 if result.get("accepted") else 202, content=result)
    except AgentWorkflowError as exc:
        raise _payload_error(exc)
    except Exception:
        LOGGER.exception("agent workflow evidence submission failed task_id=%s", task_id)
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": "agent_workflow_unavailable"},
        )


@router.post("/workers/heartbeat")
async def agent_workflow_worker_heartbeat(payload: Dict[str, Any], request: Request):
    _protected(request)
    try:
        from core.agent_workflow import heartbeat_worker

        return JSONResponse(
            content=heartbeat_worker(
                agent_id=payload.get("agent_id", ""),
                agent_provider=payload.get("agent_provider", ""),
                model_name=payload.get("model_name", ""),
                status=payload.get("status", ""),
                current_task_id=payload.get("current_task_id"),
                processed_delta=payload.get("processed_delta", 0),
                accepted_delta=payload.get("accepted_delta", 0),
                quarantined_delta=payload.get("quarantined_delta", 0),
                last_error=payload.get("last_error"),
                metadata=payload.get("metadata"),
            )
        )
    except AgentWorkflowError as exc:
        raise _payload_error(exc)
    except Exception:
        LOGGER.exception("agent workflow worker heartbeat failed")
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": "agent_workflow_unavailable"},
        )


@router.post("/tasks/{task_id}/release")
async def agent_workflow_release(task_id: str, payload: Dict[str, Any], request: Request):
    _protected(request)
    try:
        from core.agent_workflow import release_task

        return JSONResponse(
            content=release_task(
                task_id=task_id,
                agent_id=payload.get("agent_id", ""),
                lease_token=payload.get("lease_token", ""),
                reason=payload.get("reason", "agent_released"),
            )
        )
    except AgentWorkflowError as exc:
        raise _payload_error(exc)
    except Exception:
        LOGGER.exception("agent workflow release failed task_id=%s", task_id)
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": "agent_workflow_unavailable"},
        )
