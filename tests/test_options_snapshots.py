"""tests/test_options_snapshots.py — daily point-in-time options snapshots.

Forward evidence collector: pure chain math, once-per-day self-gating,
upsert-per-(symbol,date) storage, read-only history endpoint.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

import core.options_snapshots as osnap


# ── Pure chain math ──────────────────────────────────────────────────

def _chain_df(volumes, ois, strikes, ivs):
    return pd.DataFrame({"volume": volumes, "openInterest": ois,
                         "strike": strikes, "impliedVolatility": ivs})


class TestChainMetrics:
    def test_pcr_and_atm_iv(self):
        calls = _chain_df([100, 200], [1000, 500], [10.0, 12.0], [0.50, 0.60])
        puts = _chain_df([150, 150], [3000, 0], [10.0, 12.0], [0.70, 0.80])
        m = osnap.compute_chain_metrics(calls, puts, underlying=11.8)
        assert m["call_volume"] == 300 and m["put_volume"] == 300
        assert m["pcr_volume"] == 1.0
        assert m["pcr_oi"] == 2.0          # 3000 / 1500
        assert m["atm_iv_call"] == 0.60    # strike 12 is nearest to 11.8
        assert m["atm_iv_put"] == 0.80
        assert m["available"] is True

    def test_empty_chain_is_valid_unavailable_row(self):
        m = osnap.compute_chain_metrics(None, None, None)
        assert m["available"] is False
        assert m["pcr_volume"] is None and m["pcr_oi"] is None
        assert m["atm_iv_call"] is None

    def test_nan_volumes_treated_as_zero(self):
        calls = _chain_df([float("nan")], [float("nan")], [5.0], [0.4])
        m = osnap.compute_chain_metrics(calls, None, underlying=None)
        assert m["call_volume"] == 0
        assert m["available"] is False

    def test_absurd_iv_rejected(self):
        calls = _chain_df([10], [10], [5.0], [50.0])   # 5000% IV = junk
        m = osnap.compute_chain_metrics(calls, None, underlying=5.0)
        assert m["atm_iv_call"] is None


# ── Job self-gating ──────────────────────────────────────────────────

def _at(monkeypatch, wday: int, hour: int):
    # 2026-07-13 is a Monday; add wday for the target weekday.
    fake = datetime(2026, 7, 13 + wday, hour, 30,
                    tzinfo=ZoneInfo("America/Chicago"))
    monkeypatch.setattr(osnap, "_ct_now", lambda: fake)


class TestJobGating:
    def test_weekend_skip(self, monkeypatch):
        _at(monkeypatch, wday=5, hour=14)   # Saturday
        assert osnap.run_options_snapshot_job()["skipped"] == "weekend"

    def test_outside_window_skip(self, monkeypatch):
        _at(monkeypatch, wday=0, hour=9)    # Monday 09:30 CT
        out = osnap.run_options_snapshot_job()
        assert out["skipped"] == "outside_snapshot_window"

    def test_already_ran_today_skip(self, monkeypatch):
        _at(monkeypatch, wday=0, hour=14)

        class _Cur:
            def execute(self, sql, params=None): pass
            def fetchone(self): return ("2026-07-13",)

        class _Conn:
            def cursor(self): return _Cur()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        import core.db as db
        monkeypatch.setattr(db, "db_conn", lambda: _Conn())
        assert osnap.run_options_snapshot_job()["skipped"] == "already_ran_today"

    def test_claims_day_before_fetch_loop(self, monkeypatch):
        _at(monkeypatch, wday=0, hour=14)
        executed = []

        class _Cur:
            def execute(self, sql, params=None): executed.append(sql)
            def fetchone(self): return None

        class _Conn:
            def cursor(self): return _Cur()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        import core.db as db
        monkeypatch.setattr(db, "db_conn", lambda: _Conn())
        monkeypatch.setattr(osnap, "record_snapshots",
                            lambda: {"ok": True, "stored": 0})
        out = osnap.run_options_snapshot_job()
        assert out["ok"] is True
        assert any("INSERT INTO ghost_state" in s for s in executed)


# ── Storage flow ─────────────────────────────────────────────────────

class TestRecordSnapshots:
    def test_upsert_and_counts(self, monkeypatch):
        executed = []

        class _Cur:
            def execute(self, sql, params=None): executed.append(sql)
            def fetchone(self): return None

        class _Conn:
            def cursor(self): return _Cur()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        import core.db as db
        monkeypatch.setattr(db, "db_conn", lambda: _Conn())

        snaps = {
            "AAA": {"symbol": "AAA", "snap_date": "2026-07-16", "ts": 1,
                    "nearest_expiry": "2026-07-18", "underlying": 10.0,
                    "call_volume": 5, "put_volume": 5, "call_oi": 1, "put_oi": 1,
                    "pcr_volume": 1.0, "pcr_oi": 1.0,
                    "atm_iv_call": 0.5, "atm_iv_put": 0.6, "available": True},
            "BBB": {"symbol": "BBB", "snap_date": "2026-07-16", "ts": 1,
                    "nearest_expiry": None, "underlying": None,
                    "call_volume": 0, "put_volume": 0, "call_oi": 0, "put_oi": 0,
                    "pcr_volume": None, "pcr_oi": None,
                    "atm_iv_call": None, "atm_iv_put": None, "available": False},
            "CCC": None,   # breaker open / fetch failed
        }
        monkeypatch.setattr(osnap, "snapshot_symbol", lambda s: snaps[s])
        out = osnap.record_snapshots(["AAA", "BBB", "CCC"], delay_s=0)
        assert out["stored"] == 2
        assert out["empty_chain"] == 1
        assert out["failed"] == 1
        assert any("CREATE TABLE IF NOT EXISTS ghost_options_snapshots" in s
                   for s in executed)
        assert sum("ON CONFLICT(symbol, snap_date)" in s for s in executed) == 2


# ── Routes + scheduler wiring ────────────────────────────────────────

class TestWiring:
    def test_routes_registered(self):
        from api.routes_ghost_system import router
        paths = [r.path for r in router.routes]
        assert "/api/ghost/options/snapshots" in paths
        assert "/api/ghost/options/snapshot-run" in paths

    def test_snapshot_run_requires_auth(self, monkeypatch):
        from fastapi.testclient import TestClient
        from wolf_app import APP

        monkeypatch.setattr("wolf_app._cron_ok", lambda secret, strict=False: False)
        monkeypatch.setattr("wolf_app._admin_token_valid", lambda tok: False)
        r = TestClient(APP).post("/api/ghost/options/snapshot-run")
        assert r.status_code == 403

    def test_scheduler_job_registered_in_source(self):
        import os
        path = os.path.join(os.path.dirname(__file__), "..", "wolf_app.py")
        with open(path) as f:
            src = f.read()
        assert 'scheduler.register("options_snapshots"' in src


# ── Alpaca options source (2026-07-18: replaced rate-limited yfinance) ──

class TestAlpacaAggregation:
    def test_pcr_from_occ_symbols(self):
        from core.options_snapshots import aggregate_alpaca_options
        snaps = {
            "AAPL260720C00210000": {"dailyBar": {"v": 100}},
            "AAPL260720P00210000": {"dailyBar": {"v": 150}},
            "AAPL260720C00205000": {"dailyBar": {"v": 50}},
            "AAPL260720C00215000": {"latestQuote": {"ap": 1}},  # no volume
            "NOT_AN_OCC_SYMBOL": {"dailyBar": {"v": 999}},       # ignored
        }
        m = aggregate_alpaca_options(snaps)
        assert m["call_volume"] == 150 and m["put_volume"] == 150
        assert m["pcr_volume"] == 1.0 and m["available"] is True
        # Alpaca snapshots carry no OI/IV — must be explicitly null, not faked.
        assert m["call_oi"] is None and m["atm_iv_call"] is None

    def test_empty_is_valid_unavailable(self):
        from core.options_snapshots import aggregate_alpaca_options
        m = aggregate_alpaca_options({})
        assert m["available"] is False and m["pcr_volume"] is None

    def test_puts_only_pcr_none_when_no_calls(self):
        from core.options_snapshots import aggregate_alpaca_options
        m = aggregate_alpaca_options({"X260720P00100000": {"dailyBar": {"v": 5}}})
        assert m["put_volume"] == 5 and m["call_volume"] == 0
        assert m["pcr_volume"] is None and m["available"] is True
