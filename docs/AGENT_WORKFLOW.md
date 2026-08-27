# Ghost Agent Workflow

Ghost and connected research agents communicate through a durable,
advisory-only task queue. The workflow is intentionally separate from
prediction issuance, alerts, portfolio state, and order execution.

## Lifecycle

1. Ghost creates an idempotent task from a persisted observation or an
   authenticated operator request.
2. Claude, Codex, or another authenticated agent calls
   `ghost_agent_claim_task` and receives a short-lived lease token.
3. The agent may call `ghost_agent_heartbeat` while researching.
4. The agent calls `ghost_agent_submit_evidence` with structured claims,
   source references, model identity, prompt version, and confidence.
5. Ghost validates schema, bounds, source metadata, timestamps, and task
   ownership. Invalid evidence is quarantined; valid evidence is accepted.
6. Tasks requiring consensus return to `PENDING` until distinct agents provide
   the configured number of accepted submissions.
7. Every transition is appended to `ghost_agent_task_events`. Evidence and its
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
