"""roadmap #3b — daily summary aggregation + endpoint. Separate test file."""
import json
import time

import wolf_app


def _gate_hist_today():
    now = int(time.time())
    return json.dumps([
        {"ts": now - 100, "candidates": 1, "saved": 1, "would_fire": True},
        {"ts": now - 200, "candidates": 0, "saved": 0, "would_fire": False},
        {"ts": now - 99 * 86400, "candidates": 5, "saved": 5, "would_fire": True},  # old, excluded
    ])


def _wire_db(monkeypatch, gate_json, resolved_rows):
    class _Cur:
        last = ""
        def execute(self, sql, params=None): self.last = sql
        def fetchone(self):
            if "gate_outcome_history" in self.last:
                return (gate_json,)
            return None
        def fetchall(self):
            if "FROM predictions" in self.last:
                return resolved_rows
            return []

    class _Conn:
        def cursor(self): return _Cur()

    class _Ctx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _Ctx())


def test_build_daily_summary_aggregates_today(monkeypatch):
    import core.prediction as _pred
    monkeypatch.setattr(_pred, "engine_pause_state", lambda: {"paused": False})
    _wire_db(monkeypatch, _gate_hist_today(), [("WIN", 6.0), ("LOSS", -3.0), ("WIN", 4.0)])
    s = wolf_app._build_daily_summary()
    assert s["scans"] == 2                 # old cycle excluded
    assert s["candidates"] == 1
    assert s["saved"] == 1
    assert s["would_fire_cycles"] == 1
    assert s["resolved"]["wins"] == 2 and s["resolved"]["losses"] == 1
    assert abs(s["resolved"]["pnl_pct"] - 7.0) < 1e-6
    assert s["engine_paused"] is False
    assert "date" in s


def test_daily_summary_endpoint_returns_history(monkeypatch):
    import core.prediction as _pred
    monkeypatch.setattr(_pred, "engine_pause_state", lambda: {"paused": False})
    history = [{"date": "2026-05-22", "scans": 10, "saved": 1},
               {"date": "2026-05-23", "scans": 12, "saved": 0}]

    class _Cur:
        last = ""
        def execute(self, sql, params=None): self.last = sql
        def fetchone(self):
            if "daily_summary_history" in self.last:
                return (json.dumps(history),)
            return None
        def fetchall(self): return []

    class _Conn:
        def cursor(self): return _Cur()

    class _Ctx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _Ctx())
    out = wolf_app.wolf_daily_summary(limit=30)
    assert out["ok"] is True
    assert out["count"] == 2
    assert out["days"][0]["date"] == "2026-05-23"   # newest first
    assert "today" in out


def test_daily_summary_job_skips_off_hour(monkeypatch):
    """Off the configured hour => no store (no DB writes)."""
    monkeypatch.setenv("DAILY_SUMMARY_ENABLED", "1")
    monkeypatch.setenv("DAILY_SUMMARY_HOUR", "16")
    import datetime as _dt

    class _FixedDT(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _dt.datetime(2026, 5, 23, 9, 0, 0, tzinfo=tz)   # 9 AM, not 16

    monkeypatch.setattr(_dt, "datetime", _FixedDT)
    wrote = {"n": 0}

    class _Cur:
        def execute(self, sql, params=None):
            if sql.strip().startswith("INSERT"):
                wrote["n"] += 1
        def fetchone(self): return None

    class _Conn:
        def cursor(self): return _Cur()

    class _Ctx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _Ctx())
    wolf_app._daily_summary_job()
    assert wrote["n"] == 0
