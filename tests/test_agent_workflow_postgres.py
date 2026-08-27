"""PostgreSQL integration tests for durable Ghost-agent task transitions."""
from __future__ import annotations

import os
import time

import psycopg2
import pytest
from fastapi.testclient import TestClient

from core import agent_workflow as workflow


pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def workflow_database(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url or os.getenv("GHOST_INTEGRATION_TESTS") != "1":
        pytest.skip("agent workflow PostgreSQL tests require TEST_DATABASE_URL and GHOST_INTEGRATION_TESTS=1")

    class _DbContext:
        def __enter__(self):
            self.conn = psycopg2.connect(url)
            return self.conn

        def __exit__(self, exc_type, *_args):
            try:
                if exc_type:
                    self.conn.rollback()
                else:
                    self.conn.commit()
            finally:
                self.conn.close()

    import core.db as db

    monkeypatch.setattr(db, "db_conn", _DbContext)
    with _DbContext() as conn:
        workflow.ensure_agent_workflow_tables(conn.cursor())
    with _DbContext() as conn:
        conn.cursor().execute(
            "TRUNCATE ghost_agent_evidence_validations, ghost_agent_evidence, "
            "ghost_agent_task_events, ghost_agent_tasks RESTART IDENTITY CASCADE"
        )
    yield
    with _DbContext() as conn:
        conn.cursor().execute(
            "TRUNCATE ghost_agent_evidence_validations, ghost_agent_evidence, "
            "ghost_agent_task_events, ghost_agent_tasks RESTART IDENTITY CASCADE"
        )


def _claims():
    return {
        "verdict": "supports",
        "evidence": [{"fact": "source-backed catalyst"}],
        "risks": ["spread"],
        "recommended_next_step": "continue advisory monitoring",
    }


def _sources(now):
    return [
        {
            "kind": "official_release",
            "locator": "https://example.com/release",
            "published_ts": now - 10,
            "retrieved_ts": now,
        }
    ]


def test_durable_claim_heartbeat_submit_and_audit_round_trip():
    created = workflow.create_task(
        task_type="earnings_event_triage",
        symbol="OKTA",
        requested_by="ghost.earnings",
        request_payload={"question": "Classify the earnings move"},
        idempotency_key="earnings:OKTA:2026-08-27",
        now_ts=1_800_000_000,
    )
    duplicate = workflow.create_task(
        task_type="earnings_event_triage",
        symbol="OKTA",
        requested_by="ghost.earnings",
        request_payload={"question": "Classify the earnings move"},
        idempotency_key="earnings:OKTA:2026-08-27",
        now_ts=1_800_000_001,
    )
    assert created["created"] is True
    assert duplicate["created"] is False
    assert duplicate["task"]["task_id"] == created["task"]["task_id"]
    with pytest.raises(workflow.AgentWorkflowError, match="different task payload"):
        workflow.create_task(
            task_type="earnings_event_triage",
            symbol="OKTA",
            requested_by="ghost.earnings",
            request_payload={"question": "A different request"},
            idempotency_key="earnings:OKTA:2026-08-27",
            now_ts=1_800_000_002,
        )

    claimed = workflow.claim_task(
        agent_id="claude.production",
        lease_seconds=600,
        now_ts=1_800_000_010,
    )
    assert claimed["claimed"] is True
    assert claimed["task"]["symbol"] == "OKTA"
    assert claimed["task"]["decision_eligible"] is False

    heartbeat = workflow.heartbeat_task(
        task_id=claimed["task"]["task_id"],
        agent_id="claude.production",
        lease_token=claimed["lease_token"],
        lease_seconds=900,
        now_ts=1_800_000_020,
    )
    assert heartbeat["lease_expires_at"] == 1_800_000_920

    submitted = workflow.submit_evidence(
        task_id=claimed["task"]["task_id"],
        agent_id="claude.production",
        lease_token=claimed["lease_token"],
        agent_provider="anthropic",
        model_name="claude",
        prompt_version="event-triage/v1",
        summary="Official evidence supports an earnings-gap classification.",
        claims=_claims(),
        source_refs=_sources(1_800_000_030),
        agent_confidence=0.8,
        now_ts=1_800_000_030,
    )
    assert submitted["accepted"] is True
    assert submitted["task_status"] == "COMPLETED"
    assert submitted["evidence"]["decision_eligible"] is False

    audit = workflow.get_task(claimed["task"]["task_id"])
    assert audit["task"]["status"] == "COMPLETED"
    assert "lease_token_sha256" not in audit["task"]
    assert len(audit["evidence"]) == 1
    assert [event["event_type"] for event in audit["events"]] == [
        "CREATED", "CLAIMED", "HEARTBEAT", "EVIDENCE_ACCEPTED",
    ]
    health = workflow.workflow_health()
    assert health["tasks"]["completed"] == 1
    assert health["evidence"]["accepted"] == 1


def test_consensus_task_requires_distinct_accepted_agents():
    task = workflow.create_task(
        task_type="catalyst_consensus",
        symbol="ARCT",
        requested_by="ghost.external_radar",
        request_payload={"question": "Verify the catalyst independently"},
        required_submissions=2,
        max_attempts=3,
        idempotency_key="consensus:ARCT:2026-08-27",
        now_ts=1_800_100_000,
    )["task"]

    claude = workflow.claim_task(agent_id="claude.production", now_ts=1_800_100_010)
    first = workflow.submit_evidence(
        task_id=task["task_id"],
        agent_id="claude.production",
        lease_token=claude["lease_token"],
        agent_provider="anthropic",
        model_name="claude",
        prompt_version="consensus/v1",
        summary="First independent review.",
        claims=_claims(),
        source_refs=_sources(1_800_100_020),
        now_ts=1_800_100_020,
    )
    assert first["task_status"] == "PENDING"
    assert first["accepted_submissions"] == 1

    no_repeat = workflow.claim_task(agent_id="claude.production", now_ts=1_800_100_030)
    assert no_repeat["claimed"] is False

    codex = workflow.claim_task(agent_id="codex.production", now_ts=1_800_100_040)
    second = workflow.submit_evidence(
        task_id=task["task_id"],
        agent_id="codex.production",
        lease_token=codex["lease_token"],
        agent_provider="openai",
        model_name="codex",
        prompt_version="consensus/v1",
        summary="Second independent review.",
        claims=_claims(),
        source_refs=_sources(1_800_100_050),
        now_ts=1_800_100_050,
    )
    assert second["task_status"] == "COMPLETED"
    assert second["accepted_submissions"] == 2


def test_wrong_lease_token_cannot_submit():
    task = workflow.create_task(
        task_type="market_event_triage",
        symbol="WOLF",
        requested_by="ghost.test",
        request_payload={"question": "test"},
        idempotency_key="lease-test:WOLF:2026-08-27",
        now_ts=1_800_200_000,
    )["task"]
    workflow.claim_task(agent_id="claude.production", now_ts=1_800_200_010)
    with pytest.raises(workflow.AgentWorkflowError, match="invalid lease token"):
        workflow.submit_evidence(
            task_id=task["task_id"],
            agent_id="claude.production",
            lease_token="wrong",
            agent_provider="anthropic",
            model_name="claude",
            prompt_version="test/v1",
            summary="Should fail.",
            claims=_claims(),
            source_refs=_sources(1_800_200_020),
            now_ts=1_800_200_020,
        )


def test_repairable_quarantine_retains_lease_and_accepts_corrected_resubmission():
    task = workflow.create_task(
        task_type="repairable_schema_test",
        symbol="BRNX",
        requested_by="ghost.test",
        request_payload={"question": "Classify the move"},
        idempotency_key="repairable:BRNX:2026-08-27",
        now_ts=1_800_250_000,
    )["task"]
    claimed = workflow.claim_task(
        agent_id="claude.production", lease_seconds=600, now_ts=1_800_250_010,
    )
    rejected = workflow.submit_evidence(
        task_id=task["task_id"],
        agent_id="claude.production",
        lease_token=claimed["lease_token"],
        agent_provider="anthropic",
        model_name="claude",
        prompt_version="repair/v2",
        summary="Useful research in the wrong shape.",
        claims={"classification": "news_breakout"},
        source_refs=[{"url": "https://example.com/release"}],
        now_ts=1_800_250_020,
    )
    assert rejected["accepted"] is False
    assert rejected["task_status"] == "CLAIMED"
    assert rejected["lease_retained"] is True
    assert rejected["retry_allowed"] is True
    assert rejected["quarantine_category"] == "schema_error"
    assert rejected["validation_errors"]
    assert workflow.get_task(task["task_id"])["task"]["status"] == "CLAIMED"

    repaired = workflow.submit_evidence(
        task_id=task["task_id"],
        agent_id="claude.production",
        lease_token=claimed["lease_token"],
        agent_provider="anthropic",
        model_name="claude",
        prompt_version="repair/v2",
        summary="Corrected source-backed evidence.",
        claims=_claims(),
        source_refs=_sources(1_800_250_030),
        repair_of_evidence_id=rejected["evidence"]["evidence_id"],
        now_ts=1_800_250_030,
    )
    assert repaired["accepted"] is True
    assert repaired["task_status"] == "COMPLETED"
    audit = workflow.get_task(task["task_id"])
    assert [item["validation_status"] for item in audit["evidence"]] == [
        "QUARANTINED", "ACCEPTED",
    ]
    assert audit["evidence"][0]["quarantine_category"] == "schema_error"
    assert audit["evidence"][1]["repair_of_evidence_id"] == rejected["evidence"]["evidence_id"]


def test_worker_heartbeat_and_dashboard_are_durable_and_sanitized():
    first = workflow.heartbeat_worker(
        agent_id="claude.production.worker",
        agent_provider="anthropic",
        model_name="claude-sonnet",
        status="STARTING",
        metadata={"worker_version": "1.0"},
        now_ts=1_800_260_000,
    )
    assert first["worker"]["decision_eligible"] is False
    workflow.heartbeat_worker(
        agent_id="claude.production.worker",
        agent_provider="anthropic",
        model_name="claude-sonnet",
        status="IDLE",
        processed_delta=1,
        accepted_delta=1,
        now_ts=1_800_260_030,
    )
    dashboard = workflow.workflow_dashboard(limit=10, now_ts=1_800_260_040)
    assert dashboard["workers"][0]["online"] is True
    assert dashboard["workers"][0]["processed_count"] == 1
    assert dashboard["workers"][0]["accepted_count"] == 1
    assert dashboard["safety"] == {"advisory_only": True, "decision_eligible": False}
    assert "ghost_token" not in str(dashboard).lower()


def test_maintenance_requeues_then_dead_letters_abandoned_leases():
    task = workflow.create_task(
        task_type="abandoned_task_test",
        symbol="WOLF",
        requested_by="ghost.test",
        request_payload={"question": "test lease recovery"},
        max_attempts=2,
        idempotency_key="abandoned:WOLF:2026-08-27",
        now_ts=1_800_300_000,
    )["task"]
    workflow.claim_task(
        agent_id="claude.production", lease_seconds=60, now_ts=1_800_300_010,
    )
    first = workflow.maintain_workflow(now_ts=1_800_300_071)
    assert first["requeued"] == 1
    assert workflow.get_task(task["task_id"])["task"]["status"] == "PENDING"

    workflow.claim_task(
        agent_id="claude.production", lease_seconds=60, now_ts=1_800_300_080,
    )
    second = workflow.maintain_workflow(now_ts=1_800_300_141)
    assert second["dead_letter"] == 1
    assert workflow.get_task(task["task_id"])["task"]["status"] == "DEAD_LETTER"


def test_authenticated_rest_workflow_round_trip(monkeypatch):
    monkeypatch.setenv("GHOST_TEST_MODE", "1")
    monkeypatch.setenv("GHOST_MCP_TOKEN", "agent-api-secret")
    import wolf_app

    headers = {"X-Ghost-Mcp-Token": "agent-api-secret"}
    with TestClient(wolf_app.APP) as client:
        created = client.post(
            "/api/agent-workflow/tasks",
            headers=headers,
            json={
                "task_type": "api_round_trip",
                "symbol": "OKTA",
                "requested_by": "ghost.test",
                "request_payload": {"question": "Verify the event"},
                "idempotency_key": "api-round-trip:OKTA:2026-08-27",
            },
        )
        assert created.status_code == 201
        task_id = created.json()["task"]["task_id"]

        claimed = client.post(
            "/api/agent-workflow/claim",
            headers=headers,
            json={"agent_id": "claude.production", "lease_seconds": 600},
        )
        assert claimed.status_code == 200
        lease_token = claimed.json()["lease_token"]

        now = int(time.time())
        submitted = client.post(
            f"/api/agent-workflow/tasks/{task_id}/evidence",
            headers=headers,
            json={
                "agent_id": "claude.production",
                "lease_token": lease_token,
                "agent_provider": "anthropic",
                "model_name": "claude",
                "prompt_version": "api-test/v1",
                "summary": "Verified through the authenticated REST workflow.",
                "claims": _claims(),
                "source_refs": _sources(now),
                "agent_confidence": 0.75,
            },
        )
        assert submitted.status_code == 200
        assert submitted.json()["task_status"] == "COMPLETED"
        assert submitted.json()["evidence"]["decision_eligible"] is False

        audit = client.get(f"/api/agent-workflow/tasks/{task_id}", headers=headers)
        assert audit.status_code == 200
        assert audit.json()["task"]["status"] == "COMPLETED"
