import logging
import os
import time

import psycopg2
import psycopg2.pool
from core.quiet import note_suppressed
from typing import Optional

LOGGER = logging.getLogger("ghost.db")
_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None
_POOL_MIN = int(os.getenv("DB_POOL_MIN", "2"))
_POOL_MAX = int(os.getenv("DB_POOL_MAX", "25"))
_GETCONN_RETRIES = max(1, int(os.getenv("DB_POOL_GET_RETRIES", "4")))
_GETCONN_RETRY_DELAY_S = float(os.getenv("DB_POOL_RETRY_DELAY_S", "0.12"))


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# U69: psycopg2's pool raises the instant it is exhausted. Request threads
# (anyio, 40), the default executor (scheduler sync jobs + monitors) and
# background threads can together exceed DB_POOL_MAX, so a short burst used to
# surface as 503s after ~0.7 s of retries. Callers now wait up to
# DB_POOL_WAIT_S for a slot before failing.
_GETCONN_WAIT_S = max(0.0, _env_float("DB_POOL_WAIT_S", 5.0))


def _session_timeout_options() -> str:
    """libpq ``options`` for pooled sessions (U05).

    Without them one hung statement or lock wait pins a pool slot -- and a
    scheduler job, whose thread cannot be cancelled -- indefinitely.
    DB_STATEMENT_TIMEOUT_MS (default 300000 = 5 min) and DB_LOCK_TIMEOUT_MS
    (default 120000 = 2 min); 0 disables either. Only pooled connections get
    them: the leader-lock session holds its advisory lock outside the pool.
    """
    parts = []
    for env, default, guc in (
        ("DB_STATEMENT_TIMEOUT_MS", 300_000, "statement_timeout"),
        ("DB_LOCK_TIMEOUT_MS", 120_000, "lock_timeout"),
    ):
        try:
            ms = int(os.getenv(env, str(default)))
        except (TypeError, ValueError):
            ms = default
        if ms > 0:
            parts.append(f"-c {guc}={ms}")
    return " ".join(parts)


def _pool_dsn(dsn: str) -> str:
    """DATABASE_URL plus the session timeouts, merged with any existing options."""
    opts = _session_timeout_options()
    if not opts:
        return dsn
    try:
        from psycopg2.extensions import make_dsn, parse_dsn

        existing = (parse_dsn(dsn).get("options") or "").strip()
        return make_dsn(dsn, options=(existing + " " + opts).strip())
    except Exception as exc:  # unparseable DSN: never block boot on this
        LOGGER.warning("DB session timeouts not applied (dsn parse: %s)", type(exc).__name__)
        return dsn


def init_db():
    global _pool
    dsn = os.environ["DATABASE_URL"]
    pool_dsn = _pool_dsn(dsn)
    try:
        _pool = psycopg2.pool.ThreadedConnectionPool(_POOL_MIN, _POOL_MAX, dsn=pool_dsn)
    except psycopg2.OperationalError as exc:
        if pool_dsn == dsn:
            raise
        # A pooler that rejects the libpq "options" startup parameter must not
        # take the app down: fall back to the bare DSN and say so.
        LOGGER.warning(
            "DB pool with session timeouts failed (%s); retrying without them",
            type(exc).__name__,
        )
        pool_dsn = dsn
        _pool = psycopg2.pool.ThreadedConnectionPool(_POOL_MIN, _POOL_MAX, dsn=dsn)
    LOGGER.info(
        "DB pool ready (min=%s max=%s session_timeouts=%s)",
        _POOL_MIN, _POOL_MAX, "on" if pool_dsn != dsn else "off",
    )
    _ensure_tables()
    _migrate_schema()


def _on_event_loop_thread() -> bool:
    try:
        import asyncio

        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def get_conn():
    if not _pool:
        raise RuntimeError("Call init_db() first")
    last_err: Optional[Exception] = None
    # Never park the event loop: a coroutine that blocks here could be waiting
    # on a slot another coroutine can only release once the loop runs again.
    wait_s = 0.0 if _on_event_loop_thread() else _GETCONN_WAIT_S
    deadline = time.monotonic() + wait_s
    attempt = 0
    while True:
        try:
            return _pool.getconn()
        except psycopg2.pool.PoolError as exc:
            last_err = exc
            attempt += 1
            if attempt >= _GETCONN_RETRIES and time.monotonic() >= deadline:
                LOGGER.warning(
                    "DB pool exhausted after %s attempts / %.1fs (max=%s)",
                    attempt, wait_s, _POOL_MAX,
                )
                break
            time.sleep(min(0.25, _GETCONN_RETRY_DELAY_S * attempt))
    assert last_err is not None
    raise last_err


def put_conn(conn):
    if not _pool or not conn:
        return
    try:
        conn.rollback()
    except Exception:
        note_suppressed()
    _pool.putconn(conn)


def pool_stats() -> dict:
    """Lightweight pool metadata for ops dashboards."""
    return {
        "ready": _pool is not None,
        "min": _POOL_MIN,
        "max": _POOL_MAX,
        "retries": _GETCONN_RETRIES,
        "wait_s": _GETCONN_WAIT_S,
    }

class db_conn:
    def __enter__(self):
        self.conn = get_conn()
        return self.conn
    def __exit__(self, exc_type, *_):
        # try/finally: a commit/rollback failure must still return the
        # connection to the pool, or a single bad transaction permanently
        # leaks a pool slot (forensic AD-2).
        try:
            if exc_type:
                self.conn.rollback()
            else:
                self.conn.commit()
        finally:
            put_conn(self.conn)

def _ensure_tables():
    """Create tables only if they do not exist. Non-destructive."""
    with db_conn() as conn:
        cur = conn.cursor()
        # Only create if not exists - preserves v1 data
        cur.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id SERIAL PRIMARY KEY,
                symbol VARCHAR(20) NOT NULL,
                direction VARCHAR(10) NOT NULL,
                confidence FLOAT NOT NULL,
                prob_model_raw FLOAT,
                prob_train_calibrated FLOAT,
                prob_live_recalibrated FLOAT,
                confidence_final FLOAT,
                entry_price FLOAT,
                target_price FLOAT,
                stop_price FLOAT,
                run_at BIGINT,
                predicted_at BIGINT,
                expires_at BIGINT,
                resolved_at BIGINT,
                outcome VARCHAR(10),
                exit_price FLOAT,
                pnl_pct FLOAT,
                asset_type VARCHAR(10) DEFAULT 'stock',
                scores JSONB
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ghost_state (
                key TEXT PRIMARY KEY,
                val TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ghost_v3_model (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at BIGINT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_portfolio (
                id SERIAL PRIMARY KEY,
                symbol TEXT NOT NULL,
                asset_type TEXT DEFAULT 'stock',
                quantity FLOAT NOT NULL,
                buy_price FLOAT NOT NULL,
                buy_date TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                manual_price FLOAT DEFAULT NULL,
                created_at BIGINT DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT
            )
        """)
        LOGGER.info("Tables verified")


def ensure_ghost_state(cur=None):
    """Create ghost_state table if it doesn't exist.

    Call this instead of inlining CREATE TABLE IF NOT EXISTS ghost_state
    everywhere. Accepts an optional cursor; if None, opens its own connection.
    Centralized per PR #125 forensic audit — 37 duplicates eliminated.
    """
    if cur is not None:
        cur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
        return
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
        conn.commit()


_SCHEMA_BACKFILL_MARKER = "schema_backfills_v1"


def _backfills_already_done(cur) -> bool:
    """Fail-open: unknown/missing marker means run the backfills again.

    The gated backfills are idempotent by construction (fill NULLs / dedupe
    open rows), so re-running them on a marker read failure is safe and
    strictly better than skipping a migration that never completed."""
    try:
        ensure_ghost_state(cur)
        cur.execute("SELECT val FROM ghost_state WHERE key=%s", (_SCHEMA_BACKFILL_MARKER,))
        row = cur.fetchone()
        return bool(row and row[0])
    except Exception:
        return False


def _mark_backfills_done(cur) -> bool:
    """Record completion only after every gated backfill succeeds."""
    try:
        cur.execute(
            "INSERT INTO ghost_state(key,val) VALUES(%s,'1') "
            "ON CONFLICT(key) DO UPDATE SET val='1'",
            (_SCHEMA_BACKFILL_MARKER,),
        )
        return True
    except Exception:
        note_suppressed()
        return False


def _is_backfill(sql: str) -> bool:
    """True for the idempotent full-table backfill UPDATEs that dominate
    startup cost. DDL (ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS)
    is cheap and deliberately NOT classified here — it must keep running every
    boot so new deployments still get new columns/indexes."""
    return (
        "SET predicted_at = run_at" in sql
        or "SET confidence_final=confidence" in sql
        or "duplicate_open_migration" in sql
    )


def _migrate_schema():
    """Add missing columns to v1 predictions table for v2 compatibility."""
    # V1 uses run_at, v2 uses predicted_at - add both, keep v1 data intact
    migrations = [
        "ALTER TABLE predictions ADD COLUMN IF NOT EXISTS outcome VARCHAR(10)",
        "ALTER TABLE predictions ADD COLUMN IF NOT EXISTS exit_price FLOAT",
        "ALTER TABLE predictions ADD COLUMN IF NOT EXISTS pnl_pct FLOAT",
        "ALTER TABLE predictions ADD COLUMN IF NOT EXISTS resolved_at BIGINT",
        "ALTER TABLE predictions ADD COLUMN IF NOT EXISTS predicted_at BIGINT",
        "UPDATE predictions SET predicted_at = run_at WHERE predicted_at IS NULL AND run_at IS NOT NULL",
        "ALTER TABLE predictions ALTER COLUMN run_at DROP NOT NULL",
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'predictions' AND column_name = 'method'
            ) THEN
                ALTER TABLE predictions ALTER COLUMN method DROP NOT NULL;
            END IF;
        END $$
        """,
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'predictions' AND column_name = 'horizon_h'
            ) THEN
                ALTER TABLE predictions ALTER COLUMN horizon_h DROP NOT NULL;
            END IF;
        END $$
        """,
        "ALTER TABLE predictions ADD COLUMN IF NOT EXISTS features JSONB",
        "ALTER TABLE predictions ADD COLUMN IF NOT EXISTS scores JSONB",
        # Explicit probability lifecycle. Historical rows cannot truthfully
        # reconstruct model/train/live stages; only final confidence is a safe
        # compatibility backfill from the legacy confidence column.
        "ALTER TABLE predictions ADD COLUMN IF NOT EXISTS prob_model_raw FLOAT",
        "ALTER TABLE predictions ADD COLUMN IF NOT EXISTS prob_train_calibrated FLOAT",
        "ALTER TABLE predictions ADD COLUMN IF NOT EXISTS prob_live_recalibrated FLOAT",
        "ALTER TABLE predictions ADD COLUMN IF NOT EXISTS confidence_final FLOAT",
        "UPDATE predictions SET confidence_final=confidence WHERE confidence_final IS NULL",
        # Phase 3 gate: point-in-time feature snapshots (12-col ingestion prep).
        """
        CREATE TABLE IF NOT EXISTS ghost_feature_snapshots (
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            feature_asof_ts BIGINT NOT NULL,
            source TEXT NOT NULL DEFAULT 'v3_live',
            payload JSONB,
            created_at BIGINT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_feature_snapshots_symbol_asof
        ON ghost_feature_snapshots (symbol, feature_asof_ts DESC)
        """,
        # Administratively void duplicate open picks (keep highest confidence)
        # before the unique index. This is not market evidence and must never be
        # represented as canonical EXPIRED, which requires a complete horizon.
        """
        UPDATE predictions p
        SET outcome='ADMIN_VOID', resolved_at=EXTRACT(EPOCH FROM NOW())::BIGINT,
            exit_price=NULL, pnl_pct=NULL,
            scores=COALESCE(scores, '{}'::jsonb) ||
                   '{"administrative_reason":"duplicate_open_migration"}'::jsonb
        FROM (
            SELECT id, ROW_NUMBER() OVER (
                PARTITION BY symbol ORDER BY confidence DESC, predicted_at DESC, id DESC
            ) AS rn
            FROM predictions WHERE outcome IS NULL
        ) d
        WHERE p.id = d.id AND d.rn > 1
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_predictions_one_open_symbol
        ON predictions (symbol) WHERE outcome IS NULL
        """,
        # Hot-query indexes (audit M2-2). predictions is the fastest-growing
        # table (~223k rows) and previously had only the partial unique index.
        # 1. Per-symbol history reads: WHERE symbol=%s [AND predicted_at >= %s]
        #    ORDER BY predicted_at DESC (predictions/history/stats endpoints).
        """
        CREATE INDEX IF NOT EXISTS idx_predictions_symbol_time
        ON predictions (symbol, predicted_at DESC)
        """,
        # 2. Resolved-outcome scans: WHERE outcome IN ('WIN','LOSS') AND
        #    resolved_at > %s (health audits, win-rate stats, kill conditions).
        """
        CREATE INDEX IF NOT EXISTS idx_predictions_outcome_resolved
        ON predictions (outcome, resolved_at DESC) WHERE outcome IS NOT NULL
        """,
        # 3. Research-pick cap check runs every scan cycle against the JSONB
        #    scores column; expression index avoids a full-table JSONB scan.
        """
        CREATE INDEX IF NOT EXISTS idx_predictions_research_pick
        ON predictions ((scores->>'research_pick'), predicted_at DESC)
        WHERE scores->>'research_pick' = 'true'
        """,
    ]
    with db_conn() as conn:
        cur = conn.cursor()
        backfills_done = _backfills_already_done(cur)
        backfills_complete = backfills_done
        for sql in migrations:
            try:
                # Run-once gate (checklist #17): the three idempotent full-table
                # backfill UPDATEs (predicted_at, confidence_final, ADMIN_VOID
                # dedupe) rewrote ~223k rows every boot. Skip them once the
                # marker is set; keep all DDL + the unique-index step running.
                if _is_backfill(sql) and backfills_done:
                    continue
                cur.execute(sql)
                conn.commit()
            except Exception as e:
                LOGGER.warning("Migration: " + str(e)[:80])
                conn.rollback()
                if _is_backfill(sql):
                    backfills_complete = False
        if not backfills_done and backfills_complete:
            _mark_backfills_done(cur)
            conn.commit()
    try:
        from core.performance_log import ensure_perf_tables
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_perf_tables(cur)
    except Exception as e:
        LOGGER.warning("Perf log tables: " + str(e)[:80])
    try:
        from core.squeeze_outcomes import ensure_squeeze_outcomes_table
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_squeeze_outcomes_table(cur)
    except Exception as e:
        LOGGER.warning("Squeeze outcomes table: " + str(e)[:80])
    try:
        from core.shadow_outcomes import ensure_shadow_table
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_shadow_table(cur)
    except Exception as e:
        LOGGER.warning("Shadow outcomes table: " + str(e)[:80])
    try:
        from core.super_ghost_ledger import ensure_ledger_table
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_ledger_table(cur)
    except Exception as e:
        LOGGER.warning("Super Ghost ledger table: " + str(e)[:80])
    try:
        from core.super_ghost_learning import ensure_learning_tables
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_learning_tables(cur)
    except Exception as e:
        LOGGER.warning("Super Ghost learning tables: " + str(e)[:80])
    try:
        from core.super_ghost_lab import ensure_lab_tables
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_lab_tables(cur)
    except Exception as e:
        LOGGER.warning("Super Ghost lab tables: " + str(e)[:80])
    try:
        from core.super_ghost_memory import ensure_memory_tables, ensure_default_model
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_memory_tables(cur)
            ensure_default_model(cur)
    except Exception as e:
        LOGGER.warning("Super Ghost memory tables: " + str(e)[:80])
    try:
        from core.super_ghost_shadow import ensure_shadow_tables
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_shadow_tables(cur)
    except Exception as e:
        LOGGER.warning("Super Ghost shadow tables: " + str(e)[:80])
    try:
        from core.super_ghost_promotion import ensure_promotion_tables
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_promotion_tables(cur)
    except Exception as e:
        LOGGER.warning("Super Ghost promotion tables: " + str(e)[:80])
    try:
        from core.super_ghost_feature_store import ensure_feature_store_tables
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_feature_store_tables(cur)
    except Exception as e:
        LOGGER.warning("Super Ghost feature store tables: " + str(e)[:80])
    try:
        from core.super_ghost_data_brain import ensure_data_brain_tables
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_data_brain_tables(cur)
    except Exception as e:
        LOGGER.warning("Super Ghost data brain tables: " + str(e)[:80])
    try:
        from core.research_schema import ensure_research_schema
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_research_schema(cur)
    except Exception as e:
        LOGGER.warning("Research schema: " + str(e)[:80])
    try:
        from core.super_ghost_precision import ensure_precision_tables
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_precision_tables(cur)
    except Exception as e:
        LOGGER.warning("Super Ghost precision tables: " + str(e)[:80])
    try:
        from core.super_ghost_range_calibration import ensure_range_calibration_tables
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_range_calibration_tables(cur)
    except Exception as e:
        LOGGER.warning("Super Ghost range calibration tables: " + str(e)[:80])
    try:
        from core.super_ghost_regime_calibration import ensure_regime_calibration_tables
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_regime_calibration_tables(cur)
    except Exception as e:
        LOGGER.warning("Super Ghost regime calibration tables: " + str(e)[:80])
    try:
        from core.daily_report import ensure_daily_report_tables
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_daily_report_tables(cur)
    except Exception as e:
        LOGGER.warning("Daily report log tables: " + str(e)[:80])
    try:
        from core.squeeze_hunter_ledger import (
            enforce_hunter_constraints,
            ensure_hunter_tables,
            purge_invalid_hunter_samples,
        )
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_hunter_tables(cur)
            purged = purge_invalid_hunter_samples(cur)
            enforce_hunter_constraints(cur)
            conn.commit()
            if purged:
                LOGGER.info("Squeeze Hunter: purged %s invalid calibration samples", purged)
    except Exception as e:
        LOGGER.warning("Squeeze Hunter ledger tables: " + str(e)[:80])
    try:
        from core.checklist_ledger import ensure_checklist_tables
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_checklist_tables(cur)
            conn.commit()
    except Exception as e:
        LOGGER.warning("Checklist ledger tables: " + str(e)[:80])
    try:
        from core.bull_run_ledger import ensure_bull_run_tables
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_bull_run_tables(cur)
    except Exception as e:
        LOGGER.warning("Bull-run scenario ledger tables: " + str(e)[:80])
    try:
        from core.explosion_benchmark import ensure_benchmark_tables
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_benchmark_tables(cur)
    except Exception as e:
        LOGGER.warning("Explosion benchmark tables: " + str(e)[:80])
    try:
        from core.external_context_ledger import ensure_external_context_tables
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_external_context_tables(cur)
    except Exception as e:
        LOGGER.warning("External context tables: " + str(e)[:80])
    try:
        from core.agent_workflow import ensure_agent_workflow_tables
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_agent_workflow_tables(cur)
    except Exception as e:
        LOGGER.warning("Agent workflow tables: " + str(e)[:80])
    LOGGER.info("Schema migration complete")
