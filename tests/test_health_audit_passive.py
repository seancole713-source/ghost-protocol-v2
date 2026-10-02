"""The read-only health audit (auto_fix=false&persist=false) is a passive path.

CI uses it as the zero-critical release gate, so it must (a) issue no write or
DDL statement, open every transaction READ ONLY, never probe providers or
repair breakers, and (b) still surface critical findings.
"""
import core.db as db
import wolf_app

_WRITE_HEADS = ("UPDATE", "INSERT", "DELETE", "CREATE", "ALTER", "DROP", "TRUNCATE")


class _FakePool:
    def __init__(self):
        self.txns = []

    def get_conn(self):
        txn = []
        self.txns.append(txn)
        return _FakeConn(txn)

    def put_conn(self, conn):
        pass


class _FakeConn:
    def __init__(self, txn):
        self.txn = txn

    def cursor(self):
        return _FakeCursor(self.txn)

    def commit(self):
        pass

    def rollback(self):
        pass


class _FakeCursor:
    rowcount = 0

    def __init__(self, txn):
        self.txn = txn

    def execute(self, sql, params=None):
        self.txn.append(" ".join(str(sql).split()))

    def fetchone(self):
        # Large enough that health() sees every symbol dedup-blocked, which is
        # exactly when the non-passive path would void picks.
        return (5,)

    def fetchall(self):
        return []


_FORBIDDEN_CALLS = []


def _forbid(name):
    # Recorded (not only raised): several health() blocks swallow exceptions.
    def _raise(*a, **k):
        _FORBIDDEN_CALLS.append(name)
        raise AssertionError(f"{name} must not run in the passive audit")
    return _raise


def _run_passive(monkeypatch):
    _FORBIDDEN_CALLS.clear()
    monkeypatch.setenv("CRON_SECRET", "")
    monkeypatch.setenv("STOCK_SYMBOLS", "WOLF")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr("core.prices.check_feeds", _forbid("provider feed probe"))
    monkeypatch.setattr(
        "core.circuit_breaker.auto_recover_breakers", _forbid("breaker auto-recovery"),
    )
    monkeypatch.setattr("core.degraded_mode.check_degraded", _forbid("degraded re-evaluation"))
    monkeypatch.setattr("requests.get", _forbid("self-HTTP probe"))
    monkeypatch.setattr("core.leader_lock.is_leader", lambda: True)
    monkeypatch.setattr(wolf_app, "cockpit_context", _forbid("cockpit_context/health_cached"))
    # Install the recording pool last so import-time reads of the modules
    # patched above are not attributed to the audit.
    pool = _FakePool()
    monkeypatch.setattr(db, "get_conn", pool.get_conn)
    monkeypatch.setattr(db, "put_conn", pool.put_conn)
    out = wolf_app.health_audit(x_cron_secret="", auto_fix=False, persist=False)
    return out, pool


def test_passive_audit_issues_no_writes_ddl_or_probes(monkeypatch):
    out, pool = _run_passive(monkeypatch)

    assert out["ok"] is True, out
    assert out["audit"]["mode"] == "passive_read_only"
    assert _FORBIDDEN_CALLS == []
    assert pool.txns, "audit ran no DB checks"
    statements = [sql for txn in pool.txns for sql in txn]
    # health(), diagnostics(), stats and the audit's own checks all ran.
    assert any("last_prediction_cycle_ts" in sql for sql in statements)
    assert any("ghost_v3_model" in sql for sql in statements)
    assert any("v32_stats_start_ts" in sql for sql in statements)
    for txn in pool.txns:
        assert txn[0] == "SET TRANSACTION READ ONLY", txn[:2]
        for sql in txn:
            head = sql.split(" ", 1)[0].upper()
            assert head not in _WRITE_HEADS, sql


def test_passive_audit_still_reports_critical_findings(monkeypatch):
    out, _ = _run_passive(monkeypatch)

    findings = out["audit"]["findings"]
    critical = [
        f for f in findings
        if f["status"] == "FAIL" and str(f["impact"]).lower() == "critical"
    ]
    assert any("Telegram credentials missing" in f["evidence"] for f in critical)
    skipped = {f["location"] for f in findings if f["status"] == "SKIPPED"}
    assert skipped == {
        "api:squeeze_daily_log_duplicates", "api:super_ghost_history_duplicates",
    }


def test_passive_scope_is_context_local_and_restored(monkeypatch):
    pool = _FakePool()
    monkeypatch.setattr(db, "get_conn", pool.get_conn)
    monkeypatch.setattr(db, "put_conn", pool.put_conn)

    with db.passive_inspection():
        assert db.in_passive_inspection() is True
        with db.db_conn() as conn:
            conn.cursor().execute("SELECT 1")
    assert db.in_passive_inspection() is False
    with db.db_conn() as conn:
        conn.cursor().execute("SELECT 1")

    assert pool.txns[0] == ["SET TRANSACTION READ ONLY", "SELECT 1"]
    assert pool.txns[1] == ["SELECT 1"]


def test_repair_audit_keeps_self_heal_path(monkeypatch):
    """auto_fix=true is not passive: no READ ONLY scope is applied."""
    seen = {}

    def _impl(**kwargs):
        seen.update(kwargs, passive_scope=db.in_passive_inspection())
        return {"ok": True}

    monkeypatch.setenv("CRON_SECRET", "")
    monkeypatch.setattr(wolf_app, "_health_audit_impl", _impl)
    wolf_app.health_audit(x_cron_secret="", auto_fix=True, persist=False)
    assert seen["passive"] is False and seen["passive_scope"] is False
    wolf_app.health_audit(x_cron_secret="", auto_fix=False, persist=False)
    assert seen["passive"] is True and seen["passive_scope"] is True
