import datetime as dt

import pytz

import core.daily_model_issuance as issuance


class _Cursor:
    def __init__(self, state):
        self.state = state
        self.selected = None

    def execute(self, sql, params=None):
        if sql.startswith("SELECT"):
            self.selected = params[0]
        elif sql.startswith("INSERT"):
            self.state[params[0]] = params[1]

    def fetchone(self):
        value = self.state.get(self.selected)
        return (value,) if value is not None else None


class _Connection:
    def __init__(self, state):
        self.state = state

    def cursor(self):
        return _Cursor(self.state)


class _Context:
    def __init__(self, state):
        self.state = state

    def __enter__(self):
        return _Connection(self.state)

    def __exit__(self, *_args):
        return False


def _ct(hour, minute):
    return pytz.timezone("America/Chicago").localize(
        dt.datetime(2026, 8, 27, hour, minute),
    )


def test_daily_issuance_runs_once_and_claims_only_after_success(monkeypatch):
    state = {}
    calls = []
    monkeypatch.setattr("core.db.db_conn", lambda: _Context(state))
    monkeypatch.setattr("core.db.ensure_ghost_state", lambda cur: None)

    first = issuance.run_daily_model_issuance(
        now=_ct(15, 10), cycle=lambda: calls.append("ran") or [{"id": 1}],
    )
    second = issuance.run_daily_model_issuance(
        now=_ct(15, 20), cycle=lambda: calls.append("duplicate") or [],
    )

    assert first == {
        "ok": True, "ran": True, "session_date": "2026-08-27", "saved": 1,
        "symbols_scanned": None,
    }
    assert second["reason"] == "already_issued"
    assert calls == ["ran"]


def test_daily_issuance_is_noop_outside_frozen_window(monkeypatch):
    monkeypatch.setattr(
        "core.db.db_conn",
        lambda: (_ for _ in ()).throw(AssertionError("DB must not be touched")),
    )
    out = issuance.run_daily_model_issuance(now=_ct(14, 55), cycle=lambda: [])
    assert out["ran"] is False
    assert out["reason"] == "outside_issuance_window"


def test_daily_issuance_retries_when_entire_default_scan_has_no_data(monkeypatch):
    state = {}
    monkeypatch.setattr("core.db.db_conn", lambda: _Context(state))
    monkeypatch.setattr("core.db.ensure_ghost_state", lambda cur: None)

    def failed_scan(*, with_diag):
        assert with_diag is True
        return [], {
            "symbols_scanned": 104,
            "skip_counts": {"no_price": 103, "v3_engine_error": 1},
        }

    monkeypatch.setattr("core.prediction.run_prediction_cycle", failed_scan)
    out = issuance.run_daily_model_issuance(now=_ct(15, 10))

    assert out["ok"] is False
    assert out["reason"] == "scan_data_unavailable"
    assert state == {}


def test_daily_issuance_claims_valid_no_trade_cycle(monkeypatch):
    state = {}
    monkeypatch.setattr("core.db.db_conn", lambda: _Context(state))
    monkeypatch.setattr("core.db.ensure_ghost_state", lambda cur: None)
    monkeypatch.setattr(
        "core.prediction.run_prediction_cycle",
        lambda *, with_diag: (
            [], {"symbols_scanned": 104, "skip_counts": {"v3_prob_low": 104}},
        ),
    )

    out = issuance.run_daily_model_issuance(now=_ct(15, 10))

    assert out["ok"] is True
    assert out["ran"] is True
    assert out["saved"] == 0
    assert state[issuance._STATE_KEY] == "2026-08-27"
