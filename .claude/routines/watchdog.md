# Ops watchdog -- hourly, 8:20am-3:20pm CT, weekdays

## Check
1. Latest deployment status (`list-deployments`, limit 2).
2. Logs since the last watchdog run (about 1 hour), filtered: `ERROR`, `Traceback`,
   `job failed`, `timed out`, `EDGE_SHADOW`, `EDGE_PROBE_CAP`.
3. Is what should have happened by now today there? (ET clock)
   06:00 universe · 07:00 miss review · 08:30 research (if EDGE_RESEARCH_ENABLED) ·
   09:05-09:28 card + paper submit · 09:45-14:30 intraday ticks · 10:30 entry cancels ·
   15:30 time exit · 16:20 grading, card_graded, reconcile.
4. Known and NOT bugs (do not "fix"): Alpaca SIP 403 and Polygon snapshot 403 (plan
   limits), borrow sources unreachable (IBKR FTP / iBorrowDesk), yfinance circuit
   breakers in Ghost's old squeeze monitor, strategies DEGRADED on IEX quotes.

## Healthy
Reply one line: "watchdog <time CT>: healthy -- <what was checked>". No push.

## Broken
1. Diagnose from logs + code (`git fetch origin main`; work on the session's designated
   branch, restarted from origin/main if its PR already merged).
2. Fix ONLY if small and clearly correct: add a test that fails before the fix,
   `python -m pytest -q` fully green, and the isolated edge suite
   (`python -m pytest -q --noconftest tests/test_edge_*.py -k "not mcp_tool"`) green.
   Open a PR, wait for the `test` and `edge-alone` checks, squash-merge.
3. NEVER merge (a deploy restarts Ghost) between 8:00-9:00am CT (card + paper orders)
   or 2:20-2:50pm CT (time exits). In those windows leave the PR open and say so.
4. After merge: confirm the deploy is SUCCESS and the error stopped.
5. Do NOT fix -- report instead -- anything large or ambiguous, or touching frozen
   specs/gates/promotion criteria, real money, secrets or Railway settings.
6. Push: what broke, what was done, what the operator must do (if anything).
