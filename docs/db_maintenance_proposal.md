# DB maintenance proposal: `ghost_perf_symbol_evals` and `prediction_points`

Status: PROPOSAL. Nothing here has been run against production, and nothing has been deleted.
Every step below is for an operator to run off-peak, after a backup.

Observed in production: `ghost_perf_symbol_evals` is about 6.3 GB, and `prediction_points` is
an empty heap with about 168 MB of indexes.

## 1. What writes `ghost_perf_symbol_evals`

The only writer is `core/performance_log.py::log_prediction_cycle`. The prediction cycle calls it
once per cycle (`core/prediction.py`, from the `market_scan` scheduler job in `wolf_app.py`).

| | |
|---|---|
| Rows per cycle | one per watchlist symbol (`STOCK_SYMBOLS`, about 43) |
| Cadence | `SCAN_INTERVAL_MARKET_MIN` (default 30 min) in pre-market and RTH; `SCAN_INTERVAL_OFFHOURS_MIN` (default 60 min) otherwise, weekends included |
| Volume | about 33 cycles/day x 43 = about 1.4k rows/day, so about 130k rows across a 90-day window |
| Row payload | 21 columns plus `scores` JSONB, already trimmed by `_trim_scores` to the fields used by shadow seeding, calibration and lineage |
| Retention | `maybe_prune` deletes `ghost_perf_cycles` older than `GHOST_PERF_RETENTION_DAYS` (default 90, minimum 7). Evals go too via `ON DELETE CASCADE`. It runs on about 1 in 17 cycles. |

130k rows should not take 6.3 GB (that would be about 48 KB per row). The likely causes are:

1. **Every row was written twice (fixed in code by this change).** The engine sets
   `scores["confidence_final"] = None` on non-fired rows, so almost every eval was inserted with
   `confidence_final` NULL. `ensure_perf_tables` runs on every cycle and executes
   `UPDATE ... SET confidence_final=confidence WHERE confidence_final IS NULL AND confidence IS NOT NULL`.
   That update rewrote every new row once, leaving a dead tuple each time. It also ran a full
   sequential scan of the multi-GB heap every 30 to 60 minutes, because no index covers it. Each
   cycle also ran `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` four times, and each of those takes
   an ACCESS EXCLUSIVE lock.
2. **Bloat is never returned to the OS.** `DELETE` (pruning) and the double-write leave dead space.
   Autovacuum makes that space reusable but does not shrink the file. Rows written before
   `_trim_scores` existed (full event and checklist payloads) were pruned, but their pages are
   still allocated.
3. **A possible retention override.** Check whether `GHOST_PERF_RETENTION_DAYS` is set above 90 in
   Railway. The measurement queries below answer this without reading any env value.

### Code change in this branch (no data touched)

- `symbol_eval_from_scan` now writes `confidence_final = confidence` when the engine passes None.
  That is the same value the backfill would have written later, so the end state is identical and
  each row is written once.
- `log_prediction_cycle` runs the schema DDL once per process and never runs the backfill.
  `core.db` init still runs `ensure_perf_tables(cur)` with the backfill at boot, so historical rows
  are still filled.
- Tests: `tests/test_perf_symbol_evals_writes.py`.

## 2. Who reads it, and how far back

| Reader | Window |
|---|---|
| `core/shadow_outcomes.py::seed_shadow_rows` (hourly, and after scans) | last 3 days (`eval_ts >= cutoff`) |
| `core/watcher.py::watcher_summary` (15-min job, API) | 30 days by default (API allows up to 365, but the data is capped by retention) |
| `/api/wolf/performance-log/*`, `core/daily_report.py`, `wolf_app._top_scan_candidates` | one `cycle_id`, or a per-symbol history with LIMIT |
| `core/contract_70_slices.py` | point lookup `(symbol, eval_ts)` as a fallback for shadow rows written before the durable `regime_label`/flag columns existed (added 2026-07-14) |

Nothing needs raw rows older than about 30 days, except the contract-70 fallback for pre-2026-07-14
shadow rows. Those rows are about to leave the current 90-day window anyway. `ghost_shadow_outcomes`
already keeps the durable copy and is never pruned.

**Missing index:** the two windowed readers (`seed_shadow_rows`, `watcher_summary`) filter on
`eval_ts` alone. The only indexes are `(cycle_id)` and `(symbol, eval_ts DESC)`, so both of them
sequentially scan the whole table.

## 3. Recommendation

1. **Measure first** (read-only):
   ```sql
   SELECT pg_size_pretty(pg_relation_size('ghost_perf_symbol_evals'))       AS heap,
          pg_size_pretty(pg_total_relation_size('ghost_perf_symbol_evals')
                         - pg_relation_size('ghost_perf_symbol_evals')
                         - pg_indexes_size('ghost_perf_symbol_evals'))     AS toast,
          pg_size_pretty(pg_indexes_size('ghost_perf_symbol_evals'))        AS indexes;
   SELECT n_live_tup, n_dead_tup, last_autovacuum, last_vacuum
     FROM pg_stat_user_tables WHERE relname = 'ghost_perf_symbol_evals';
   SELECT to_timestamp(MIN(eval_ts)) AS oldest, COUNT(*) AS rows,
          AVG(pg_column_size(scores))::int AS avg_scores_bytes
     FROM ghost_perf_symbol_evals;
   ```
   If `oldest` is more than 90 days ago, retention is being overridden or pruning is not running.
2. **Back up** before any change: `pg_dump -Fc -t ghost_perf_symbol_evals -t ghost_perf_cycles`,
   or a Railway volume snapshot.
3. **Aggregate, then shorten retention.** Add an additive daily rollup table:
   `ghost_perf_eval_daily(trade_date, symbol, skip_code, n, n_fired, avg_up_prob, max_up_prob, last_eval_ts)`.
   Fill it for every day still present, then lower `GHOST_PERF_RETENTION_DAYS` from 90 to 45.
   45 days covers the 30-day watcher default with margin. Do not go below 35 until
   `ghost_shadow_outcomes` has its durable regime columns filled for every row the contract-70
   search still uses. A one-time `UPDATE ghost_shadow_outcomes SET regime_label = ... WHERE regime_label IS NULL`
   from the evals still present closes that gap. Pruning stays the existing
   `DELETE FROM ghost_perf_cycles WHERE cycle_ts < cutoff` (cascade). No historical outcome, pick
   or shadow row is touched.
4. **Index:** `CREATE INDEX CONCURRENTLY idx_perf_evals_eval_ts ON ghost_perf_symbol_evals (eval_ts);`
   Run it by hand. Do not put it in `ensure_perf_tables`, because a plain `CREATE INDEX` on this
   table blocks writes while it builds.
5. **Reclaim space** off-peak (weekend, outside 03:00 to 20:00 ET):
   - `VACUUM (VERBOSE, ANALYZE) ghost_perf_symbol_evals;` (not FULL). This makes dead space reusable
     and stops growth. It only gives disk back from the end of the file.
   - `REINDEX TABLE CONCURRENTLY ghost_perf_symbol_evals;` (PG 12+). This rebuilds the bloated
     indexes without blocking writes.
   - To actually shrink the heap, use `pg_repack` if the extension is available (online), or
     `VACUUM FULL` only in an announced maintenance window. VACUUM FULL takes ACCESS EXCLUSIVE and
     the scan loop would block.

## 4. `prediction_points` (empty heap, about 168 MB of indexes)

No code in this repository references `prediction_points`, and neither does its git history. It is
a legacy table from an earlier version, and nothing in the app writes or reads it. Its indexes are
bloat left over from rows deleted long ago.

1. Confirm it is unused:
   ```sql
   SELECT seq_scan, idx_scan, n_tup_ins, n_tup_upd, n_tup_del, n_live_tup
     FROM pg_stat_user_tables WHERE relname = 'prediction_points';
   SELECT indexrelname, idx_scan, pg_size_pretty(pg_relation_size(indexrelid))
     FROM pg_stat_user_indexes WHERE relname = 'prediction_points';
   ```
   Also check that no other service (another Railway service, BI tool or notebook) uses this
   database.
2. Back up the schema: `pg_dump -s -t prediction_points`.
3. Choose one:
   - **Lowest risk:** `REINDEX TABLE CONCURRENTLY prediction_points;`. The indexes of an empty table
     rebuild to a few KB each, which reclaims about 168 MB with no behavior change.
   - **Then, after a hold period with `idx_scan`/`n_tup_ins` still 0:** `DROP INDEX CONCURRENTLY`
     on each non-primary-key index, and later drop the table itself.

## 5. Order of operations

1. Deploy this branch. It stops the double-write and the per-cycle full scan.
2. Run the measurement queries, then take a backup.
3. Off-peak: `CREATE INDEX CONCURRENTLY` on `eval_ts`, then `VACUUM (ANALYZE)`, then
   `REINDEX TABLE CONCURRENTLY` on both tables.
4. Add the daily rollup, then lower retention to 45 days. The next prunes drop the older rows
   through the existing path.
5. Optional: `pg_repack` (or VACUUM FULL in a maintenance window) to return the disk.
