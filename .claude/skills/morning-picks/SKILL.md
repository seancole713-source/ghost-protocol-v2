---
name: morning-picks
description: >
  Run the morning premarket routine: pull Ghost's live radar and picks, search
  the live web for today's biggest premarket movers independently, cross-reference
  the two lists, deep-research every overlap, and report an honest agreement
  verdict per symbol. Also handles the intraday follow-up ("what's today's pick
  update", "are we still on track") by re-pulling live prices and comparing them
  against the morning read. Trigger on: "run morning picks", "morning picks",
  "premarket picks", "what's moving premarket", "todays pick update",
  "are we still on track".
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
   are Ghost's/Claude's reads, not recommendations. Note that fabricated-
   feeling RVOL on very thin premarket volume is a known artifact — sanity
   check any RVOL > 5 against absolute share volume before citing it.

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
