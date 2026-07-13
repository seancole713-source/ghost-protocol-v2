"""Daily report logs: full Ghost day report + append-only notebook."""
import importlib
import time

import core.daily_report as dr


class _FakeCursor:
    def __init__(self):
        self.sqls = []
        self.params = []
        self._rows = []
        self._one = None
        self.description = []
        self.rowcount = 0

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
        elif "DELETE FROM ghost_daily_report_logs" in sql:
            self.rowcount = 4
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
    assert out["doctrine"]["ok"] is True
    assert out["doctrine"]["words"] == ["Clarity", "Decision", "Direction", "Alignment", "Consistency", "Results"]
    assert [s["key"] for s in out["doctrine"]["steps"]] == [
        "clarity", "decision", "direction", "alignment", "consistency", "results"
    ]
    assert "Ghost Doctrine: Clarity → Decision → Direction → Alignment → Consistency → Results" in " ".join(out["narrative"])
    assert "fired ZERO live picks" in " ".join(out["narrative"])
    assert not any("wolf_gate_status" in sql for sql in cur.sqls)


def test_report_doctrine_maps_six_words_without_engine_calls():
    out = dr._build_report_doctrine(
        identity={"health_score": 95},
        decisions={
            "scan_cycles_today": 3,
            "latest_cycle_age_seconds": 300,
            "gate_open": False,
            "picks_fired_today": 0,
            "latest_cycle_paused": True,
            "latest_cycle_pause_reason": "win_rate->auto_pause",
            "gate_reason": "engine_pause",
            "closest_to_firing": {"symbol": "XPO", "up_prob": 0.89},
            "latest_symbol_evals": [{"symbol": "XPO"}],
            "regime": "risk_on",
            "phase": "bootstrap",
            "top_skip_reasons": {"v3_prob_low": 4},
        },
        wallet={
            "total_value": 10010.0,
            "today_pnl": 10.0,
            "closed_today_wins": 1,
            "closed_today_losses": 0,
            "goal_pct": 50.1,
        },
        calibration={
            "status": "real_but_not_70",
            "resolved_n": 1031,
            "high_conf_win_rate": 0.621,
            "brier": 0.28,
        },
        breakers={"alpaca": "closed", "yfinance": "closed"},
    )
    assert out["display_only"] is True
    assert len(out["steps"]) == 6
    by_key = {s["key"]: s for s in out["steps"]}
    assert by_key["clarity"]["status"] == "pass"
    assert by_key["decision"]["status"] == "hold"
    assert by_key["direction"]["headline"] == "Closest candidate: XPO"
    assert by_key["consistency"]["status"] == "pass"
    assert by_key["results"]["status"] == "pass"


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
    # Retention must run in the same transaction so the notebook stays bounded.
    assert "DELETE FROM ghost_daily_report_logs WHERE created_at" in joined
    assert out["pruned_rows"] == 4
    assert out["retention_days"] == dr._LOG_RETENTION_DAYS


def test_snapshot_prune_uses_retention_cutoff(monkeypatch):
    cur = _patch_db(monkeypatch)
    monkeypatch.setattr(dr, "build_daily_report", lambda day=None: {
        "ok": True, "date": "2026-07-08", "generated_ts": 1783524000,
        "identity": {}, "decisions": {}, "wallet": {}, "calibration": {}, "narrative": [],
    })
    monkeypatch.setattr(dr, "_LOG_RETENTION_DAYS", 30)
    dr.snapshot_daily_report()
    # Find the DELETE and confirm the cutoff is ~30 days before generated_ts.
    delete_params = [p for sql, p in zip(cur.sqls, cur.params) if "DELETE FROM ghost_daily_report_logs" in sql]
    assert delete_params, "prune DELETE must fire"
    cutoff = delete_params[0][0]
    # cutoff = now - 30d; allow slack because now=time.time() at call.
    assert cutoff < 1783524000
    assert abs((int(time.time()) - 30 * 86400) - cutoff) < 5


def test_snapshot_survives_prune_failure(monkeypatch):
    # A prune error must never block the append (observability first).
    class _PruneBoomCursor(_FakeCursor):
        def execute(self, sql, params=None):
            if "DELETE FROM ghost_daily_report_logs" in sql:
                raise RuntimeError("prune boom")
            return super().execute(sql, params)
    cur = _PruneBoomCursor()
    monkeypatch.setattr(dr._db, "db_conn", lambda: _FakeConn(cur))
    monkeypatch.setattr(dr, "build_daily_report", lambda day=None: {
        "ok": True, "date": "2026-07-08", "generated_ts": 1783524000,
        "identity": {}, "decisions": {}, "wallet": {}, "calibration": {}, "narrative": [],
    })
    out = dr.snapshot_daily_report()
    assert out["ok"] is True and out["log_id"] == 123
    assert out["pruned_rows"] == 0


def test_latest_daily_report_logs_reads_persisted_rows(monkeypatch):
    _patch_db(monkeypatch)
    out = dr.latest_daily_report_logs(limit=5, day="2026-07-08")
    assert out["ok"] is True and out["read_only"] is True
    assert out["rows"][0]["id"] == 123
    assert out["rows"][0]["narrative"] == ["hello"]


def test_latest_logs_by_day_collapses_to_one_per_day(monkeypatch):
    # by_day must use DISTINCT ON (report_date) so ?limit means "days back",
    # not "snapshots back" (~96/day would otherwise all be the same day).
    cur = _patch_db(monkeypatch)
    out = dr.latest_daily_report_logs(limit=7, by_day=True)
    assert out["ok"] is True and out["by_day"] is True
    joined = "\n".join(cur.sqls)
    assert "DISTINCT ON (report_date)" in joined


def test_latest_logs_default_is_not_by_day(monkeypatch):
    cur = _patch_db(monkeypatch)
    out = dr.latest_daily_report_logs(limit=5)
    assert out.get("by_day") is False
    assert "DISTINCT ON" not in "\n".join(cur.sqls)


def test_narrative_speaks_breakers_and_freshness():
    r = {
        "date": "2026-07-08",
        "identity": {"pr_version": 159, "health_score": 95, "health_status": "healthy"},
        "decisions": {"picks_fired_today": 0, "scan_cycles_today": 44, "symbols_scanned": 74,
                      "gate_reason": "v3_meta_gate", "regime": "Neutral",
                      "latest_cycle_age_seconds": 300, "top_skip_reasons": {"no_v3_model": 800}},
        "wallet": {"error": "skip"},
        "calibration": {},
        "breakers": {"yfinance": "open", "alpaca": "open", "finnhub": "closed"},
    }
    txt = " ".join(dr._narrate(r))
    assert "Freshness: last scan 5 min ago" in txt
    assert "scanning live" in txt
    assert "Data feeds: DEGRADED" in txt
    assert "yfinance" in txt and "alpaca" in txt


def test_narrative_flags_stale_scan_and_healthy_feeds():
    r = {
        "date": "2026-07-08", "identity": {},
        "decisions": {"picks_fired_today": 0, "scan_cycles_today": 1, "symbols_scanned": 74,
                      "gate_reason": "v3_meta_gate", "regime": "Neutral",
                      "latest_cycle_age_seconds": 5400, "top_skip_reasons": {}},
        "wallet": {"error": "skip"}, "calibration": {},
        "breakers": {"yfinance": "closed", "alpaca": "closed"},
    }
    txt = " ".join(dr._narrate(r))
    assert "STALE — no recent scan" in txt
    assert "Data feeds: all healthy" in txt


def test_daily_report_routes(monkeypatch):
    from fastapi.testclient import TestClient
    from wolf_app import APP

    monkeypatch.setattr("core.daily_report.build_daily_report", lambda day=None: {"ok": True, "date": day or "today"})
    monkeypatch.setattr("core.daily_report.latest_daily_report_logs", lambda limit=24, day=None, include_payload=False, by_day=False: {
        "ok": True, "limit": limit, "date": day, "include_payload": include_payload, "by_day": by_day, "rows": []
    })
    monkeypatch.setattr("wolf_app._cron_ok", lambda secret: secret == "ok")
    monkeypatch.setattr("core.daily_report.snapshot_daily_report", lambda day=None: {"ok": True, "report_date": day})

    c = TestClient(APP)
    assert c.get("/api/report/daily?day=2026-07-08").json()["date"] == "2026-07-08"
    logs = c.get("/api/report/daily/logs?limit=3&day=2026-07-08&include_payload=1").json()
    assert logs["limit"] == 3 and logs["include_payload"] is True
    by_day = c.get("/api/report/daily/logs?limit=7&by_day=1").json()
    assert by_day["by_day"] is True and by_day["limit"] == 7
    assert c.post("/api/report/daily/snapshot").status_code == 403
    assert c.post("/api/report/daily/snapshot?day=2026-07-08", headers={"x-cron-secret": "ok"}).json()["report_date"] == "2026-07-08"
