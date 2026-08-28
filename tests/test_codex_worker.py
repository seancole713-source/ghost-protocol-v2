"""Unit tests for the standalone persistent Codex (OpenAI) worker.

Structural mirror of tests/test_claude_worker.py -- the Ghost-integration
guarantees (GhostClient contract, repair loop, rate budget) are identical
by design, so they're tested the same way. The tests specific to this file
cover what's actually different: the Responses API extraction shape, the
distinct-agent_id guard that makes consensus possible, and the
ResearchIncompleteError refusal-to-submit-unsupported-claims guard.
"""
from __future__ import annotations

import pytest

from services.codex_worker import worker


def _config(**overrides):
    base = dict(
        ghost_base_url="https://ghost.example",
        ghost_token="ghost-secret",
        openai_api_key="openai-secret",
        heartbeat_seconds=600,
        max_repairs=2,
    )
    base.update(overrides)
    return worker.Config(**base)


def test_agent_id_defaults_distinct_from_claude_worker():
    """The entire consensus mechanism (core.agent_workflow.submit_evidence's
    COUNT(DISTINCT agent_id)) depends on this never colliding."""
    cfg = _config()
    assert not cfg.agent_id.startswith("claude")
    assert cfg.agent_id == "codex.production.worker"


def test_from_env_rejects_agent_id_starting_with_claude(monkeypatch):
    monkeypatch.setenv("GHOST_BASE_URL", "https://ghost.example")
    monkeypatch.setenv("GHOST_MCP_TOKEN", "t")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("CODEX_WORKER_AGENT_ID", "claude.impersonator")
    with pytest.raises(worker.WorkerError, match="must not start with 'claude'"):
        worker.Config.from_env()


def test_find_json_object_accepts_fenced_output():
    parsed = worker._find_json_object('```json\n{"summary":"ok","claims":{}}\n```')
    assert parsed["summary"] == "ok"


def test_source_extraction_from_responses_api_output():
    now = 1_800_000_000
    output = [
        {
            "type": "message",
            "content": [
                {
                    "type": "output_text",
                    "text": "result",
                    "annotations": [
                        {
                            "type": "url_citation",
                            "url": "https://example.com/release",
                            "title": "Official release",
                        }
                    ],
                }
            ],
        }
    ]
    fallback = worker._source_refs_from_openai_response(output, now)
    assert fallback == [
        {"kind": "web_search", "locator": "https://example.com/release", "retrieved_ts": now, "title": "Official release"}
    ]


def test_source_extraction_ignores_urls_without_citation_hint():
    """A plain URL-shaped string anywhere in the payload must not be
    mistaken for a real citation -- only annotation/citation-typed items
    count (rule: never guess a source into existence)."""
    now = 1_800_000_000
    output = [{"type": "message", "content": [{"type": "output_text", "text": "see https://example.com"}]}]
    assert worker._source_refs_from_openai_response(output, now) == []


def test_normalize_source_refs_matches_ghost_contract_shape():
    now = 1_800_000_000
    refs = worker._normalize_source_refs(
        [{"kind": "filing", "locator": "https://example.com/release"}], [], now,
    )
    assert refs == [{"kind": "filing", "locator": "https://example.com/release", "retrieved_ts": now}]


class _FakeResponse:
    status_code = 200
    headers = {}

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class _ResearchSession:
    """One response containing both a JSON envelope and real citations --
    the happy path, no format-repair round-trip needed."""

    def __init__(self):
        self.headers = {}
        self.calls = []

    def post(self, _url, json, timeout):
        self.calls.append(json)
        return _FakeResponse(
            {
                "id": "resp_1",
                "model": "gpt-5",
                "status": "completed",
                "usage": {},
                "output_text": (
                    '{"summary":"Source-backed.","claims":{"verdict":"supports",'
                    '"evidence":[{"fact":"x"}],"risks":[],"recommended_next_step":"monitor"},'
                    '"source_refs":[{"kind":"official_release","locator":"https://example.com/release"}],'
                    '"agent_confidence":0.8}'
                ),
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "annotations": [
                                    {"type": "url_citation", "url": "https://example.com/release", "title": "Release"}
                                ],
                            }
                        ],
                    }
                ],
            }
        )


def test_openai_client_research_happy_path_needs_no_repair():
    session = _ResearchSession()
    client = worker.OpenAIClient(_config(), session=session)
    result = client.research(
        {"task_id": "canary", "request_payload": {"question": "research"}},
        {"required_response_schema": {"type": "object"}},
    )
    assert len(session.calls) == 1
    assert result["claims"]["verdict"] == "supports"
    assert result["source_refs"][0]["locator"] == "https://example.com/release"
    assert result["raw_response"]["format_repaired"] is False


class _NoSourcesSession:
    """Model returns a well-formed envelope but zero real citations --
    must refuse to submit rather than send unsupported claims."""

    def __init__(self):
        self.headers = {}

    def post(self, _url, json, timeout):
        return _FakeResponse(
            {
                "id": "resp_1",
                "model": "gpt-5",
                "output_text": (
                    '{"summary":"no sources","claims":{"verdict":"insufficient","evidence":[{}],'
                    '"risks":[],"recommended_next_step":"monitor"},"source_refs":[],"agent_confidence":0.2}'
                ),
                "output": [],
            }
        )


def test_research_refuses_to_submit_when_no_source_refs_found():
    client = worker.OpenAIClient(_config(), session=_NoSourcesSession())
    with pytest.raises(worker.ResearchIncompleteError):
        client.research(
            {"task_id": "canary", "request_payload": {}},
            {"required_response_schema": {"type": "object"}},
        )


class _FormatRepairSession:
    """First response has no clean JSON but does carry a real citation;
    second (format-repair) response has the JSON."""

    def __init__(self):
        self.headers = {}
        self.calls = []

    def post(self, _url, json, timeout):
        self.calls.append(json)
        if len(self.calls) == 1:
            return _FakeResponse(
                {
                    "id": "resp_research",
                    "model": "gpt-5",
                    "usage": {},
                    "output_text": "Research draft without JSON.",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "annotations": [
                                        {"type": "url_citation", "url": "https://example.com/release", "title": "Release"}
                                    ],
                                }
                            ],
                        }
                    ],
                }
            )
        return _FakeResponse(
            {
                "id": "resp_format",
                "model": "gpt-5",
                "usage": {},
                "output_text": (
                    '{"summary":"Insufficient fresh evidence.","claims":{"verdict":"insufficient",'
                    '"evidence":[{}],"risks":["limited source"],"recommended_next_step":"monitor"},'
                    '"source_refs":[],"agent_confidence":0.4}'
                ),
                "output": [],
            }
        )


def test_openai_client_repairs_non_json_first_pass_without_second_web_search():
    session = _FormatRepairSession()
    client = worker.OpenAIClient(_config(), session=session)
    result = client.research(
        {"task_id": "canary", "request_payload": {"question": "research"}},
        {"required_response_schema": {"type": "object"}},
    )
    assert len(session.calls) == 2
    assert "tools" in session.calls[0]
    assert "tools" not in session.calls[1]
    assert result["raw_response"]["format_repaired"] is True
    assert result["source_refs"][0]["locator"] == "https://example.com/release"


class _FakeGhost:
    def __init__(self):
        self.submissions = []
        self.worker_beats = []
        self.released = []

    def claim(self):
        return {
            "claimed": True,
            "task": {"task_id": "agt_" + "a" * 32, "symbol": "BRNX"},
            "lease_token": "lease-token",
            "submission_contract": {"version": "ghost.agent-evidence/v2"},
        }

    def heartbeat_task(self, task_id, lease_token):
        return {"ok": True}

    def submit(self, task_id, payload):
        self.submissions.append(payload)
        if len(self.submissions) == 1:
            return {
                "accepted": False,
                "task_status": "CLAIMED",
                "lease_retained": True,
                "retry_allowed": True,
                "quarantine_category": "schema_error",
                "validation_errors": [
                    {"code": "required", "path": "claims.verdict", "message": "required"}
                ],
                "evidence": {"evidence_id": "evd_" + "b" * 32},
                "submission_contract": {"version": "ghost.agent-evidence/v2"},
            }
        return {"accepted": True, "task_status": "COMPLETED"}

    def release(self, task_id, lease_token, reason):
        self.released.append(reason)
        return {"ok": True}

    def worker_heartbeat(self, status, **kwargs):
        self.worker_beats.append((status, kwargs))
        return {"ok": True}


class _FakeOpenAI:
    def __init__(self):
        self.calls = []

    def research(self, task, contract, **kwargs):
        self.calls.append(kwargs)
        return {
            "summary": "Source-backed research.",
            "claims": {
                "verdict": "supports",
                "evidence": [{"fact": "verified"}],
                "risks": [],
                "recommended_next_step": "monitor",
            },
            "source_refs": [{"kind": "official_release", "locator": "https://example.com/release"}],
            "agent_confidence": 0.8,
            "raw_response": {"response_id": "resp_test"},
        }


def test_worker_repairs_quarantined_submission_under_same_lease():
    ghost = _FakeGhost()
    openai = _FakeOpenAI()
    service = worker.CodexWorker(_config(), ghost=ghost, openai=openai)

    assert service.run_once() == "accepted"
    assert len(ghost.submissions) == 2
    assert ghost.submissions[0]["agent_provider"] == "openai"
    assert "repair_of_evidence_id" not in ghost.submissions[0]
    assert ghost.submissions[1]["repair_of_evidence_id"] == "evd_" + "b" * 32
    assert openai.calls[1]["correction"]["quarantine_category"] == "schema_error"
    assert ghost.released == []
    assert ghost.worker_beats[-1][0] == "IDLE"
    assert ghost.worker_beats[-1][1]["accepted_delta"] == 1
    assert ghost.worker_beats[-1][1]["quarantined_delta"] == 1


class _IncompleteResearchOpenAI:
    def research(self, task, contract, **kwargs):
        raise worker.ResearchIncompleteError("no usable source_refs")


def test_worker_releases_task_on_incomplete_research_without_burning_a_submission():
    ghost = _FakeGhost()
    service = worker.CodexWorker(_config(), ghost=ghost, openai=_IncompleteResearchOpenAI())

    assert service.run_once() == "incomplete"
    assert ghost.submissions == []  # never attempted a submission
    assert len(ghost.released) == 1


def test_rate_budget_enforces_hourly_and_daily_caps():
    budget = worker.RateBudget(per_hour=2, per_day=3)
    budget.record(1000)
    budget.record(1100)
    assert budget.allowed(1200) is False
    assert budget.allowed(5000) is True
    budget.record(5000)
    assert budget.allowed(5100) is False
