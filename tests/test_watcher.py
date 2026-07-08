"""PR #153: Watcher read-only calibration observer."""
from core.watcher import brier_score, calibration_bins, summarize_shadow_outcomes, watcher_verdict


def test_watcher_calibration_bins_and_brier():
    rows = [
        {"prob": 0.56, "win": True},
        {"prob": 0.57, "win": False},
        {"prob": 0.72, "win": True},
        {"prob": 0.74, "win": True},
        {"prob": None, "win": True},
    ]
    bins = calibration_bins(rows)
    hi = next(b for b in bins if b["label"] == "70+")
    assert hi["n"] == 2 and hi["wins"] == 2 and hi["win_rate"] == 1.0
    assert brier_score(rows) is not None


def test_watcher_verdict_real_but_not_70():
    out = watcher_verdict(high_win_rate=0.62, high_n=91, brier=0.24)
    assert out["status"] == "real_but_not_70"
    assert "not yet 70" in out["headline"]


def test_watcher_summary_shadow_outcomes_high_confidence():
    rows = []
    for i in range(54):
        rows.append({"symbol": "WOLF", "up_prob": 0.60, "outcome": "WIN"})
    for i in range(33):
        rows.append({"symbol": "WOLF", "up_prob": 0.60, "outcome": "LOSS"})
    rows.append({"symbol": "WOLF", "up_prob": 0.50, "outcome": "LOSS"})
    out = summarize_shadow_outcomes(rows)
    assert out["resolved_n"] == 88
    assert out["high_confidence"]["n"] == 87
    assert 0.61 <= out["high_confidence"]["win_rate"] <= 0.63
    assert out["verdict"]["status"] == "real_but_not_70"


def test_watcher_endpoint_routes(monkeypatch):
    from fastapi.testclient import TestClient
    from wolf_app import APP

    monkeypatch.setattr("core.watcher.watcher_summary", lambda days=30, limit=5000: {
        "ok": True, "read_only": True, "days": days, "limit_seen": limit,
    })
    r = TestClient(APP).get("/api/watcher/summary?days=7&limit=123")
    assert r.status_code == 200
    assert r.json()["read_only"] is True
    assert r.json()["days"] == 7


def test_watcher_snapshot_writes_only_notebook_table(monkeypatch):
    import core.watcher as w
    calls = []

    class Cur:
        def execute(self, sql, params=None):
            calls.append((sql, params))
        def fetchall(self):
            return []
    class Conn:
        def cursor(self): return Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(w, "watcher_summary", lambda days=30: {
        "ok": True, "days": days,
        "shadow_calibration": {"resolved_n": 87, "brier": 0.23,
            "high_confidence": {"n": 87, "win_rate": 0.6207},
            "verdict": {"status": "real_but_not_70"}},
    })
    import core.db as db
    monkeypatch.setattr(db, "db_conn", lambda: Conn())
    out = w.snapshot_watcher(days=30)
    assert out["ok"] is True and out["read_only_decisions"] is True
    joined = "\n".join(sql for sql, _ in calls)
    assert "ghost_watcher_snapshots" in joined
    assert "predictions" not in joined
    assert "ghost_shadow_outcomes" not in joined  # snapshot path uses summary output only in this test


def test_watcher_snapshots_endpoint_routes(monkeypatch):
    from fastapi.testclient import TestClient
    from wolf_app import APP
    monkeypatch.setattr("core.watcher.latest_watcher_snapshots", lambda limit=20: {"ok": True, "rows": [{"limit": limit}]})
    r = TestClient(APP).get("/api/watcher/snapshots?limit=3")
    assert r.status_code == 200
    assert r.json()["rows"][0]["limit"] == 3
