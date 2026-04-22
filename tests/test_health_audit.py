from core.health_audit import run_health_audit


class FakeCursor:
    def __init__(self):
        self.last_sql = ""
        self.executed = []
        self.v32_value = "bad-int"
        self.inserted_audit_runs = 0

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.executed.append((sql, params))
        if "INSERT INTO health_audit_runs" in sql:
            self.inserted_audit_runs += 1
        if "INSERT INTO ghost_state(key,val) VALUES('v32_stats_start_ts'" in sql:
            self.v32_value = "0"

    def fetchone(self):
        if "SELECT 1" in self.last_sql:
            return (1,)
        if "SELECT val FROM ghost_state WHERE key='v32_stats_start_ts'" in self.last_sql:
            return (self.v32_value,)
        return None

    def fetchall(self):
        if "SELECT outcome, COUNT(*) FROM predictions WHERE outcome IN ('WIN','LOSS')" in self.last_sql:
            return [("WIN", 2), ("LOSS", 1)]
        return []


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class FakeDbCtx:
    def __init__(self, cursor):
        self._conn = FakeConn(cursor)

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        return False


def test_run_health_audit_returns_structured_findings_and_autofix():
    cur = FakeCursor()

    class _R:
        def __init__(self, path):
            self.path = path

    app = type("App", (), {"routes": [_R("/health"), _R("/api/health"), _R("/api/diagnostics"), _R("/api/stats"), _R("/api/cockpit/context"), _R("/api/picks"), _R("/api/v2/recent"), _R("/api/news")]})

    report = run_health_audit(
        app=app,
        db_conn=lambda: FakeDbCtx(cur),
        health_payload={"status": "healthy", "score": 100},
        diagnostics_payload={"checks_passed": 10, "warnings": 0, "errors": 0},
        stats_payload={"wins": 2, "losses": 1},
        cockpit_payload={"stats": {"wins": 2, "losses": 1}},
        auto_fix=True,
    )

    assert report["status"] in ("PASS", "FAIL")
    assert isinstance(report["findings"], list)
    assert report["summary"]["autofix_attempted"] >= 1
    assert report["summary"]["autofix_resolved"] >= 1
    assert cur.inserted_audit_runs >= 1

    sample = report["findings"][0]
    for key in ("status", "location", "evidence", "impact", "auto_fix", "fix_result", "category"):
        assert key in sample
