"""DB pool resilience + kill-status bundle."""



def test_pool_stats_after_init(monkeypatch):
    import core.db as db

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/test")
    db._pool = None
    monkeypatch.setattr(
        db,
        "psycopg2",
        __import__("psycopg2"),
    )

    class FakePool:
        def __init__(self, mn, mx, dsn):
            self.mn, self.mx, self.dsn = mn, mx, dsn

    monkeypatch.setattr(db.psycopg2.pool, "ThreadedConnectionPool", FakePool)
    monkeypatch.setattr(db, "_ensure_tables", lambda: None)
    monkeypatch.setattr(db, "_migrate_schema", lambda: None)

    db.init_db()
    stats = db.pool_stats()
    assert stats["ready"] is True
    assert stats["max"] == 25


def test_get_conn_retries_on_pool_error(monkeypatch):
    import core.db as db

    calls = {"n": 0}

    class FakePool:
        def getconn(self):
            calls["n"] += 1
            if calls["n"] < 3:
                raise db.psycopg2.pool.PoolError("connection pool exhausted")
            return object()

        def putconn(self, _conn):
            pass

    db._pool = FakePool()
    monkeypatch.setattr(db.time, "sleep", lambda _s: None)
    conn = db.get_conn()
    assert conn is not None
    assert calls["n"] == 3


def test_evaluate_kill_conditions_include_pause(monkeypatch):
    import core.prediction as pred

    monkeypatch.setattr(pred, "_kill_cfg", lambda: {
        "enabled": True,
        "winrate_floor": 0.7,
        "winrate_window": 30,
        "brier_ceiling": 0.35,
        "brier_window": 30,
        "consec_losses": 3,
        "expectancy_window": 20,
        "cooldown_minutes": 1440,
        "min_samples": 10,
    })
    monkeypatch.setattr(pred, "_kill_symbol_universe", lambda: ["WOLF"])

    class Cur:
        def execute(self, sql, params=None):
            self.sql = sql

        def fetchall(self):
            if "predictions" in getattr(self, "sql", ""):
                return [(0.9, "WIN", 1.0)]
            return []

    class Conn:
        def cursor(self):
            return Cur()

        def commit(self):
            pass

        def rollback(self):
            pass

    class Ctx:
        def __enter__(self):
            return Conn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(pred, "db_conn", lambda: Ctx())
    out = pred.evaluate_kill_conditions(include_pause=True)
    assert out["ok"] is True
    assert "engine_pause" in out
    assert out["engine_pause"]["paused"] is False
