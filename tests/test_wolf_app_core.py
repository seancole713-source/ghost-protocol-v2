import json

import wolf_app


class QueueCursor:
    def __init__(self, fetchall_values=None, fetchone_values=None):
        self.fetchall_values = list(fetchall_values or [])
        self.fetchone_values = list(fetchone_values or [])
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        if not self.fetchall_values:
            return []
        return self.fetchall_values.pop(0)

    def fetchone(self):
        if not self.fetchone_values:
            return None
        return self.fetchone_values.pop(0)


class RoutingCursor:
    def __init__(self, sticky_ts, metas, hist_min):
        self.sticky_ts = sticky_ts
        self.metas = metas
        self.hist_min = hist_min
        self.last_sql = ""
        self.executed = []

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.executed.append((sql, params))

    def fetchone(self):
        if "SELECT val FROM ghost_state WHERE key='v32_stats_start_ts'" in self.last_sql:
            return (str(self.sticky_ts),) if self.sticky_ts else None
        if "SELECT MIN(predicted_at) FROM predictions" in self.last_sql:
            return (self.hist_min,) if self.hist_min else (None,)
        return None

    def fetchall(self):
        if "SELECT key, value FROM ghost_v3_model WHERE key LIKE 'meta_%'" in self.last_sql:
            return [
                (f"meta_{sym}", json.dumps(meta))
                for sym, meta in self.metas.items()
            ]
        return []


def test_v32_stats_start_ts_prefers_env_override(monkeypatch):
    monkeypatch.setenv("V3_STATS_START_TS", "1775347200")
    cur = QueueCursor()
    assert wolf_app._v32_stats_start_ts(cur) == 1775347200
    assert cur.executed == []


def test_v32_stats_start_ts_allows_backward_correction(monkeypatch):
    monkeypatch.delenv("V3_STATS_START_TS", raising=False)
    cur = RoutingCursor(
        sticky_ts=1775606400,
        metas={
            "UNI": {"label_type": "tp_sl_daily", "trained_at": 1775347200},
            "SOL": {"label_type": "tp_sl_daily", "trained_at": 1775400000},
        },
        hist_min=1775380000,
    )
    out = wolf_app._v32_stats_start_ts(cur)
    assert out == 1775347200
    assert any("INSERT INTO ghost_state(key,val) VALUES('v32_stats_start_ts'" in sql for sql, _ in cur.executed)


def test_compute_get_stats_uses_v32_breakdowns(monkeypatch):
    monkeypatch.setenv("V3_STATS_START_TS", "1775347200")
    monkeypatch.setenv("CRYPTO_SYMBOLS", "ETH, UNI")
    monkeypatch.setenv("STOCK_SYMBOLS", "TSLA, META")
    cur = QueueCursor(
        fetchall_values=[
            [("WIN", 10), ("LOSS", 5)],
            [("WIN", 2), ("LOSS", 1)],
            [("WIN", 1), ("LOSS", 2)],
        ],
        fetchone_values=[(3,)],
    )
    payload = wolf_app._compute_get_stats(cur)
    assert payload["wins"] == 10
    assert payload["losses"] == 5
    assert payload["open_positions"] == 3
    assert payload["post_v32"]["start_ts"] == 1775347200
    assert payload["post_v32"]["wins"] == 2
    assert payload["post_v32"]["losses"] == 1
    assert payload["post_v32_resolved"]["wins"] == 1
    assert payload["post_v32_resolved"]["losses"] == 2
    assert payload["scan_symbols"]["crypto"] == ["ETH", "UNI"]
    assert payload["scan_symbols"]["stocks"] == ["TSLA", "META"]


def test_api_health_alias_calls_health(monkeypatch):
    expected = {"status": "healthy", "score": 100}
    monkeypatch.setattr(wolf_app, "health", lambda: expected)
    assert wolf_app.api_health() == expected


def test_api_health_route_registered_once():
    paths = [getattr(r, "path", "") for r in wolf_app.APP.routes]
    assert paths.count("/api/health") == 1


def test_health_audit_endpoint_returns_wrapped_report(monkeypatch):
    class _Cur:
        def execute(self, sql, params=None):
            return None

        def fetchall(self):
            return []

        def fetchone(self):
            return None

    class _Conn:
        def cursor(self):
            return _Cur()

    class _DbCtx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, exc_type, exc, tb):
            return False

    async def _diag():
        return {"checks_passed": 1, "warnings": 0, "errors": 0}

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _DbCtx())
    monkeypatch.setattr(wolf_app, "health", lambda: {"status": "healthy", "score": 100})
    monkeypatch.setattr(wolf_app, "diagnostics", _diag)
    monkeypatch.setattr(wolf_app, "_compute_get_stats", lambda cur: {"wins": 2, "losses": 1})
    monkeypatch.setattr(wolf_app, "cockpit_context", lambda: {"stats": {"wins": 2, "losses": 1}})
    monkeypatch.setenv("CRON_SECRET", "")
    monkeypatch.setattr(
        "core.health_audit.run_health_audit",
        lambda **kwargs: {"status": "PASS", "summary": {"total_checks": 1}, "findings": []},
    )

    out = wolf_app.health_audit(x_cron_secret="", auto_fix=True)
    assert out["ok"] is True
    assert out["audit"]["status"] == "PASS"
