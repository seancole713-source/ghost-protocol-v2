import os, logging, psycopg2, psycopg2.pool
from typing import Optional

LOGGER = logging.getLogger("ghost.db")
_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None

def init_db():
    global _pool
    _pool = psycopg2.pool.ThreadedConnectionPool(2, 10, dsn=os.environ["DATABASE_URL"])
    LOGGER.info("DB pool ready")
    _ensure_tables()
    _migrate_schema()

def get_conn():
    if not _pool: raise RuntimeError("Call init_db() first")
    return _pool.getconn()

def put_conn(conn):
    if _pool: _pool.putconn(conn)

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
                asset_type VARCHAR(10) DEFAULT 'crypto'
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                id SERIAL PRIMARY KEY,
                prediction_id INTEGER,
                symbol VARCHAR(20),
                direction VARCHAR(10),
                entry_price FLOAT,
                target_price FLOAT,
                stop_price FLOAT,
                entry_time BIGINT,
                exit_time BIGINT,
                exit_price FLOAT,
                result VARCHAR(10),
                pnl_pct FLOAT,
                usd_in FLOAT DEFAULT 100.0,
                usd_out FLOAT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS price_cache (
                symbol VARCHAR(20) PRIMARY KEY,
                price FLOAT NOT NULL,
                source VARCHAR(30),
                updated_at BIGINT NOT NULL
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
    LOGGER.info("Schema migration complete")