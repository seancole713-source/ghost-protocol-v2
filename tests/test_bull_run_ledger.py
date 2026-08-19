"""Tests for immutable bull-run scenario snapshots and outcome resolution."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from core import bull_run_checklist as bc
from core import bull_run_ledger as bl


CT = ZoneInfo("America/Chicago")


def _ct_ts(date_key: str, hour: int, minute: int) -> int:
    return int(datetime.fromisoformat(date_key).replace(
        hour=hour,
        minute=minute,
        tzinfo=CT,
    ).timestamp())


def _record(value, *, ts=None, provenance="auto_market", status="CONFIRMED", unit="reported unit"):
    observed_at = ts or _ct_ts("2026-08-19", 8, 45)
    return {
        "value": value,
        "actual_value": value,
        "expected_value": 0.0,
        "source": "test",
        "as_of_ts": observed_at,
        "source_timestamp": observed_at,
        "observation_timestamp": observed_at,
        "reporting_period": "2026-Q2",
        "currency": "N/A",
        "unit": unit,
        "basis": "reported",
        "calculation_methodology": "test fixture",
        "status": status,
        "confidence_status": status,
        "provenance": provenance,
    }


class _Cursor:
    def __init__(self, *, fetchall_rows=None, fetchone_rows=None):
        self.fetchall_rows = list(fetchall_rows or [])
        self.fetchone_rows = list(fetchone_rows or [])
        self.executed = []
        self.params = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        self.params.append(params)

    def fetchall(self):
        return self.fetchall_rows

    def fetchone(self):
        if self.fetchone_rows:
            return self.fetchone_rows.pop(0)
        return None


def _client():
    from api.routes_ghost_system import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_phase_windows_are_preregistered_and_non_backfilling():
    assert bl.phase_for_ts(_ct_ts("2026-08-18", 15, 15)) == "pre_release_close"
    assert bl.phase_for_ts(_ct_ts("2026-08-18", 16, 0)) is None
    assert bl.phase_for_ts(_ct_ts("2026-08-19", 6, 15)) == "post_release"
    assert bl.phase_for_ts(_ct_ts("2026-08-19", 7, 30)) == "premarket"
    assert bl.phase_for_ts(_ct_ts("2026-08-19", 8, 45)) == "open_15"
    assert bl.phase_for_ts(_ct_ts("2026-08-19", 9, 30)) == "open_60"
    assert bl.phase_for_ts(_ct_ts("2026-08-19", 15, 10)) == "close"
    assert bl.phase_for_ts(_ct_ts("2026-08-20", 8, 45)) is None


def test_ensure_tables_creates_snapshot_and_resolution_ledgers():
    cur = _Cursor()
    bl.ensure_bull_run_tables(cur)
    sql = " ".join(cur.executed)
    assert "CREATE TABLE IF NOT EXISTS ghost_bull_run_scenario_snapshots" in sql
    assert "UNIQUE (scenario_id, scoring_version, slot_key)" in sql
    assert "CREATE TABLE IF NOT EXISTS ghost_bull_run_scenario_resolutions" in sql
    assert "CREATE TABLE IF NOT EXISTS ghost_bull_run_evidence_claims" in sql
    assert "CREATE TABLE IF NOT EXISTS ghost_bull_run_evidence_conflicts" in sql


def test_startup_migration_persists_quote_training_conflict_idempotently():
    cur = _Cursor()
    bl.ensure_bull_run_tables(cur)
    claim_inserts = [
        params for sql, params in zip(cur.executed, cur.params)
        if "INSERT INTO ghost_bull_run_evidence_claims" in sql
    ]
    conflict_inserts = [
        params for sql, params in zip(cur.executed, cur.params)
        if "INSERT INTO ghost_bull_run_evidence_conflicts" in sql
    ]
    expected_receipt_ts = int(
        datetime(2026, 8, 19, 13, 13, 5, tzinfo=timezone.utc).timestamp()
    )
    claim_corrections = [
        params for sql, params in zip(cur.executed, cur.params)
        if "UPDATE ghost_bull_run_evidence_claims SET captured_at" in sql
    ]
    conflict_corrections = [
        params for sql, params in zip(cur.executed, cur.params)
        if "UPDATE ghost_bull_run_evidence_conflicts SET captured_at" in sql
    ]
    assert bl._TRAINING_CONFLICT_CAPTURED_AT == expected_receipt_ts
    assert len(claim_corrections) == 1
    assert claim_corrections[0][0] == expected_receipt_ts
    assert len(claim_corrections[0][1]) == 2
    assert claim_corrections[0][2] == expected_receipt_ts
    assert len(conflict_corrections) == 1
    assert conflict_corrections[0][0] == expected_receipt_ts
    assert conflict_corrections[0][2] == expected_receipt_ts
    assert sorted(params[8] for params in claim_inserts) == ["8.79", "9.04"]
    assert all(params[11] == expected_receipt_ts for params in claim_inserts)
    assert all(params[9] == "UNVERIFIED" for params in claim_inserts)
    assert len(conflict_inserts) == 1
    assert conflict_inserts[0][8] == expected_receipt_ts
    conflict = json.loads(conflict_inserts[0][7])
    assert conflict["record_type"] == "DATA_CONFLICT"
    assert conflict["resolution_status"] == "UNRESOLVED"
    assert {conflict["value_a"], conflict["value_b"]} == {8.79, 9.04}


def test_capture_phase_persists_data_unavailable(monkeypatch):
    now = _ct_ts("2026-08-19", 8, 45)
    monkeypatch.setattr(bl, "fetch_auto_evidence", lambda _symbol: ({}, {}))
    cur = _Cursor(fetchone_rows=[(1,)])
    out = bl.capture_snapshot(phase="open_15", now_ts=now, cur=cur)
    assert out["inserted"] is True
    assert out["observation_status"] == "data_unavailable"
    assert "live point-in-time price" in out["note"]
    insert_params = next(
        params for sql, params in zip(cur.executed, cur.params)
        if "INSERT INTO ghost_bull_run_scenario_snapshots" in sql
    )
    assert insert_params[12] == "data_unavailable"


def test_capture_snapshot_is_inserted_once_per_phase(monkeypatch):
    now = _ct_ts("2026-08-19", 8, 45)
    auto = {
        "live_price": _record(9.75, ts=now),
        "price_change_pct": _record(7.0, ts=now),
    }
    monkeypatch.setattr(bl, "fetch_auto_evidence", lambda _symbol: (auto, {}))
    cur = _Cursor(fetchone_rows=[(41,)])
    out = bl.capture_snapshot(phase="open_15", now_ts=now, cur=cur)
    assert out["inserted"] is True
    assert out["snapshot_id"] == 41
    assert out["slot_key"] == "scheduled:open_15"
    insert_params = next(
        params for sql, params in zip(cur.executed, cur.params)
        if "INSERT INTO ghost_bull_run_scenario_snapshots" in sql
    )
    assert insert_params[3] == "open_15"
    assert insert_params[4] == "scheduled:open_15"


def test_capture_duplicate_reports_idempotently(monkeypatch):
    now = _ct_ts("2026-08-19", 9, 30)
    monkeypatch.setattr(
        bl,
        "fetch_auto_evidence",
        lambda _symbol: ({"live_price": _record(10.0, ts=now)}, {}),
    )
    out = bl.capture_snapshot(phase="open_60", now_ts=now, cur=_Cursor())
    assert out["inserted"] is False
    assert out["duplicate"] is True


def test_premarket_and_operator_evidence_are_sticky():
    premarket = {"premarket_gap_pct": _record(8.0)}
    operator = {
        "guidance_outcome": _record("raised", provenance=bc.OPERATOR_PROVENANCE),
        "revenue_actual_usd_m": _record(480.0, provenance=bc.OPERATOR_PROVENANCE),
    }
    transient = {"live_price": _record(9.80), "relative_volume": _record(3.0)}
    cur = _Cursor(fetchall_rows=[
        ("premarket_gap_pct", json.dumps(premarket["premarket_gap_pct"])),
        ("guidance_outcome", json.dumps(operator["guidance_outcome"])),
        ("revenue_actual_usd_m", json.dumps(operator["revenue_actual_usd_m"])),
        ("live_price", json.dumps(transient["live_price"])),
        ("relative_volume", json.dumps(transient["relative_volume"])),
    ])
    preserved = bl._load_preserved_evidence_cur(cur)
    assert preserved["premarket_gap_pct"]["value"] == 8.0
    assert preserved["guidance_outcome"]["value"] == "raised"
    assert preserved["revenue_actual_usd_m"]["value"] == 480.0
    assert "live_price" not in preserved
    assert "relative_volume" not in preserved


def test_preserved_evidence_conflicts_instead_of_latest_value_winning():
    newer = _record("raised", ts=_ct_ts("2026-08-19", 7, 0), provenance=bc.OPERATOR_PROVENANCE)
    older_inserted_later = _record(
        "maintained",
        ts=_ct_ts("2026-08-19", 6, 30),
        provenance=bc.OPERATOR_PROVENANCE,
    )
    cur = _Cursor(fetchall_rows=[
        ("guidance_outcome", json.dumps(newer)),
        ("guidance_outcome", json.dumps(older_inserted_later)),
    ])
    preserved = bl._load_preserved_evidence_cur(cur)
    assert preserved["guidance_outcome"]["status"] == "VERIFIED_CONFLICT"
    assert len(preserved["guidance_outcome"]["claims"]) == 2
    assert len(preserved["guidance_outcome"]["data_conflict"]) == 1


def test_preserved_evidence_falls_back_to_legacy_snapshots():
    legacy_gap = _record(8.0, status="UNVERIFIED")

    class LegacyCursor(_Cursor):
        def fetchall(self):
            if "FROM ghost_bull_run_evidence_claims" in self.executed[-1]:
                return []
            return [(json.dumps({"premarket_gap_pct": legacy_gap}),)]

    cur = LegacyCursor()
    preserved = bl._load_preserved_evidence_cur(cur)
    assert preserved["premarket_gap_pct"]["value"] == 8.0
    assert preserved["premarket_gap_pct"]["status"] == "UNVERIFIED"
    assert "FROM ghost_bull_run_scenario_snapshots" in cur.executed[-1]


def test_current_report_merges_preserved_premarket(monkeypatch):
    monkeypatch.setattr(
        bl,
        "fetch_auto_evidence",
        lambda _symbol: ({"live_price": _record(10.1)}, {}),
    )
    monkeypatch.setattr(
        bl,
        "load_preserved_evidence",
        lambda: {"premarket_gap_pct": _record(8.0)},
    )
    out = bl.current_scenario_report("YMM")
    gap = next(item for item in out["checks"] if item["key"] == "premarket_gap")
    assert gap["state"] == "very_green"
    assert out["source_status"]["ledger"]["preserved_keys"] == ["premarket_gap_pct"]


def test_current_report_marks_preserved_earnings_evidence_available(monkeypatch):
    monkeypatch.setattr(bl, "fetch_auto_evidence", lambda _symbol: ({}, {
        "earnings": {"available": False, "reason": "operator evidence required"},
    }))
    monkeypatch.setattr(bl, "load_preserved_evidence", lambda: {
        "revenue_actual_usd_m": _record(480.0, provenance=bc.OPERATOR_PROVENANCE),
    })
    out = bl.current_scenario_report("YMM")
    assert out["source_status"]["earnings"]["available"] is True
    assert out["source_status"]["earnings"]["keys"] == ["revenue_actual_usd_m"]


def test_operator_slot_is_content_idempotent():
    first = {"guidance_outcome": _record("raised", provenance=bc.OPERATOR_PROVENANCE)}
    same = {"guidance_outcome": dict(first["guidance_outcome"])}
    changed = {"guidance_outcome": _record("maintained", provenance=bc.OPERATOR_PROVENANCE)}
    assert bl._operator_slot_key(first) == bl._operator_slot_key(same)
    assert bl._operator_slot_key(first) != bl._operator_slot_key(changed)


def test_outcome_waits_for_five_exact_sessions():
    missing_monday = [
        {"ts": date_key, "high": 10, "low": 8, "close": 9}
        for date_key in ("2026-08-19", "2026-08-20", "2026-08-21", "2026-08-22", "2026-08-25")
    ]
    assert bl._resolve_from_series(missing_monday) is None


def test_outcome_computes_target_hit_and_excursions():
    closes = [9.2, 10.0, 11.0, 11.5, 11.8]
    series = [
        {
            "ts": date_key,
            "high": 12.2 if i == 3 else close + 0.2,
            "low": 8.2 if i == 0 else close - 0.2,
            "close": close,
        }
        for i, (date_key, close) in enumerate(zip(bl._RESOLUTION_DATES, closes))
    ]
    out = bl._resolve_from_series(series)
    assert out is not None
    assert out["price_1d"] == 9.2
    assert out["price_horizon"] == 11.8
    assert out["hit_target"] is True
    assert out["return_horizon_pct"] == 34.091
    assert out["max_favorable_pct"] == 38.636


def test_resolver_waits_until_the_fifth_session_has_closed(monkeypatch):
    monkeypatch.setattr(
        "core.squeeze_hunter_ledger._ohlc_series",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("history fetched early")),
    )
    out = bl.resolve_scenario(now_ts=_ct_ts("2026-08-25", 15, 0))
    assert out["resolved"] is False
    assert "still open" in out["note"]


def test_resolver_stops_fetching_after_outcome_exists(monkeypatch):
    monkeypatch.setattr(bl, "_resolution_exists", lambda: True)
    monkeypatch.setattr(
        "core.squeeze_hunter_ledger._ohlc_series",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("history refetched")),
    )
    out = bl.resolve_scenario(now_ts=_ct_ts("2026-08-25", 16, 0))
    assert out["duplicate"] is True
    assert "already resolved" in out["note"]


def test_resolver_insert_is_idempotent():
    series = [
        {"ts": date_key, "high": 10 + i, "low": 8, "close": 9 + i}
        for i, date_key in enumerate(bl._RESOLUTION_DATES)
    ]
    inserted = bl.resolve_scenario(series=series, now_ts=123, cur=_Cursor(fetchone_rows=[(7,)]))
    assert inserted["resolved"] is True
    assert inserted["resolution_id"] == 7
    duplicate = bl.resolve_scenario(series=series, now_ts=123, cur=_Cursor())
    assert duplicate["resolved"] is False
    assert duplicate["duplicate"] is True


def test_elapsed_job_records_missed_without_fetching_current_evidence(monkeypatch):
    now = _ct_ts("2026-08-20", 12, 0)
    monkeypatch.setattr(
        bl,
        "fetch_auto_evidence",
        lambda _symbol: (_ for _ in ()).throw(AssertionError("present-day evidence fetched")),
    )
    captured = []

    def fake_capture(**kwargs):
        captured.append(kwargs)
        return {
            "ok": True,
            "inserted": True,
            "phase": kwargs["phase"],
            "observation_status": kwargs["observation_status"],
        }

    monkeypatch.setattr(bl, "capture_snapshot", fake_capture)
    out = bl.run_snapshot_job(now_ts=now)
    assert out["ok"] is True
    assert [item["phase"] for item in captured] == list(bl.SCHEDULED_PHASES)
    assert all(item["fetch_evidence"] is False for item in captured)
    assert all(item["observation_status"] == "missed_no_observation" for item in captured)


def test_runtime_paths_do_not_execute_ddl(monkeypatch):
    now = _ct_ts("2026-08-19", 8, 45)
    monkeypatch.setattr(
        bl,
        "fetch_auto_evidence",
        lambda _symbol: ({"live_price": _record(9.5, ts=now)}, {}),
    )
    cur = _Cursor(fetchone_rows=[(2,)])
    bl.capture_snapshot(phase="open_15", now_ts=now, cur=cur)
    assert not any("CREATE TABLE" in sql or "ALTER TABLE" in sql for sql in cur.executed)


def test_database_failure_raises_sanitized_error(monkeypatch):
    class BadConnection:
        def __enter__(self):
            raise RuntimeError("secret database detail")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("core.db.db_conn", lambda: BadConnection())
    with pytest.raises(bl.BullRunDatabaseError, match="database_unavailable"):
        bl.load_preserved_evidence()


def test_scheduler_and_migration_are_wired():
    scheduler_source = open("wolf_app.py", encoding="utf-8").read()
    db_source = open("core/db.py", encoding="utf-8").read()
    assert 'scheduler.register("bull_run_snapshot"' in scheduler_source
    assert 'scheduler.register("bull_run_resolver"' in scheduler_source
    assert "ensure_bull_run_tables" in db_source


def test_ledger_routes_are_registered():
    from api.routes_ghost_system import router

    methods = {route.path: route.methods for route in router.routes}
    assert methods["/api/bull-run/checklist/{symbol}/snapshot"] == {"POST"}
    assert methods["/api/bull-run/checklist/{symbol}/ledger"] == {"GET"}
    assert methods["/api/bull-run/checklist/snapshot-run"] == {"POST"}
    assert methods["/api/bull-run/checklist/resolve"] == {"POST"}


def test_operator_snapshot_route_is_auth_gated(monkeypatch):
    monkeypatch.setattr("wolf_app._cron_ok", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("wolf_app._admin_token_valid", lambda _token: False)
    response = _client().post("/api/bull-run/checklist/YMM/snapshot", json={})
    assert response.status_code == 404


def test_operator_snapshot_route_persists_validated_evidence(monkeypatch):
    monkeypatch.setattr("wolf_app._cron_ok", lambda secret, strict=False: secret == "ok")
    monkeypatch.setattr(bc, "_now", lambda: bc._EVENT_EVIDENCE_NOT_BEFORE_TS + 3600)
    monkeypatch.setattr(
        bl,
        "capture_snapshot",
        lambda **kwargs: {"ok": True, "inserted": True, "keys": sorted(kwargs["operator_evidence"])},
    )
    payload = {
        "scenario_id": bc.SCENARIO["scenario_id"],
        "period": bc.SCENARIO["period"],
        "evidence": {
            "guidance_outcome": {
                "value": "raised",
                "source": "https://ir.fulltruckalliance.com/2026-08-19-Full-Truck-Alliance-Q2-Results",
                "as_of_ts": bc._EVENT_EVIDENCE_NOT_BEFORE_TS + 300,
                "source_timestamp": bc._EVENT_EVIDENCE_NOT_BEFORE_TS + 300,
                "observation_timestamp": bc._EVENT_EVIDENCE_NOT_BEFORE_TS + 300,
                "unit": "categorical outlook",
                "currency": "N/A",
                "basis": "reported",
                "calculation_methodology": "operator-transcribed claim",
                "expected_value": "maintained",
            }
        },
    }
    response = _client().post(
        "/api/bull-run/checklist/YMM/snapshot",
        headers={"X-Cron-Secret": "ok"},
        json=payload,
    )
    assert response.status_code == 200
    assert response.json()["inserted"] is True
    assert response.json()["keys"] == ["guidance_outcome"]
