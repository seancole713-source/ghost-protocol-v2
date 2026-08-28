# Independent Codex Worker — Interface Contract

Status: **specification only, not implemented.** No code in this repo builds
or deploys this worker. This document exists so whoever does build it isn't
reverse-engineering the protocol from `services/claude_worker/worker.py` —
and so the result plugs into consensus scoring that already exists, with
zero changes to `core/agent_workflow.py` or `core/shadow_evidence_ledger.py`.

Everything below is verified against the live code in this repo as of
`c85fdd9` (`core/agent_workflow.py`, `api/agent_workflow_endpoints.py`,
`services/claude_worker/worker.py`), not assumed.

---

## 1. The one fact that makes consensus already work today

```python
# core/agent_workflow.py, submit_evidence()
cur.execute(
    """SELECT COUNT(DISTINCT agent_id) FROM ghost_agent_evidence
       WHERE task_id=%s AND validation_status='ACCEPTED'""",
    (task_id,),
)
accepted_count = int((cur.fetchone() or (0,))[0])
required = int(task.get("required_submissions") or 1)
...
if validation_status == "ACCEPTED" and accepted_count >= required:
    next_status = "COMPLETED"
else:
    next_status = "PENDING"   # task reopens, unclaimed, for another agent
```

`accepted_count` is `COUNT(DISTINCT agent_id)` — not row count. When a task's
`required_submissions` is 2 and one agent submits ACCEPTED evidence, the task
does **not** complete: it reopens to `PENDING` with `claimed_by` cleared,
ready for a *different* `agent_id` to claim and submit. Two submissions from
the same agent never satisfy a `required_submissions >= 2` task — the
distinctness is enforced by the `COUNT(DISTINCT agent_id)`, not by any
per-worker logic.

**This means the server-side consensus mechanism needs no new code.** The
two things that don't exist yet are:

1. A second worker registering under an `agent_id` distinct from the Claude
   worker's (this document).
2. Task issuance (`ghost.external_radar`, in whatever module creates
   `external_mover_triage` tasks) setting `required_submissions=2` for
   events important enough to require consensus. Every task observed live
   in this session had `required_submissions: 1` — that's an issuance-side
   change, not a worker-side one, and out of scope for this document.

## 2. Auth

Every `/api/agent-workflow/*` route is gated by `require_mcp_auth`
(`mcp/security.py`) — the same mechanism `services/claude_worker/worker.py`
already uses. Concretely: a `GHOST_MCP_TOKEN` bearer/header credential, or an
OAuth bearer token from the same MCP OAuth flow. No new auth surface to
build; provision a token for the Codex worker the same way the Claude
worker's was provisioned (separate token recommended, so the two workers'
credentials can be revoked/rotated independently).

## 3. HTTP surface (`/api/agent-workflow/*`)

| Method | Path | Purpose |
|---|---|---|
| POST | `/claim` | Claim the next available task matching `task_types`; server picks it, not the caller |
| POST | `/tasks/{task_id}/heartbeat` | Extend the lease while researching |
| POST | `/tasks/{task_id}/evidence` | Submit evidence |
| POST | `/tasks/{task_id}/release` | Release a claimed task without submitting |
| POST | `/workers/heartbeat` | Worker-level liveness ping (for the dashboard) |
| GET | `/health` | `workflow_health()` — queue depth, quarantine counts |
| GET | `/tasks`, `/tasks/{task_id}` | Optional: peek at queue depth or one task's full state without claiming (dashboard/debugging use, not part of the core loop) |

All nine routes above are confirmed present in `api/agent_workflow_endpoints.py`
by exact path and method — verified against the route decorators directly,
not inferred from the client.

Field names below match `services/claude_worker/worker.py`'s `GhostClient`
class exactly (`claim`, `heartbeat_task`, `submit`, `release`,
`worker_heartbeat` methods) — that file is the executable reference if this
document and the code ever drift.

## 4. Claim → heartbeat → research → submit → release loop

```
1. POST /claim   {agent_id, lease_seconds, task_types: ["external_mover_triage"]}
   -> {task: {task_id, ...request_payload, required_response_schema}, lease_token, lease_expires_at}
   (no task_id in the request -- the server atomically picks the next
   PENDING task matching task_types and hands it to this caller; there is
   no separate "list then claim by id" step in the reference worker)
2. while researching:
     POST /tasks/{task_id}/heartbeat   {agent_id, lease_token, lease_seconds}
     (extends lease_expires_at; call at roughly lease_seconds/3 intervals)
3. POST /tasks/{task_id}/evidence
   {agent_id, lease_token, agent_provider, model_name, prompt_version,
    summary, claims, source_refs, agent_confidence}
   -> {accepted, task_status, accepted_submissions, required_submissions,
       quarantine_category, validation_errors, retry_allowed, lease_retained}
4. if retry_allowed and lease_retained: correct claims/source_refs per
   validation_errors, resubmit with claims.repair_of_evidence_id set to
   the rejected evidence_id, same lease_token, before lease_expires_at.
5. if unable to research this task: POST /tasks/{task_id}/release
   {agent_id, lease_token, reason}
```

`agent_id` **must be stable and distinct from the Claude worker's** (e.g.
`codex-worker-prod`, not `claude-*`). This is the entire mechanism consensus
depends on — see §1.

## 5. The evidence schema (do not deviate)

```jsonc
// claims — must validate against task.required_response_schema
// (DEFAULT_RESPONSE_SCHEMA unless a task overrides it):
{
  "verdict": "supports" | "rejects" | "mixed" | "insufficient",
  "evidence": [ { /* at least 1 object; shape is free-form */ } ],
  "risks": ["string", ...],
  "recommended_next_step": "non-empty string",
  "classification": "..."   // task-specific, e.g. external_mover_triage's
                             // earnings_gap/short_squeeze/news_breakout/
                             // momentum_anomaly/unknown -- read from
                             // task.request_payload.required_output.classifications
}

// source_refs — every entry needs BOTH fields, non-empty, bounded:
[
  {
    "kind": "sec_filing" | "exchange_notice" | "press_release" |
            "news_article" | "equity_research" | "web_search" | ...,
    "locator": "https://...",          // <= 2048 chars
    "published_ts": 1787862258,        // optional, epoch seconds, not in the future
    "observed_ts": null,
    "retrieved_ts": null
  }
]
```

This is not advisory formatting guidance — it's `validate_submission`'s
literal `required`/`type`/`minItems`/`enum` checks in
`core/agent_workflow.py`. The two prior manual-chat submissions in this
session's live queue were quarantined for exactly this: free-form field
names (`classification`/`pattern_observed` instead of `verdict`/`evidence`/
`risks`/`recommended_next_step`) and `{url, note}` source_refs instead of
`{locator, kind}`. `services/claude_worker/worker.py`'s
`_normalize_source_refs` helper exists specifically to guarantee this shape
before every submission — port that function, don't reimplement it from
scratch.

Malformed submissions are **not** rejected as `injection_suspected` — that
category is reserved for a distinct pattern-match against known
prompt-injection strings (`_PROMPT_INJECTION_PATTERNS` in
`core/agent_workflow.py`). A shape mismatch quarantines as `schema_error` or
`source_error`, both repairable via `repair_of_evidence_id` without
reclaiming the task.

## 6. Repair loop

On `QUARANTINED` with `retry_allowed: true` and `lease_retained: true`, the
task stays `CLAIMED` by the same agent/lease — fix only the fields named in
`validation_errors`, set `repair_of_evidence_id` to the rejected
`evidence_id`, and resubmit before `lease_expires_at`. Capped at
`AGENT_MAX_REPAIR_SUBMISSIONS` (env var, default 2) repair attempts per
lease; beyond that, or past `max_attempts` total attempts, the task moves to
`DEAD_LETTER` and is not automatically reissued.

## 7. What "count" as the Codex agent, for scoring

`core/evidence_scoring.py`'s `score_contradiction()` reads every OTHER
ACCEPTED evidence row for the same `task_id` as `sibling_evidence` and
compares `verdict`/`classification`. The moment a second distinct
`agent_id` has an ACCEPTED submission on a task, this activates
automatically — no changes needed in `core/evidence_scoring.py` or
`core/shadow_evidence_ledger.py`. Two independent, disagreeing verdicts on
the same task will visibly lower that evidence's `contradiction` dimension
and the shadow ledger will show it at `GET /api/ghost/shadow-evidence/scores`.
Two agreeing verdicts score exactly what they'd score alone — agreement
between Claude and Codex is not itself treated as extra evidence quality
(rule 3 in `evidence_scoring.py`'s docstring: contradiction can only
subtract, never inflate).

## 8. Explicitly out of scope here

- Which OpenAI model, prompt, or web-search tool the Codex worker uses —
  implementation detail, not part of the contract.
- `OPENAI_API_KEY` provisioning and the new Railway service itself —
  infrastructure/ownership decision, not a code contract.
- Raising `required_submissions` on issued tasks so consensus is actually
  *required* rather than merely *possible* — that's a change to whatever
  issues `external_mover_triage` tasks (`ghost.external_radar`), tracked
  separately from this worker's build.
