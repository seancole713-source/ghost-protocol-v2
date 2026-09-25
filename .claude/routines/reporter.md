# Evening reporter -- 3:40pm CT (4:40 ET), weekdays

Grading, the card counterfactuals and paper reconciliation run from 3:20pm CT.

1. Read today's `paper`, `experiments`, `scorecard`, `radar`, `control` views and today's `today`
   (connector, else `EDGE_VIEW` logs since 8:00am CT). Not a trading day: no reply, stop.
   Evening views missing by 3:40pm CT: check `EDGE_SHADOW` / errors and report a PROBLEM.
2. Report, plain English, CT times:
   - What the shadow picked this morning and what each did (simulated), and the
     PAPER fill vs the simulation (slippage, no-fill, rejection).
   - Intraday radar: forecasts issued, and why the rest expired (top reasons).
   - Running record per experiment: trades, win rate WITH its interval vs the 37.5%
     break-even, and the promotion gate's "still needs".
   - AI scorecard lines (keyword filter, AI research, model): the verdicts as written --
     "not enough data" is the honest answer for weeks.
   - Control arm: quote the `control` view's `headline` as written (approved vs unapproved
     radar names at the same frozen levels, one feed regime; "TOO_FEW" for weeks). Never list
     UNAPPROVED names as picks.
   - Anything a watchdog run flagged today.
3. Grade the operator's scoreboard (audit F03: it was never graded, so the 20-trade decision
   and the -$300 pause line could never trigger). Scoreboard:
   https://claude.ai/artifact/6tujj3k9uikamyT8u76xhe via `ArtifactData`. For every
   `cards/<date>` call (today, and any earlier card with a call but no `grades/<date>-<SYM>`):
   - `fills/<date>-<SYM>` exists: `placed: false` -> SKIPPED; else
     `core.gap_and_go.grade_from_fills(...)` (graded_from "operator_fill").
   - No fill record: grade the RULE, not the operator. If the same symbol and levels were on
     Ghost's own card that day, use its minute-bar outcome from the `paper` view
     (`simulated`, graded_from "edge_minute_bars"); otherwise the day's open/high/low with
     `python3 -m core.gap_and_go grade ...` (graded_from "daily_bar_estimate"; say where the
     bar came from).
   - Write all grades in one `batch`: `{date, symbol, outcome, entry_fill, exit_price, shares,
     pnl_usd, pnl_pct, graded_from, graded_at, note}`.
   - Report the running record (graded calls of 20), win rate with its interval vs 37.5%, and
     net P&L vs the -$300 pause line. Pause line reached: say it FIRST and push it.
4. If `ghost_edge_note` is available: one note, kind=report, author=evening-reporter.
5. Push a one-line summary. No hype; small samples are called small.
