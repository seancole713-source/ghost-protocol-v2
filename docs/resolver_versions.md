# Resolver versions

The bar resolver (`edge/resolver.py`) turns minute bars into the **forecast** and
**simulated** outcome records. It is versioned so a change in how outcomes are
judged is never silently mixed into the record it changes.

* The current version is `RESOLVER_VERSION` in `edge/resolver.py`.
* Every resolution it makes carries `resolver_version`; it is stored on each new
  `outcomes` row (forecast and simulated records), on each newly graded
  `edge_card_outcomes` row, on each `edge_control` day graded from now on, and on
  each new backtest summary.
* A row **without** `resolver_version` was made by `resolver_v1`.
* Versions are **cohorts, never pooled**: see "Cohort rule" below.
* **Stored outcomes are never re-graded.** A terminal row (WIN, LOSS, TIME_EXIT,
  NO_FILL, EXCLUDED) keeps the outcome and the version it was written with. Only
  UNRESOLVED rows (data missing) are resolved again, and those are resolved by the
  current version. `resolve_day` skips any record that is already final, so a
  newer version cannot collide with, or overwrite, an older final record.
* The operator's frozen rules (Gap-and-Go v1 +5% / −3%, 37.5% break-even,
  `promotion_v1`, `retirement_v1`, `control_arm_v1`) are untouched: the resolver
  version is not part of any frozen spec or design hash.

## Cohort rule (audit NEW-02)

Outcomes graded by different resolver versions are **never pooled**, exactly as IEX
and SIP feed regimes are never pooled. History stays as it was written; only what
is *read* together changes.

* `edge/ledger.py resolver_of(outcome)` is the version of a stored row: its
  `resolver_version`, else `resolver_v1`. It mirrors `Ledger.feed_of`.
* **A forecast's cohort** (`resolver_cohort` / `Ledger.cohort_of`) is the version
  that finalized its resolver-graded records (`forecast`, `simulated`). A forecast
  with no final record yet (pending, UNRESOLVED) is in the current cohort, because
  the current resolver will grade it. One whose two records were finalized by
  different versions is a separate "mixed" cohort (e.g. `resolver_v1+resolver_v2`),
  never the current one. The broker's `actual` record is not a resolver grade and
  carries no version; it is reported with its forecast's cohort, so a cohort's three
  records always describe the same forecasts.
* **The headline is one cohort:** `Ledger.report()`'s `records` (and the flat
  simulated view) are the current resolver's cohort (`resolver_version` in the
  report) inside the current feed regime. Every older cohort is reported beside it
  under `other_resolvers`, labelled, e.g. "resolver_v1 (legacy: fill-bar target
  touches may be optimistic; missing data finalized as NO_FILL)". The other feed
  regime is split the same way (`other_regimes.<feed>.other_resolvers`).
* **Everything that judges reads only the current cohort:**
  * promotion (`promotion_v1`) and retirement (`retirement_v1`) read the report's
    headline, and `edge/readout.py _by_day` (the day-clustered sample) keeps only
    the current cohort. A report of any other cohort is never judged on: promotion
    lists it as unmet and retirement declines to recommend.
  * the ledger's calibration bands are computed per cohort.
  * model training, isotonic calibration and the walk-forward test
    (`edge/models.py training_rows`) use only rows whose `resolver_version` is the
    current resolver; backtest dataset rows now carry it, and an untagged row (the
    stored `gap_and_go_backtest_v7` dataset) is resolver_v1 and never used.
  * the card scorecard and its Top 10 comparison (`edge/scorecard.py`) compare only
    current-resolver rows; card rows graded before resolver_v2 have no
    `resolver_version`, are resolver_v1 and are reported under `other_resolvers`.
    The Top 10 view shows each entry's grading version.
  * both control arms (`edge/control.py` and `edge/control_v2.py` `summary()`):
    `regimes`, the headline and every SUCCESS/KILL decision read only rows graded by
    the current resolver, within the feed regime chosen as before. A row's version is
    its own `resolver_version`, else its day's, else resolver_v1 (a day graded before
    the version was recorded). Older cohorts are reported under `other_resolvers`,
    labelled legacy, and decide nothing. Each newly graded control row now records
    its version; when a partial day is completed later, rows it keeps (never
    re-graded) are labelled with the version their day recorded, so the day's new tag
    cannot relabel them. The `control_arm_v1` / `control_arm_v2` designs and hashes
    (605453cd49128631 / 3baa238954712ba2) are unchanged.
* This is a measurement correction. It can only shrink and clean the sample; no
  threshold moves. The `promotion_v1` and `retirement_v1` criteria dicts and their
  hashes are unchanged; the cohort filter lives in the data selection, not in them.
  After the 2026-10-02 switch the current cohort starts small, so every count in
  promotion and retirement (trades, sessions) restarts from the resolver_v2 record.

## resolver_v1

Everything resolved before 2026-10-02. The rules are in the `edge/resolver.py`
module docstring.

## resolver_v2 (2026-10-02)

Two changes, both in the conservative direction. Neither applies to rows already
stored.

1. **Fill-bar target touches need an established order (audit EDGE-02).** A minute
   bar cannot show its own path. A target touch in the bar that filled the entry now
   counts only when the fill provably came first: the fill was the bar's first
   trade (the bar opened at or below the limit), or the bar climbed from below
   through the trigger. A bar that opened **above** the limit and filled on a dip
   back to it may have printed its high (often the open) before the fill. Example
   from the audit: O=H=11.00, L=C=10.20, limit 10.20, target 10.61. resolver_v1
   graded that a clean WIN. resolver_v2 does not count the touch, marks the record
   `ambiguous`, and lets later bars decide. A later stop or time exit is an
   ambiguous LOSS or TIME_EXIT; a later bar reaching the target is a WIN, because
   that touch is after the fill whatever the fill bar did. The forecast record
   (market entry at the trigger) is unchanged: when a bar gaps through the trigger,
   its entry is the open, so the whole bar comes after it.
2. **A missing entry window is UNRESOLVED unless the data is known complete
   (audit EDGE-03).** No bar between the window start and the entry expiry used to
   finalize as a terminal NO_FILL, even when the bars were simply missing.
   `resolve_day` and the card scorecard now read bars through `bars_pages` and pass
   the provider's completeness flag to the resolver. With no entry-window bars,
   the result is NO_FILL only when the provider said its answer was whole
   (`complete=True`); with a truncated or unknown answer it is UNRESOLVED and is
   retried on the next tick. The control arm passes `complete=True` because its
   fetch never keeps a truncated answer.

### Not re-run under v2

The stored backtest summaries (`gap_and_go_backtest_v7`,
`post_split_momentum_backtest_v1`) were made by resolver_v1 and are kept as they
were. A backtest run from now on records `resolver_version` in its summary. To
measure history under resolver_v2, bump the backtest version (a new record), as
v4 did for the earlier resolver change. That would also re-run overnight model
training, so it is the operator's decision.
