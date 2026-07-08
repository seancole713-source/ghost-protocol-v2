"""Daily report logs: full Ghost day report + append-only notebook."""
import importlib

import core.daily_report as dr


class _FakeCursor:
    def __init__(self):
        self.sqls = []
        self.params = []
        self._rows = []
        self._one = None
        self.description = []

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        self.params.append(params)
        if "FROM ghost_perf_cycles" in sql:
            self._rows = [
                (
                    42, 1783521000, 1200, 74, 0, 0, 0, False,
                    "v3_meta_gate", False, None, 0, None,
                    {"v3_meta_gate": 4, "no_v3_model": 2},
                    {"symbol": "XPO", "up_prob": 0.89, "skip": "v3_meta_gate", "min_win_proba": 0.55},
                    {"label": "Trend-up"}, {"mode": "aggressive"}, [],
                )
            ]
        elif "FROM ghost_perf_symbol_evals" in sql:
            self._rows = [
                ("XPO", "v3_meta_gate", False, False, None, 0.89, None, 0.55, "Neutral", 1783521000),
                ("ARDT", "v3_regime_gate", False, False, None, 0.61, None, 0.55, "Trend-down", 1783521000),
            ]
        elif "FROM ghost_perf_events" in sql:
            self._rows = [(9, 1783521001, "cycle_complete", None, None, 42, {"saved": 0})]
        elif "COALESCE(SUM(pnl),0)" in sql:
            self._one = (10.0, 1, 2)
        elif "COUNT(*), COALESCE(SUM(qty*entry_price),0)" in sql:
            self._one = (1, 500.0)
        elif "FROM ghost_paper_daily" in sql:
            self._one = ("2026-07-08", 10010.0, 10.0)
        elif "paper_wallet_config" in sql:
            self._one = ('{"starting_balance":10000,"monthly_goal":20000}',)
        elif "status='open'" in sql:
            self._rows = [("ARCT", "shadow", 7.29, 1783522000)]
        elif "status='closed'" in sql:
            self._rows = [("CLNE", "target", 10.0, 2.0, 1783523000)]
        elif "INSERT INTO ghost_daily_report_logs" in sql:
            self._one = (123,)
        elif "FROM ghost_daily_report_logs" in sql:
            self._rows = [(123, "2026-07-08", 1783524000, 159, "abc123", 95, False,
                           "v3_meta_gate", 0, 10010.0, 10.0, "real_but_not_70",
                           {"narrative": ["hello"], "ok": True})]
        else:
            self._rows = []
            self._one = None

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, cur): self.cur = cur
    def cursor(self): return self.cur
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _patch_db(monkeypatch):
    cur = _FakeCursor()
    monkeypatch.setattr(dr._db, "db_conn", lambda: _FakeConn(cur))
    return cur


def test_build_daily_report_uses_db_only_perf_log_not_live_gate(monkeypatch):
    cur = _patch_db(monkeypatch)
    monkeypatch.setattr(dr, "_day_bounds_ct", lambda day=None: ("2026-07-08", 1783520000, 1783606400))
    monkeypatch.setattr("wolf_app._deploy_meta", lambda: {"_pr_version": 159, "git_sha_short": "abc123"})
    monkeypatch.setattr("wolf_app._health_public", lambda: {"score": 95, "status": "healthy"})
    monkeypatch.setattr("core.watcher.watcher_summary", lambda days=30: {
        "shadow_calibration": {"resolved_n": 10, "brier": 0.2,
            "high_confidence": {"win_rate": 0.6},
            "verdict": {"headline": "real signal", "status": "real_but_not_70"},
            "bins": []},
        "blind_spots": {"top_skip_codes": []},
    })
    monkeypatch.setattr("core.circuit_breaker.all_breaker_status", lambda: {"alpaca": {"state": "closed"}})
    # If report regresses to calling the slow live gate endpoint, this test fails.
    monkeypatch.setattr("api.routes_wolf_ops.wolf_gate_status", lambda: (_ for _ in ()).throw(AssertionError("slow gate called")))

    out = dr.build_daily_report()
    assert out["ok"] is True
    assert out["decisions"]["source"] == "ghost_perf_cycles_db_only"
    assert out["decisions"]["latest_cycle_id"] == 42
    assert out["decisions"]["picks_fired_today"] == 0
    assert out["decisions"]["top_skip_reasons"]["v3_meta_gate"] == 4
    assert out["wallet"]["opened_today"][0]["symbol"] == "ARCT"
    assert "fired ZERO live picks" in " ".join(out["narrative"])
    assert not any("wolf_gate_status" in sql for sql in cur.sqls)


def test_snapshot_daily_report_writes_only_report_log(monkeypatch):
    cur = _patch_db(monkeypatch)
    monkeypatch.setattr(dr, "build_daily_report", lambda day=None: {
        "ok": True, "date": "2026-07-08", "generated_ts": 1783524000,
        "identity": {"pr_version": 159, "git_sha": "abc123", "health_score": 95},
        "decisions": {"gate_open": False, "gate_reason": "v3_meta_gate", "picks_fired_today": 0},
        "wallet": {"total_value": 10010.0, "today_pnl": 10.0},
        "calibration": {"status": "real_but_not_70"},
        "narrative": ["hello"],
    })
    out = dr.snapshot_daily_report()
    joined = "\n".join(cur.sqls)
    assert out["ok"] is True and out["log_id"] == 123
    assert "ghost_daily_report_logs" in joined
    assert "INSERT INTO ghost_daily_report_logs" in joined
    assert "INSERT INTO predictions" not in joined
    assert "ghost_paper_trades" not in joined
    assert "ghost_shadow_outcomes" not in joined


def test_latest_daily_report_logs_reads_persisted_rows(monkeypatch):
    _patch_db(monkeypatch)
    out = dr.latest_daily_report_logs(limit=5, day="2026-07-08")
    assert out["ok"] is True and out["read_only"] is True
    assert out["rows"][0]["id"] == 123
    assert out["rows"][0]["narrative"] == ["hello"]


def test_daily_report_routes(monkeypatch):
    from fastapi.testclient import TestClient
    from wolf_app import APP

    monkeypatch.setattr("core.daily_report.build_daily_report", lambda day=None: {"ok": True, "date": day or "today"})
    monkeypatch.setattr("core.daily_report.latest_daily_report_logs", lambda limit=24, day=None, include_payload=False: {
        "ok": True, "limit": limit, "date": day, "include_payload": include_payload, "rows": []
    })
    monkeypatch.setattr("wolf_app._cron_ok", lambda secret: secret == "ok")
    monkeypatch.setattr("core.daily_report.snapshot_daily_report", lambda day=None: {"ok": True, "report_date": day})

    c = TestClient(APP)
    assert c.get("/api/report/daily?day=2026-07-08").json()["date"] == "2026-07-08"
    logs = c.get("/api/report/daily/logs?limit=3&day=2026-07-08&include_payload=1").json()
    assert logs["limit"] == 3 and logs["include_payload"] is True
    assert c.post("/api/report/daily/snapshot").status_code == 403
    assert c.post("/api/report/daily/snapshot?day=2026-07-08", headers={"x-cron-secret": "ok"}).json()["report_date"] == "2026-07-08"
