# Control arm v2 (point-in-time), preregistered 2026-10-02

Code: `edge/control_v2.py` (`DESIGN`, `DESIGN_HASH`). The design is frozen. Changing any
part of it makes a new version. A stored design whose hash differs from the code's is
refused (`edge_control_design/control_arm_v2`).

Design hash: `3baa238954712ba2`

This is the **primary** control arm result. Control arm v1 (`docs/control_arm_v1.md`, hash
`605453cd49128631`) keeps running unchanged, with the same numbers, and is reported as
**exploratory (full-session approval, not point-in-time)**.

## Why a new version

v1 labels a name APPROVED if any intraday experiment recorded a forecast on it at any time
that session, and grades every name from `first_seen` + 60 s / + 300 s. A name first seen at
09:45 and approved at 13:00 is graded from 09:46 as approved. The label uses information from
hours after the entry it grades, so v1 measures post-selection, not a decision that could have
been made at the time (audit finding EDGE-10). v1's frozen design can't be edited, so the fix
is this new version.

## Design

- **Population.** Every name the intraday radar detected that session (`edge_radar` rows).
  `first_seen` is the radar's `detected_at`. This is the same as v1.
- **Decision time.** Each comparison is graded from its own decision time, using only what
  was known at its entry:
  - **APPROVED comparison.** The decision time is the `issued_at` of the symbol's earliest
    intraday forecast that session. The entry reference is taken at `issued_at` + 60 s
    ("auto") and + 300 s ("human"). The approval counts because that forecast's `issued_at`
    is at or before the comparison's entry time.
  - **UNAPPROVED comparison.** The decision time is `first_seen`. The entry is at
    `first_seen` + 60 s / + 300 s. It counts only when no intraday forecast on the symbol
    was issued at or before that entry time. Otherwise the variant is recorded as
    `APPROVED_BY_ENTRY` and counted in neither arm.
  - A name approved later is an UNAPPROVED comparison at `first_seen`, because that was its
    state then. From its approval onward it is an APPROVED comparison. Whether a name is
    unapproved is never decided by what happened after its entry.
  - **Approval window.** A decision time outside the radar's approval window (09:45-14:30
    ET, when the intraday experiments can issue) is recorded as `OUTSIDE_APPROVAL_WINDOW`
    and not counted.
- **Entry reference, levels, simulation, variants, data.** These are the same as v1:
  - the close of the last 1-minute bar that ended by decision time + delay (none if that bar
    is more than 5 minutes old)
  - buy stop ×1.002, limit ×1.01, +5% / −3% from the trigger, $1,000 per trade
  - a 20-minute entry window and a 15:30 ET time exit
  - `resolve_execution` at 10 and 25 bps a side, giving `auto_10bps`, `auto_25bps`,
    `human_10bps` and `human_25bps`
  - the day's live feed. v2 uses v1's recorded feed for the day when there is one, and IEX
    and SIP are never pooled.
  - bars requested 10 symbols at a time; truncated data is never kept
- **Unapproved rows are a labelled control.** They are never shown as picks, never
  paper-traded, and never written to `forecasts`.
- **Schedule.** After the close, inside `edge/pipeline.py run()`'s 16:20-20:00 ET window, as
  its own guarded step (`control_v2`) next to v1's step (`control`). A complete day is never
  graded again. Rows are stored as `edge_control_v2/<day>`. Grading starts on the day this
  is deployed. Past sessions aren't backfilled.

## Hypothesis

The approval rules add value. Among radar names, comparisons entered at the moment of an
intraday approval hit the target before the stop more often than comparisons entered when a
name was seen and not yet approved. Both use the same levels and the same delay from their
decision time.

## Analysis

- **Win.** A simulated `WIN`. Filled means `WIN`, `LOSS` or `TIME_EXIT`.
- **Reported** per feed regime and per variant, cumulated over sessions:
  - approved and unapproved win rates, each with its Wilson interval
  - the difference with Newcombe's interval (`edge/stats.py diff_ci`, unchanged)
  - **session-clustered** bootstrap intervals for the difference and for the approved rate
    (`edge/stats.py clustered_diff_ci`). These resample whole sessions with replacement,
    using the same draw for both arms, with 4000 draws and seed 7.
  - expectancy per trade after costs
- **Decision bounds.** Each decision bound is the lower of two bounds, both at alpha 0.05 / 4
  (Bonferroni over the four variants):
  - the independent-rows bound: Wilson, or Newcombe for the difference
  - the session-clustered bound

  Clustering can therefore only make a verdict harder, never easier. The 95% intervals are
  for display.
- **Small samples.** Each rate carries `break_even_verdict`, which reads "too few trades"
  below 30.

## Success rule (identical to v1)

This is judged in one feed regime and one variant, once there are at least **150 approved
fills**. Both of these must hold:

1. The approved-minus-unapproved difference lower bound is above 0 (session-clustered, as
   above).
2. The approved lower bound exceeds the **37.5% break-even after costs**, meaning the higher of
   37.5% and the variant's own after-cost break-even (40.0% at 10 bps, 43.8% at 25 bps).

## Kill rule (identical to v1)

This is judged in one feed regime, once every variant has at least 150 approved fills. If
approved does not beat unapproved in any variant, the approval rules add nothing
demonstrable and should be simplified.

## Other outcomes

- `SELECTION_ONLY` means approved beats unapproved but misses break-even.
- `TOO_FEW` means fewer than 150 approved fills, or no unapproved fills.

## How to read it

Use `ghost_edge_report view=control` (optionally with `day=YYYY-MM-DD`).

- The top level is v2: `headline`, `regimes.<feed>.variants.<variant>` and `day`. Each
  variant gives `difference_ci_95`, `difference_ci_95_clustered`,
  `approved_ci_95_clustered`, `difference_low_decision`, `approved_low_decision` and
  `sessions_with_fills`.
- `exploratory_v1` is v1, labelled "exploratory (full-session approval, not
  point-in-time)", with its numbers unchanged.
- The `summary` view shows `control_arm` (v2) and `control_arm_v1_exploratory`.

## Known limits

- A name approved later contributes one comparison to each arm, at different times on the
  same day. Clustering by session covers that dependence, along with the shared market.
- An eligible name that went unrecorded because of a daily cap is an unapproved comparison,
  as it is in v1. Its radar state and last blocker are stored on the row.
- Approvals come from the intraday experiments only. A premarket (Gap-and-Go) card forecast
  is not an intraday approval.
