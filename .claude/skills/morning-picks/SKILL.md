---
name: morning-picks
description: >
  Run the morning premarket routine: pull Ghost's live radar and picks, search
  the live web for today's biggest premarket movers independently, cross-reference
  the two lists, deep-research every overlap, and report an honest agreement
  verdict per symbol. PRODUCES THE GAP-AND-GO v1 TRADE CARD (frozen rule,
  $1,000 per trade) and logs it to the operator's scoreboard. Also handles the
  intraday follow-up ("what's today's pick update", "are we still on track") and
  the after-close grade ("grade today", "how did we do"). Trigger on:
  "run morning picks", "morning picks", "premarket picks", "today's card",
  "what's moving premarket", "todays pick update", "are we still on track",
  "grade today", "how did we do".
---

# Morning Picks

Claude drives; Ghost does the heavy lifting. Ghost supplies the compute-side
read (radar scans across the full symbol universe, model scores, the
external-mover queue, outcome tracking). Claude supplies what Ghost cannot do
for itself: live web research, news verification, filings, and the final
cross-referenced judgment. The user gets one report, in plain English, that
says where both agree — and is honest about what the numbers do and don't mean.

## Non-negotiable honesty rules

These override everything else in this skill:

1. **Never present an uncalibrated number as a probability.** Ghost's raw
   confidence/score is NOT a win rate: its claimed-70%+ bucket historically
   realizes ~59%, its official high-confidence fired picks are 3/9, and
   Contract 70 is FALSIFIED_AT_CURRENT_DATA with zero registered forward
   outcomes. Always attach this caveat when quoting a Ghost score. Claude's
   own confidence is a judgment, not a measured frequency — label it
   "my read", never "X% probability".
2. **Agreement is the product, not a percentage.** The deliverable per symbol
   is a labeled verdict: `ALIGNED` (both point the same way), `SPLIT`
   (they disagree — say why), or `ONE-SIDED` (only one system sees it).
   When calibration bands mature (15+ resolved samples per band on the
   checklist ledger; a completed Contract 70 forward experiment), real
   percentages replace these labels — not before.
3. **Missing data is reported as missing**, never smoothed over. If short
   interest coverage is degraded (it currently is: free-provider outage),
   say "short-interest data unavailable" — do not treat absence as neutral.
4. **Advisory only.** Never phrase output as an instruction to trade. Levels
   are Ghost's/Claude's reads, not recommendations. The one exception is the
   Mode 0 card: its levels are the frozen rule's mechanical output, stated as
   such — the operator chose the rule and places every order. Note that fabricated-
   feeling RVOL on very thin premarket volume is a known artifact — sanity
   check any RVOL > 5 against absolute share volume before citing it.

## Mode 0 — The Gap-and-Go v1 trade card (THE DELIVERABLE)

Since 2026-09-22 the operator trades ONE frozen rule: **docs/gap_and_go_v1.md**.
Read it at the start of every run. Option A: **$1,000 per trade**, first 20
filled trades. The morning run below exists to fill this card; the research
verdicts feed its catalyst check (rule E4/E5).

**The freeze is absolute.** Never change a threshold, level, ranking or size —
not after a loss, not because a setup "looks close", not because the operator
asks for a pick on a day nothing qualifies. "No setups today — keep the cash" is
a correct card. Any change is v2 with a new ledger, decided only after trade 20.
This rule exists because ten months of re-planning produced zero trades.

**Scoreboard:** https://claude.ai/artifact/6tujj3k9uikamyT8u76xhe — write with the
`ArtifactData` tool (load via ToolSearch). Collections:
- `cards/<YYYY-MM-DD>` — written ONCE, before 9:30am ET, never edited after:
  `{date, issued_at_et, rule:"gap_and_go_v1", size_usd:1000,
    calls:[{symbol, company, move_pct, ref_price, ref_time_et, catalyst,
            source_url, entry_stop, entry_limit, target, stop, shares,
            avg_dollar_volume}],
    excluded:[{symbol, reason}], blind_spots, note}`
- `fills/<date>-<SYM>` — the operator's own entries from the page
  (`placed`, `entry_fill`, `exit_price`). Read-only for Claude.
- `grades/<date>-<SYM>` — written after the close:
  `{date, symbol, outcome, entry_fill, exit_price, shares, pnl_usd, pnl_pct,
    graded_from, graded_at, note}`. outcome ∈ WIN | LOSS | TIME_EXIT |
  NO_FILL | SKIPPED | UNGRADED.

**Edge shadow (read it, don't duplicate it):** `ghost_edge_report` (Ghost MCP)
shows the independent system's own 09:05 card (`view: today`), per-experiment
records with intervals and promotion stage (`view: experiments`), the backtest,
the miss review and the probe. It is shadow/paper only -- useful context for the
operator's card (e.g. "the automated shadow also took SHOP" or "research found
dilution"), never a substitute for the frozen rule.

**Card procedure (target: card written by 9:15am ET):**
1. Candidates = Ghost discovery (`ghost_context` → `discovery_alerts.alerts`,
   gainers only, `observation_kind: intraday_observation`,
   `move_basis: premarket`) ∪ Ghost radar tasks ∪ the independent web sweep.
2. Reference price per name: a timestamped CURRENT-SESSION premarket price
   ≤ 30 min old. `ghost_symbol_quote` counts only when
   `session_price_basis == "current_session"` — `prior_session_trade` is
   yesterday's print and has no gap (the 2026-09-22 bug). Otherwise use the
   discovery row's price if `source_age_s` ≤ 1800. No fresh price → excluded.
3. Check E1–E3 with code, never by eye:
   `python3 -c "import core.gap_and_go as g; print(g.eligibility_failures(move_pct=..., price=..., avg_shares=..., avg_dollars=..., has_dated_catalyst=..., dilutive_or_mechanical=...))"`
   E4 (dated company catalyst, last 24h, with a source link — sector
   sympathy does NOT count) and E5 (offering/ATM/dilution, ex-div, split) come
   from the research in Steps 2–4 below.
4. Rank the eligible by average daily dollar volume; take the top 2.
5. Levels, from code only: `python3 -m core.gap_and_go levels <ref>`.
6. Write `cards/<date>` (one `set`). List every checked-and-excluded mover with
   its failing rule in `excluded`; name what Ghost could not see or price in
   `blind_spots`.
7. Tell the operator, per call: symbol, catalyst in one line, buy stop / limit,
   target, stop, shares, max loss — and the two manual duties: **cancel an
   unfilled entry at 9:30am CT (10:30 ET)**, **sell anything open at 2:30pm CT
   (3:30 ET)**. The operator works in CENTRAL time: the market opens 8:30am CT.
   Always lead with CT. Link the scoreboard. These are the frozen rule's levels, not Claude's opinion;
   the operator decides and places every order.

**Grading ("grade today", after 4pm ET):** for each call, read
`fills/<date>-<SYM>` first. Reported fills win:
`core.gap_and_go.grade_from_fills(...)`. Otherwise fetch the day's
open/high/low (and the ~3:30pm price if neither level hit) and use
`python3 -m core.gap_and_go grade ...` → `graded_from: "daily_bar_estimate"`.
A bar touching both levels is LOSS. `placed: false` → SKIPPED. Write
`grades/<date>-<SYM>` in one `batch`. Report the running record, win rate vs
the 37.5% break-even, and net P&L vs the −$300 pause line. If the pause line is
reached, say so first and do not issue another card until the operator reviews.

## Mode 1 — Morning run ("run morning picks")

### Step 0: Connect check
`ToolSearch` for `mcp__Ghost__` tools (e.g. `ghost_picks`, `ghost_agent_tasks`,
`ghost_kill_status`). If Ghost's connector is not available in this chat, say
so plainly — "Ghost isn't connected in this chat; toggle it on in the
connector settings and ask again" — then still run Steps 2–3 (the web half)
so the user gets something, clearly labeled as Claude-only.

### Step 1: Pull Ghost's morning state (parallel where possible)
- `ghost_kill_status` — is the official engine paused? (It often is, by its
  own safety design. If paused, official picks are absent and that is
  correct behavior — say so, don't work around it.)
- `ghost_picks` — any active official picks with entry/target/stop.
- `ghost_agent_tasks` with status PENDING — the external-mover radar queue.
  Each task's `request_payload.observation` carries premarket ground truth:
  `observed_price`, `prior_close`, `observed_current_move_pct`,
  `observed_peak_move_pct`, `observed_rvol`, `session_volume`,
  `avg_daily_volume`, `market_data_as_of`. This is Ghost's premarket scan
  output — use it as the Ghost-side candidate list.
- `ghost_agent_workflow_health` — sanity: worker online, queue depth.

### Step 2: Independent web sweep (do NOT look at Ghost's list first —
independence is the point)
WebSearch for today's premarket movers: "biggest premarket gainers today",
"premarket movers [today's date]", plus a news-driven pass ("premarket
earnings movers today"). Build Claude's own top 5–10 candidate list with the
% move and the claimed catalyst for each.

### Step 3: Cross-reference
Three buckets:
- **Overlap** (both lists) — these are the day's real candidates.
- **Ghost-only** — radar caught it, web sweep didn't surface it. Check each
  briefly: thin-float artifact (see honesty rule 4) or a genuinely early
  catch? Say which.
- **Claude-only** — web shows it moving, Ghost's radar missed it (off-universe
  symbol, or scan timing). Flag these back: if the symbol is outside Ghost's
  universe (`ghost_symbol_universe`), note it as a coverage gap.

### Step 4: Deep-research every overlap (and the strongest single from each
one-sided bucket)
For each: WebSearch/WebFetch the actual catalyst — earnings numbers vs.
expectations, the filing itself where possible (SEC/EDGAR/IR page beats a
news aggregator), float/dilution context (recent reverse splits and offerings
explain "moves" that aren't catalysts — the BRNX lesson). Classify:
`earnings_gap` / `news_breakout` / `short_squeeze` / `momentum_anomaly` /
`unknown`.

**Feed the machine while you're at it:** if a researched symbol has a PENDING
Ghost agent task, claim it (`ghost_agent_claim_task`) and submit the research
as evidence (`ghost_agent_submit_evidence`) with proper `{kind, locator}`
source_refs and an honest verdict (use "insufficient" freely — a forced
confident verdict poisons the calibration data this routine exists to build).
Every resolved submission moves the system closer to real percentages.

### Step 5: The report
Per candidate, this shape (plain English, no jargon without an inline
explanation):

```
TICKER — Company (what it does, one clause)
  Move: +22% premarket ($4.10 → $5.02), volume 3.1M vs 0.9M avg
  Catalyst: Q2 beat — revenue $480M vs ~$463M expected (verified: company 8-K)
  Ghost:   radar flagged at +19%, RVOL 2.9 [raw score if an official pick exists,
           with the calibration caveat]
  My read: real catalyst, primary-source verified; main risk is X
  Verdict: ALIGNED — both point up
  Levels:  Ghost target $5.40 / stop $4.60 (if an official pick exists);
           my research level: resistance ~$5.25 (prior gap fill)
```

Close the report with: engine status (paused/live), data-quality notes
(short-interest coverage, anything degraded), and the standing line that
verdicts are advisory and percentages arrive only when the calibration
ledger earns them.

## Mode 2 — Intraday update ("what's today's pick update")

1. Re-pull live state for the morning's candidates: `ghost_symbol_quote`
   (per-symbol, works for any ticker — official watchlist or not) for a live
   price/gap read, plus a quick web check for any new headline on each.
   Note: `ghost_score` is NOT a per-symbol tool — it's Ghost's own
   parameterless WOLF cockpit score, always. Don't call it with a `symbol`
   argument expecting a different ticker back.
2. Compare against the morning report: current price vs. morning price,
   peak so far, direction still intact?
3. Report per symbol: "still on track" / "reversed" / "new information",
   with the morning call and the actual tape side by side — including when
   the morning read was wrong. Wrong calls get stated plainly; they are
   calibration data, not embarrassments.
4. If Ghost issued or resolved an official pick since morning, surface it
   with its outcome.

## Mode 3 — End of day (optional, "how did we do")

Compare every morning verdict against the close: direction right/wrong,
peak vs. cited levels. State the running tally honestly (e.g. "this week:
4 aligned calls, 3 went our way"). This tally is the raw material for the
real confidence percentages — treat it as sacred: never retro-edit a
morning call.

## Failure modes

- **Ghost connector off** → run the Claude-only half, label it as such,
  tell the user how to re-enable.
- **Ghost engine paused** → normal and expected; radar/mover data still
  flows. Never present the pause as an error — it is the safety system
  working.
- **Web sources conflict on a number** → quote both with sources; do not
  average.
- **No overlap between lists** → say so. "No agreement today" is a valid,
  honest morning report and better than a forced pick.
