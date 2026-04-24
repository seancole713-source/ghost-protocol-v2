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
        if "COUNT(*) FROM predictions WHERE outcome IS NULL" in self.last_sql and "expires_at > extract" in self.last_sql:
            return (3,)
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


def test_run_health_audit_returns_structured_findings_and_autofix(monkeypatch):
    monkeypatch.delenv("STOCK_SYMBOLS", raising=False)
    cur = FakeCursor()

    class _R:
        def __init__(self, path):
            self.path = path

    _paths = [
        "/health",
        "/api/health",
        "/api/diagnostics",
        "/api/stats",
        "/api/cockpit/context",
        "/api/picks",
        "/api/v2/recent",
        "/api/news",
        "/api/coverage",
        "/api/v3/status",
        "/api/regime",
        "/cockpit",
        "/api/portfolio",
        "/api/health/audit",
        "/api/health/audit/history",
    ]
    app = type("App", (), {"routes": [_R(p) for p in _paths]})

    _pv = {"start_ts": 1775347200, "wins": 1, "losses": 0}
    report = run_health_audit(
        app=app,
        db_conn=lambda: FakeDbCtx(cur),
        health_payload={"status": "healthy", "score": 100, "issues": [], "warnings": []},
        diagnostics_payload={"checks_passed": 10, "warnings": 0, "errors": 0, "details": {"errors": []}},
        stats_payload={"wins": 2, "losses": 1, "post_v32": _pv},
        cockpit_payload={"stats": {"wins": 2, "losses": 1, "post_v32": _pv}, "activity": {"open_predictions": 3}},
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
