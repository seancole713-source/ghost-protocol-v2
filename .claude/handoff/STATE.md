# Ghost operator state (handoff, updated 2026-10-03 ~1:30 am CT)

Read this first in a new chat. No secrets in this file.

## Standing rules (from the operator)
- Paper only; real money only by the operator's hand. Risk $1,000/trade. Times in Central first.
- Never loosen a gate/threshold to make a small sample look fixed; a changed frozen rule is a new version.
- Merge/deploy freely, but never 8:00-9:00 am CT or 2:20-2:50 pm CT; deploy freeze 08:00-16:30 ET on trading days.
- Railway: full access; variable NAMES only, never read or print secret values. Never disable TLS / unset HTTPS_PROXY.
- Don't pay for data until code is proven. Every paid eval run needs operator approval of inputs, grading and cost.
- Commit to branch `claude/admiring-euler-1BIUC`; reset it to origin/main after each merge.

## Infra
- Railway project tender-benevolence f910dbba-dc10-4a8b-b654-28001e64f4ec, env production a036418b-07aa-424d-8b66-8b3bc5be9a11
- ghost-protocol-v2 (main app) 98593080-065d-43ef-840c-4a3d36a1b572 - live on fa25e4b (PR #233) since 12:48 am CT 10/3
- ghost-claude-worker 07be5c2d-10d2-4e62-ab83-31190671788d - CLAUDE_WORKER_MODEL=claude-sonnet-5-5 since 12:55 am CT 10/3 (confirmed in log)
- ghost-codex-worker ae90bcbe-64bf-4170-b07d-437f11755102 - disabled by design (CODEX_WORKER_ENABLED=false)
- Edge research: claude-opus-5-5 author, OpenAI gpt-6-sol reviewer, cap EDGE_RESEARCH_DAILY_USD=4

## Research eval (.claude/hillclimb/ghost-research/)
- Runner run_eval.py calls the real research_symbol; grading.py is code-only; cases.jsonl.
- Scored: r01-r07 (labels come from Ghost's own records -> operator spot-check pending).
- Held (scored=false): m01-m07 (need labels/times), d01-d05 dilution (need exact publish times; PMTS is a non-dilution trap).
- Operator approved: Claude reviewer; 3-case pilot (~$1.30) with harness approval.
- Pilot command (needs ANTHROPIC_API_KEY in the Claude Code environment, new chat):
  `python .claude/hillclimb/ghost-research/run_eval.py --ids r01,r03,r07 --reps 1 --approve-harness`
- Later option: same cases on claude-sonnet-5-5 / claude-fable-5-1 for comparison (needs approval).

## Open items
- Monday 10/5: confirm first card issues on fixed code; /api/readiness ready; no new dead letters; Claude worker tasks complete on Sonnet 5.5.
- #57 paper-test partial-fill OCO on Alpaca paper during market hours.
- #59 missed-mover recall (VEEA no-forecast bug, MEDS catalyst miss, rvol_tod history, IEX no-print).
- #65 replay drift: HOOD 9/30 ELIGIBLE->REJECTED, VKTX 9/23.
- #58 vwap_reclaim@v1; #60 catch-up report 9/28-10/1; #63 borrow-fee source (IBKR timeout, iBorrowDesk refused).
- Operator-only: SIP plan (probe: 403 "subscription does not permit recent SIP data"); GitHub secrets CRON_SECRET + TEST_DATABASE_URL; rotate Postgres credential; replace weak MODEL_BLOB_HMAC_KEY; claim cloud credit by Oct 7.
- scan_coverage readiness reads degraded (too few symbols with usable evidence) - real signal while SIP is off.
