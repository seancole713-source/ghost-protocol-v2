import os, logging, psycopg2, psycopg2.pool
from typing import Optional

LOGGER = logging.getLogger("ghost.db")
_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None

def init_db():
    global _pool
    _pool = psycopg2.pool.ThreadedConnectionPool(2, 10, dsn=os.environ["DATABASE_URL"])
    LOGGER.info("DB pool ready")
    _ensure_tables()

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
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id SERIAL PRIMARY KEY,
                symbol VARCHAR(20) NOT NULL,
                direction VARCHAR(10) NOT NULL,
                confidence FLOAT NOT NULL,
                entry_price FLOAT NOT NULL,
                target_price FLOAT NOT NULL,
                stop_price FLOAT NOT NULL,
                predicted_at BIGINT NOT NULL,
                expires_at BIGINT NOT NULL,
                resolved_at BIGINT,
                outcome VARCHAR(10),
                exit_price FLOAT,
                pnl_pct FLOAT,
                asset_type VARCHAR(10) DEFAULT 'crypto',
                notes TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                id SERIAL PRIMARY KEY,
                prediction_id INTEGER REFERENCES predictions(id),
                symbol VARCHAR(20) NOT NULL,
                direction VARCHAR(10) NOT NULL,
                entry_price FLOAT NOT NULL,
                target_price FLOAT NOT NULL,
                stop_price FLOAT NOT NULL,
                entry_time BIGINT NOT NULL,
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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS system_events (
                id SERIAL PRIMARY KEY,
                event_type VARCHAR(50),
                message TEXT,
                data JSONB,
                created_at BIGINT NOT NULL
            )
        """)
        LOGGER.info("Tables verified")
