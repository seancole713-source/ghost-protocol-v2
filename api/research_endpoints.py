"""api/research_endpoints.py — read-only research platform API (Phase 7).

All routes are GET-only and mounted under /api/research. They expose contracts,
artifacts, ledger predictions/resolutions, forward registrations/proof, selector
decisions, promotion status, activation history, evidence leases, and platform
health. No mutation endpoints — scheduled/orchestration code owns writes.

Every response follows the {ok, error, ...} convention. Research-only tasks
carry explicit notes that they are not trading signals.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

LOGGER = logging.getLogger("ghost.research_endpoints")

router = APIRouter(prefix="/api/research", tags=["research"])


# ── contracts ───────────────────────────────────────────────────────────────

@router.get("/contracts")
async def list_research_contracts():
    """All registered research contracts with their specifications."""
    try:
        from core.research_contracts import list_contracts as _list
        contracts = _list()
        return JSONResponse(content={
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
            "note": "Research contracts define prediction tasks. Only tp_sl_swing may become live-eligible.",
        })
    except Exception as e:
        return JSONResponse(content={"ok": False, "error": str(e)[:200]})


@router.get("/contracts/{name}/{version}")
async def get_research_contract(name: str, version: str = "v1"):
    """One contract by name and version."""
    try:
        from core.research_contracts import get_contract
        c = get_contract(name, version)
        if not c:
            return JSONResponse(content={"ok": False, "error": f"Contract {name}/{version} not found"})
        return JSONResponse(content={
            "ok": True,
            "contract": {
                "name": c.name,
                "version": c.version,
                "contract_id": c.contract_id(),
                "description": c.description,
                "output_domain": sorted(c.output_domain),
                "outcome_domain": {
                    "terminal_outcomes": sorted(c.outcome_domain.terminal_outcomes),
                    "invalid_outcome": c.outcome_domain.invalid_outcome,
                    "expired_is_non_win": c.outcome_domain.expired_is_non_win,
                },
                "horizon_bars": c.horizon_bars,
                "live_eligible": c.live_eligible,
                "lifecycle": c.lifecycle,
                "resolver_id": c.resolver_id,
                "resolver_version": c.resolver_version,
                "proof": {
                    "target_wilson_low": c.proof.target_wilson_low,
                    "min_support": c.proof.min_support,
                    "min_forward_support": c.proof.min_forward_support,
                    "precision_applicable": c.proof.precision_applicable,
                },
                "allowed_sources": [
                    {"source_id": s.source_id, "required": s.required, "max_staleness_s": s.max_staleness_s}
                    for s in c.allowed_sources
                ],
            },
        })
    except Exception as e:
        return JSONResponse(content={"ok": False, "error": str(e)[:200]})


# ── artifacts ───────────────────────────────────────────────────────────────

@router.get("/artifacts")
async def list_research_artifacts(
    contract_id: str = Query(default=""),
    status: str = Query(default="ACTIVE"),
    limit: int = Query(default=50, ge=1, le=200),
):
    """List research artifacts, optionally filtered by contract and status."""
    try:
        from core.research_artifacts import list_artifacts
        artifacts = list_artifacts(
            contract_id=contract_id or None,
            status=status,
        )
        return JSONResponse(content={
            "ok": True,
            "artifacts": [
                {k: v for k, v in a.items() if k not in ("payload_bytes",)}
                for a in artifacts[:limit]
            ],
            "count": min(len(artifacts), limit),
            "total": len(artifacts),
        })
    except Exception as e:
        return JSONResponse(content={"ok": False, "error": str(e)[:200]})


@router.get("/artifacts/{artifact_sha}")
async def get_research_artifact(artifact_sha: str):
    """One artifact by SHA-256."""
    try:
        from core.research_artifacts import get_artifact, get_lifecycle_events
        artifact = get_artifact(artifact_sha)
        if not artifact:
            return JSONResponse(content={"ok": False, "error": "Artifact not found"})
        events = get_lifecycle_events(artifact_sha)
        return JSONResponse(content={
            "ok": True,
            "artifact": {k: v for k, v in artifact.items() if k != "payload_bytes"},
            "lifecycle_events": events,
        })
    except Exception as e:
        return JSONResponse(content={"ok": False, "error": str(e)[:200]})


# ── ledger ──────────────────────────────────────────────────────────────────

@router.get("/predictions")
async def list_research_predictions(
    contract_id: str = Query(default=""),
    artifact_sha: str = Query(default=""),
    resolved: bool = Query(default=True),
    limit: int = Query(default=100, ge=1, le=500),
):
    """List research predictions with optional filters."""
    try:
        from core.research_ledger import get_resolved_predictions, get_pending_predictions
        if resolved:
            rows = get_resolved_predictions(
                contract_id=contract_id or None,
                artifact_sha=artifact_sha or None,
                limit=limit,
            )
        else:
            rows = get_pending_predictions(
                contract_id=contract_id or None,
                limit=limit,
            )
        return JSONResponse(content={
            "ok": True,
            "predictions": rows,
            "count": len(rows),
            "resolved": resolved,
        })
    except Exception as e:
        return JSONResponse(content={"ok": False, "error": str(e)[:200]})


# ── proof ───────────────────────────────────────────────────────────────────

@router.get("/proof/{contract_id}/{artifact_sha}")
async def get_research_proof(contract_id: str, artifact_sha: str):
    """Forward proof status for a contract/artifact pair."""
    try:
        from core.research_proof import get_forward_registration, compute_proof
        from core.research_ledger import get_resolved_predictions

        reg = get_forward_registration(contract_id, artifact_sha)
        resolved = get_resolved_predictions(
            contract_id=contract_id, artifact_sha=artifact_sha, limit=500,
        )

        proof = compute_proof(resolved, min_support=10, expired_is_non_win=True)

        return JSONResponse(content={
            "ok": True,
            "contract_id": contract_id,
            "artifact_sha": artifact_sha,
            "registration": reg,
            "proof": {
                "total_predictions": proof.total_predictions,
                "actionable": proof.actionable,
                "wins": proof.wins,
                "losses": proof.losses,
                "expired": proof.expired,
                "data_invalid": proof.data_invalid,
                "win_rate": proof.win_rate,
                "wilson": proof.wilson,
                "brier": proof.brier,
                "coverage": proof.coverage,
                "invalid_rate": proof.invalid_rate,
                "proven": proof.proven,
                "target": proof.target,
            },
        })
    except Exception as e:
        return JSONResponse(content={"ok": False, "error": str(e)[:200]})


# ── activation ──────────────────────────────────────────────────────────────

@router.get("/activation/history")
async def get_activation_history_route(
    symbol: str = Query(default=""),
    direction: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
):
    """Activation event history."""
    try:
        from core.research_activation import get_activation_history
        history = get_activation_history(
            symbol=symbol or None,
            direction=direction or None,
            limit=limit,
        )
        return JSONResponse(content={"ok": True, "events": history, "count": len(history)})
    except Exception as e:
        return JSONResponse(content={"ok": False, "error": str(e)[:200]})


@router.get("/activation/lease/{artifact_sha}")
async def get_evidence_lease(
    artifact_sha: str,
    symbol: str = Query(default="WOLF"),
    direction: str = Query(default="UP"),
):
    """Current evidence lease status for an artifact."""
    try:
        from core.research_activation import compute_evidence_lease
        lease = compute_evidence_lease(
            artifact_sha=artifact_sha,
            symbol=symbol.upper(),
            direction=direction.upper(),
        )
        return JSONResponse(content={"ok": True, "lease": lease})
    except Exception as e:
        return JSONResponse(content={"ok": False, "error": str(e)[:200]})


# ── health ──────────────────────────────────────────────────────────────────

@router.get("/health")
async def research_platform_health():
    """Platform health: table presence, pending/invalid rates, lease state."""
    try:
        from core.db import db_conn
        health: Dict[str, Any] = {"ok": True, "tables": {}, "stats": {}}

        with db_conn() as conn:
            cur = conn.cursor()

            # Check table presence
            for table in (
                "ghost_research_artifacts",
                "ghost_research_predictions",
                "ghost_research_resolutions",
                "ghost_research_dataset_manifests",
                "ghost_research_dataset_samples",
                "ghost_research_forward_registrations",
                "ghost_research_activation_events",
                "ghost_research_activation_predecessors",
            ):
                cur.execute(
                    "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name=%s)",
                    (table,),
                )
                row = cur.fetchone()
                health["tables"][table] = bool(row and row[0])

            # Pending prediction count
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

            # Total resolved
            try:
                cur.execute("SELECT COUNT(*) FROM ghost_research_resolutions")
                row = cur.fetchone()
                health["stats"]["total_resolved"] = int(row[0]) if row else 0
            except Exception:
                health["stats"]["total_resolved"] = "table_not_ready"

            # Active artifacts
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM ghost_research_artifacts WHERE status='ACTIVE'"
                )
                row = cur.fetchone()
                health["stats"]["active_artifacts"] = int(row[0]) if row else 0
            except Exception:
                health["stats"]["active_artifacts"] = "table_not_ready"

        return JSONResponse(content=health)
    except Exception as e:
        return JSONResponse(content={"ok": False, "error": str(e)[:200]})


# ── note ────────────────────────────────────────────────────────────────────

@router.get("/")
async def research_platform_index():
    """Research platform overview."""
    return JSONResponse(content={
        "ok": True,
        "platform": "Ghost Research Platform v1",
        "endpoints": [
            "GET /api/research/contracts",
            "GET /api/research/contracts/{name}/{version}",
            "GET /api/research/artifacts",
            "GET /api/research/artifacts/{artifact_sha}",
            "GET /api/research/predictions",
            "GET /api/research/proof/{contract_id}/{artifact_sha}",
            "GET /api/research/activation/history",
            "GET /api/research/activation/lease/{artifact_sha}",
            "GET /api/research/health",
        ],
        "note": (
            "Research platform is evidence-generation only. No trading signals. "
            "Only tp_sl_swing artifacts may become live-eligible after exact-SHA "
            "forward Wilson proof. All other tasks are permanently research-only."
        ),
    })
