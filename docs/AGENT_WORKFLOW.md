# Ghost Agent Workflow

Ghost and connected research agents communicate through a durable,
advisory-only task queue. The workflow is intentionally separate from
prediction issuance, alerts, portfolio state, and order execution.

## Lifecycle

1. Ghost creates an idempotent task from a persisted observation or an
   authenticated operator request.
2. Claude, Codex, or another authenticated agent calls
   `ghost_agent_claim_task` and receives a short-lived lease token plus the
   exact response schema, a valid submission example, and repair rules.
3. The agent may call `ghost_agent_heartbeat` while researching.
4. The agent calls `ghost_agent_submit_evidence` with structured claims,
   source references, model identity, prompt version, and confidence.
5. Ghost validates schema, bounds, source metadata, timestamps, and task
   ownership. Invalid evidence is categorized as `schema_error`,
   `source_error`, `injection_suspected`, or `policy_violation`.
6. Repairable schema/source failures retain the active lease and return
   machine-readable errors. The agent resubmits with
   `repair_of_evidence_id`; every rejected and corrected version stays in the
   append-only evidence ledger.
7. Tasks requiring consensus return to `PENDING` until distinct agents provide
   the configured number of accepted submissions.
8. Every transition is appended to `ghost_agent_task_events`. Evidence and its
   validation result remain immutable.

Expired leases are automatically requeued until `max_attempts`; tasks then
move to `DEAD_LETTER`. Missed deadlines move to `EXPIRED`.

## MCP Tools

- `ghost_agent_tasks`
- `ghost_agent_task`
- `ghost_agent_claim_task`
- `ghost_agent_heartbeat`
- `ghost_agent_submit_evidence`
- `ghost_agent_release_task`
- `ghost_agent_workflow_health`

All tool calls require the existing MCP OAuth or protected token path.

## REST Surface

Authenticated REST operations are mounted at `/api/agent-workflow`:

- `GET /health`
- `GET /tasks`
- `GET /tasks/{task_id}`
- `POST /tasks`
- `POST /claim`
- `POST /tasks/{task_id}/heartbeat`
- `POST /tasks/{task_id}/evidence`
- `POST /tasks/{task_id}/release`
- `POST /workers/heartbeat`

The cookie-authenticated operator dashboard at `/admin` includes worker
heartbeats, task states, categorized quarantines, repairs, and recent evidence.

## Evidence Contract

The default claims object must contain:

```json
{
  "verdict": "supports",
  "evidence": [{"fact": "source-backed finding"}],
  "risks": ["known limitation"],
  "recommended_next_step": "continue advisory monitoring"
}
```

Every submission must include at least one source reference with `kind` and
`locator`. Source timestamps cannot be in the future. Tasks may provide a
stricter JSON response schema.

When a repairable submission is quarantined, the response contains:

```json
{
  "accepted": false,
  "task_status": "CLAIMED",
  "quarantine_category": "schema_error",
  "retry_allowed": true,
  "lease_retained": true,
  "validation_errors": [
    {
      "code": "required",
      "path": "claims.verdict",
      "message": "required",
      "category": "schema_error",
      "repairable": true
    }
  ]
}
```

## Persistent Claude Worker

`services/claude_worker/worker.py` is deployed as a separate Railway worker.
It polls for `external_mover_triage` tasks, maintains task and worker
heartbeats, researches through Anthropic's server-side web search, submits the
strict evidence envelope, repairs bounded schema/source failures, and releases
work safely on errors. It has hourly/daily task budgets and never connects to
the prediction or execution paths.

Required worker variables:

- `GHOST_BASE_URL`
- `GHOST_MCP_TOKEN`
- `ANTHROPIC_API_KEY`
- `CLAUDE_WORKER_ENABLED=1`

Optional worker controls include `CLAUDE_WORKER_MODEL`,
`CLAUDE_WORKER_MAX_TASKS_PER_HOUR`, `CLAUDE_WORKER_MAX_TASKS_PER_DAY`,
`CLAUDE_WORKER_WEB_SEARCH_MAX_USES`, and `CLAUDE_WORKER_MAX_REPAIRS`.

## Automatic Tasks

The external radar creates one `external_mover_triage` task per significant
symbol and New York session date when the observed absolute move is at least
5% or relative volume is at least 2. The operation is idempotent and agent
workflow failures cannot fail the market-data scheduler.

Set `AGENT_WORKFLOW_AUTOTASKS_ENABLED=0` to disable automatic task creation.
Set `AGENT_EVENT_REQUIRED_SUBMISSIONS=1..3` to require independent agent
consensus for mover triage.

## Safety Boundary

Database constraints force `advisory_only=true` and
`decision_eligible=false` on tasks and evidence. The workflow has no import or
call path to prediction issuance, alerting, portfolio mutation, kill-switch
control, or broker execution. A separate deterministic, preregistered research
contract must evaluate agent-derived features before they can influence any
official prediction.
