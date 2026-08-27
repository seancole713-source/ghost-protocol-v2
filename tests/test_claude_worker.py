"""Unit tests for the standalone persistent Claude worker."""
from __future__ import annotations

from services.claude_worker import worker


def _config():
    return worker.Config(
        ghost_base_url="https://ghost.example",
        ghost_token="ghost-secret",
        anthropic_api_key="anthropic-secret",
        heartbeat_seconds=600,
        max_repairs=2,
    )


def test_find_json_object_accepts_fenced_output():
    parsed = worker._find_json_object('```json\n{"summary":"ok","claims":{}}\n```')
    assert parsed["summary"] == "ok"


def test_source_normalization_uses_citations_and_deduplicates():
    now = 1_800_000_000
    content = [
        {
            "type": "text",
            "text": "result",
            "citations": [
                {
                    "type": "web_search_result_location",
                    "url": "https://example.com/release",
                    "title": "Official release",
                }
            ],
        }
    ]
    fallback = worker._source_refs_from_response(content, now)
    refs = worker._normalize_source_refs(
        [{"kind": "filing", "locator": "https://example.com/release"}], fallback, now,
    )
    assert refs == [
        {
            "kind": "filing",
            "locator": "https://example.com/release",
            "retrieved_ts": now,
        }
    ]


class _FakeResponse:
    status_code = 200
    headers = {}

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class _FormatRepairSession:
    def __init__(self):
        self.headers = {}
        self.calls = []

    def post(self, _url, json, timeout):
        self.calls.append(json)
        if len(self.calls) == 1:
            return _FakeResponse(
                {
                    "id": "msg_research",
                    "model": "claude",
                    "stop_reason": "max_tokens",
                    "usage": {},
                    "content": [
                        {
                            "type": "web_search_tool_result",
                            "content": [
                                {
                                    "type": "web_search_result",
                                    "url": "https://example.com/release",
                                    "title": "Official release",
                                }
                            ],
                        },
                        {"type": "text", "text": "Research draft without JSON."},
                    ],
                }
            )
        return _FakeResponse(
            {
                "id": "msg_format",
                "model": "claude",
                "stop_reason": "end_turn",
                "usage": {},
                "content": [
                    {
                        "type": "text",
                        "text": '{"summary":"Insufficient fresh evidence.","claims":{"verdict":"insufficient","evidence":[{}],"risks":["limited source"],"recommended_next_step":"monitor"},"source_refs":[],"agent_confidence":0.4}',
                    }
                ],
            }
        )


def test_anthropic_client_repairs_non_json_first_pass_without_second_web_search():
    session = _FormatRepairSession()
    client = worker.AnthropicClient(_config(), session=session)
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


class _FakeAnthropic:
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
            "source_refs": [
                {"kind": "official_release", "locator": "https://example.com/release"}
            ],
            "agent_confidence": 0.8,
            "raw_response": {"message_id": "msg_test"},
        }


def test_worker_repairs_quarantined_submission_under_same_lease():
    ghost = _FakeGhost()
    anthropic = _FakeAnthropic()
    service = worker.ClaudeWorker(_config(), ghost=ghost, anthropic=anthropic)

    assert service.run_once() == "accepted"
    assert len(ghost.submissions) == 2
    assert "repair_of_evidence_id" not in ghost.submissions[0]
    assert ghost.submissions[1]["repair_of_evidence_id"] == "evd_" + "b" * 32
    assert anthropic.calls[1]["correction"]["quarantine_category"] == "schema_error"
    assert ghost.released == []
    assert ghost.worker_beats[-1][0] == "IDLE"
    assert ghost.worker_beats[-1][1]["accepted_delta"] == 1
    assert ghost.worker_beats[-1][1]["quarantined_delta"] == 1


def test_rate_budget_enforces_hourly_and_daily_caps():
    budget = worker.RateBudget(per_hour=2, per_day=3)
    budget.record(1000)
    budget.record(1100)
    assert budget.allowed(1200) is False
    assert budget.allowed(5000) is True
    budget.record(5000)
    assert budget.allowed(5100) is False
