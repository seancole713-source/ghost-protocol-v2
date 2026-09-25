# Control arm v1 (observe-all), preregistered 2026-09-25

Code: `edge/control.py` (`DESIGN`, `DESIGN_HASH`). The design is frozen. Changing any
part of it makes a new version. A stored design whose hash differs from the code's is
refused (`edge_control_design/control_arm_v1`).

Design hash: `605453cd49128631`

## Why

Each intraday strategy gets at most about 3 fills a day, so comparing one strategy with
break-even takes many months (audit 2026-09-25, C1). The radar already notices about 40
names a day. If every one of them is graded, approved or not, the question "do the approval
rules pick better stocks than the radar's own unfiltered list?" can be answered in
roughly 40-75 sessions instead of years.

## Design

- **Population.** Every name the intraday radar detected that session (`edge_radar`
  rows). `first_seen` is the radar's `detected_at`.
- **Label.**
  - APPROVED: at least one intraday experiment recorded a forecast on the name that
    session (the `forecasts` store). The experiment ids are stored on the row.
  - UNAPPROVED: every other name. These rows are a labelled control. They are never
    shown as picks, never paper-traded, and never written to `forecasts`.
- **Entry reference.** The close of the last 1-minute bar that ended at or before
  `first_seen + 60 s` ("auto") and `first_seen + 300 s` ("human"). If that bar ended more
  than 5 minutes earlier there is no reference, and the row is recorded as `NO_REFERENCE`.
- **Levels.** The same frozen levels as the intraday specs:
  - buy stop at ref × 1.002, limit ref × 1.01
  - +5% target and −3% stop, both measured from the trigger
  - $1,000 per trade
  - entry window of 20 minutes from the reference time
  - time exit at 15:30 ET
- **Simulation.** `edge/resolver.py resolve_execution` (the conservative stop-limit
  model), at 10 and 25 bps a side. That gives four variants: `auto_10bps`,
  `auto_25bps`, `human_10bps` and `human_25bps`.
- **Data.** 1-minute Alpaca bars from 09:30 to 16:00, on the day's own live feed
  (`edge/feeds.py live_feed`). The feed is recorded on every row. Bars are requested 10
  symbols at a time.
  - If an answer is truncated, that chunk is asked again one symbol at a time.
  - A symbol that is still truncated is recorded as `DATA_TRUNCATED`, and the day stays
    incomplete.
  - A later tick grades only the missing symbols. Truncated data is never kept.
- **Schedule.** After the close, inside the existing 16:20-20:00 ET grading window of
  `edge/pipeline.py run()`, as its own guarded step (`control`). A complete day is never
  graded again. Rows are stored as `edge_control/<day>`.

## Hypothesis

The approval rules add value. Among radar names, approved names hit the target before the
stop more often than unapproved names, when both are traded at the same levels from the
same moment.

## Analysis

- **Win.** A simulated `WIN`, meaning the target was hit before the stop. A filled trade is
  `WIN`, `LOSS` or `TIME_EXIT`.
- **What is reported.** Per feed regime (IEX and SIP are never pooled), per variant,
  cumulated over days:
  - the approved and unapproved win rates, each with its Wilson interval
  - the difference between them, with Newcombe's hybrid-score interval
    (`edge/stats.py diff_ci`)
  - expectancy per trade after costs
- **Small samples.** Each rate also carries the shared `break_even_verdict`, which reads
  "too few trades" below `MIN_FILLED` = 30.
- **Decision intervals.** Decisions use alpha 0.05 / 4 (Bonferroni over the four variants).
  The 95% intervals are for display only.

## Success rule

This is judged in one feed regime and one variant, once there are at least 150 approved
fills. Both of these must hold:

1. The approved-minus-unapproved difference interval excludes 0, with its lower bound
   above 0.
2. The approved Wilson lower bound exceeds the 37.5% break-even **after costs**.

On point 2: 37.5% is the break-even with no costs. Once each side pays its cost, the
break-even rises to 40.0% at 10 bps and 43.8% at 25 bps. The bound must clear the higher
of 37.5% and the variant's own after-cost break-even.

## Kill rule

This is judged in one feed regime, once every variant has at least 150 approved fills. If
approved does not beat unapproved in any variant (the difference lower bound is at or
below 0 everywhere), the approval rules add nothing demonstrable and should be
simplified.

## Other outcomes

- If approved beats unapproved but misses break-even, the result is `SELECTION_ONLY`. The
  filters pick better names, but the names still do not pay at these levels.
- Below 150 approved fills, the result is `TOO_FEW`, and no conclusion is drawn in either
  direction.

## How to read it

Use `ghost_edge_report view=control` (optionally with `day=YYYY-MM-DD`).

- `headline` is the one-line summary, also shown in the `summary` view and in the
  evening log. It uses `human_25bps`, the most conservative variant, and the current
  regime (SIP once any SIP row exists).
- `regimes.<feed>.variants.<variant>` gives the approved and unapproved rates,
  `difference`, `difference_ci_95`, `difference_ci_decision`, `break_even_after_costs`,
  `decision` and `why`.
- `day` holds that session's rows. Unapproved rows are labelled "UNAPPROVED (control, not
  a pick)".

## Known limits

- An approved name is graded from when it was first seen, not from when it was approved.
  That is deliberate: both groups start from the same moment. It also means this measures
  the selection itself, not the approved trade as it was actually recorded.
- Rows on the same day share a market. The intervals treat rows as independent, so they
  are somewhat narrow. The session count is reported next to them.
- An eligible name that went unrecorded because of a daily cap counts as UNAPPROVED. Its
  radar state and last blocker are stored on the row, so it can be examined separately.
  That separate look is not part of the preregistered test.
