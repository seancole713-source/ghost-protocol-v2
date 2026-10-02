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
* **Stored outcomes are never re-graded.** A terminal row (WIN, LOSS, TIME_EXIT,
  NO_FILL, EXCLUDED) keeps the outcome and the version it was written with. Only
  UNRESOLVED rows (data missing) are resolved again, and those are resolved by the
  current version. `resolve_day` skips any record that is already final, so a
  newer version cannot collide with, or overwrite, an older final record.
* The operator's frozen rules (Gap-and-Go v1 +5% / −3%, 37.5% break-even,
  `promotion_v1`, `retirement_v1`, `control_arm_v1`) are untouched: the resolver
  version is not part of any frozen spec or design hash.

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
