import os, logging, time, psycopg2, psycopg2.pool
from typing import Optional

LOGGER = logging.getLogger("ghost.db")
_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None
_POOL_MIN = int(os.getenv("DB_POOL_MIN", "2"))
_POOL_MAX = int(os.getenv("DB_POOL_MAX", "25"))
_GETCONN_RETRIES = max(1, int(os.getenv("DB_POOL_GET_RETRIES", "4")))
_GETCONN_RETRY_DELAY_S = float(os.getenv("DB_POOL_RETRY_DELAY_S", "0.12"))


def init_db():
    global _pool
    _pool = psycopg2.pool.ThreadedConnectionPool(
        _POOL_MIN, _POOL_MAX, dsn=os.environ["DATABASE_URL"],
    )
    LOGGER.info("DB pool ready (min=%s max=%s)", _POOL_MIN, _POOL_MAX)
    _ensure_tables()
    _migrate_schema()


def get_conn():
    if not _pool:
        raise RuntimeError("Call init_db() first")
    last_err: Optional[Exception] = None
    for attempt in range(_GETCONN_RETRIES):
        try:
            return _pool.getconn()
        except psycopg2.pool.PoolError as exc:
            last_err = exc
            if attempt + 1 < _GETCONN_RETRIES:
                time.sleep(_GETCONN_RETRY_DELAY_S * (attempt + 1))
            else:
                LOGGER.warning(
                    "DB pool exhausted after %s attempts (max=%s)",
                    _GETCONN_RETRIES,
                    _POOL_MAX,
                )
    assert last_err is not None
    raise last_err


def put_conn(conn):
    if not _pool or not conn:
        return
    try:
        conn.rollback()
    except Exception:
        pass
    _pool.putconn(conn)


def pool_stats() -> dict:
    """Lightweight pool metadata for ops dashboards."""
    return {
        "ready": _pool is not None,
        "min": _POOL_MIN,
        "max": _POOL_MAX,
        "retries": _GETCONN_RETRIES,
    }

class db_conn:
    def __enter__(self):
        self.conn = get_conn()
        return self.conn
    def __exit__(self, exc_type, *_):
        if exc_type: self.conn.rollback()
        else: self.conn.commit()
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
        LOGGER.info("Tables verified")

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
        "ALTER TABLE predictions ALTER COLUMN method DROP NOT NULL",
        "ALTER TABLE predictions ALTER COLUMN horizon_h DROP NOT NULL",
        "ALTER TABLE predictions ADD COLUMN IF NOT EXISTS features JSONB",
        "ALTER TABLE predictions ADD COLUMN IF NOT EXISTS scores JSONB",
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
        # Expire duplicate open picks (keep highest confidence) before unique index.
        """
        UPDATE predictions p SET outcome='EXPIRED', resolved_at=EXTRACT(EPOCH FROM NOW())::BIGINT
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
    ]
    with db_conn() as conn:
        cur = conn.cursor()
        for sql in migrations:
            try:
                cur.execute(sql)
                conn.commit()
            except Exception as e:
                LOGGER.warning("Migration: " + str(e)[:80])
                conn.rollback()
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
    LOGGER.info("Schema migration complete")
