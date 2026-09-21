"""Kill-switch recency bound, and the pause-start timestamp.

Two defects found live on 2026-09-03 while production had been paused
continuously on `brier->degrade_watching`:

1. SELF-PERPETUATING PAUSE. The kill windows are COUNTS (30/30/3/20), not
   time, and evaluate_kill_conditions applied no age bound at all unless a
   manual resume had set since_ts (and even then only during a 24h grace).
   So the switch judged the live engine on the last N resolved picks however
   old they were. Because a pause suppresses firing, no new outcomes resolve,
   so the stale rows that tripped the switch stay the newest rows forever and
   the pause can never clear on its own — resume, wait out the grace, re-pause
   on the identical rows, repeat. Production's tripping brier was computed
   from picks resolved months earlier, against a since-retrained generation.

   The fix bounds admissible evidence by KILL_RECENCY_DAYS, mirroring the
   CB_RECENCY_DAYS guard the circuit breaker already had. It changes which
   outcomes count as evidence, never what counts as failing — no threshold
   moves.

2. FOREVER-ADVANCING "since". enforce_kill_conditions rewrote engine_pause_ts
   to now() on EVERY cycle while a pause persisted. The one-time Telegram
   alert was correctly guarded, but the timestamp write was not, so a pause
   explicitly reported as `latched: true` also reported a "since" that kept
   moving. Observed live: 1788448992 then 1788456424, +2h04m apart, with
   paused/latched true throughout and no resume in between. Nobody could tell
   how long the engine had actually been down.
"""
from __future__ import annotations

import time

import core.prediction as pred


# ------------------------------------------------------------------ helpers --

def _cfg(**over):
    base = {
        "enabled": True,
        "winrate_floor": 0.7,
        "winrate_window": 30,
        "brier_ceiling": 0.35,
        "brier_window": 30,
        "consec_losses": 3,
        "expectancy_window": 20,
        "cooldown_minutes": 1440,
        "min_samples": 10,
        "recency_days": 14,
    }
    base.update(over)
    return base


class _Recorder:
    """Captures every (sql, params) so a test can assert on the real query."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def _cursor(self):
        rec = self

        class Cur:
            def execute(self, sql, params=None):
                self.sql = sql
                rec.calls.append((sql, params))

            def fetchall(self):
                return rec.rows if "predictions" in getattr(self, "sql", "") else []

            def fetchone(self):
                return None

        return Cur()

    def ctx(self):
        rec = self

        class Conn:
            def cursor(self):
                return rec._cursor()

            def commit(self):
                pass

            def rollback(self):
                pass

        class Ctx:
            def __enter__(self):
                return Conn()

            def __exit__(self, *a):
                return False

        return Ctx()

    def predictions_call(self):
        for sql, params in self.calls:
            if "predictions" in sql:
                return sql, params
        raise AssertionError("no predictions query was issued")


# --------------------------------------------------------- config defaults --

def test_kill_recency_days_defaults_to_the_circuit_breaker_value(monkeypatch):
    """Same guard, same default, same disable semantics as CB_RECENCY_DAYS."""
    monkeypatch.delenv("KILL_RECENCY_DAYS", raising=False)
    assert pred._kill_cfg()["recency_days"] == 14


def test_kill_recency_days_env_override_and_disable(monkeypatch):
    monkeypatch.setenv("KILL_RECENCY_DAYS", "45")
    assert pred._kill_cfg()["recency_days"] == 45

    monkeypatch.setenv("KILL_RECENCY_DAYS", "0")
    assert pred._kill_cfg()["recency_days"] == 0

    monkeypatch.setenv("KILL_RECENCY_DAYS", "-5")
    assert pred._kill_cfg()["recency_days"] == 0


# ------------------------------------------------------------ recency bound --

def test_stale_outcomes_are_excluded_by_the_recency_floor(monkeypatch):
    """The regression: without a floor the query admitted any-age rows."""
    monkeypatch.setattr(pred, "_kill_cfg", lambda: _cfg(recency_days=14))
    monkeypatch.setattr(pred, "_kill_symbol_universe", lambda: ["WOLF"])
    rec = _Recorder([(0.9, "LOSS", -1.0)])
    monkeypatch.setattr(pred, "db_conn", lambda: rec.ctx())

    out = pred.evaluate_kill_conditions()
    sql, params = rec.predictions_call()

    assert "resolved_at >= %s" in sql, "no age bound applied to the evidence query"
    floor = params[1]
    expected = int(time.time()) - 14 * 86400
    assert abs(floor - expected) <= 5
    assert out["recency_days"] == 14
    assert out["resolved_since_ts"] == floor


def test_recency_zero_disables_the_floor(monkeypatch):
    """0 must restore the old unbounded behaviour, as CB_RECENCY_DAYS does."""
    monkeypatch.setattr(pred, "_kill_cfg", lambda: _cfg(recency_days=0))
    monkeypatch.setattr(pred, "_kill_symbol_universe", lambda: ["WOLF"])
    rec = _Recorder([(0.9, "LOSS", -1.0)])
    monkeypatch.setattr(pred, "db_conn", lambda: rec.ctx())

    out = pred.evaluate_kill_conditions()
    sql, _ = rec.predictions_call()

    assert "resolved_at >= %s" not in sql
    assert out["resolved_since_ts"] == 0


def test_explicit_resume_window_wins_when_tighter(monkeypatch):
    """since_ts and the recency floor coexist — the tighter one binds."""
    monkeypatch.setattr(pred, "_kill_cfg", lambda: _cfg(recency_days=14))
    monkeypatch.setattr(pred, "_kill_symbol_universe", lambda: ["WOLF"])
    rec = _Recorder([])
    monkeypatch.setattr(pred, "db_conn", lambda: rec.ctx())

    recent = int(time.time()) - 3600          # 1h ago, tighter than 14d
    pred.evaluate_kill_conditions(since_ts=recent)
    _, params = rec.predictions_call()
    assert params[1] == recent

    rec.calls.clear()
    ancient = int(time.time()) - 365 * 86400  # looser than 14d
    pred.evaluate_kill_conditions(since_ts=ancient)
    _, params = rec.predictions_call()
    assert params[1] > ancient, "recency floor must still bind when since_ts is older"


def test_no_recent_evidence_reads_insufficient_and_cannot_trip(monkeypatch):
    """The deadlock-breaker: aged-out evidence leaves nothing to trip on.

    Same behaviour the docstring already promised for cold start — a condition
    without enough evidence reads 'insufficient', not 'red'.
    """
    monkeypatch.setattr(pred, "_kill_cfg", lambda: _cfg(recency_days=14))
    monkeypatch.setattr(pred, "_kill_symbol_universe", lambda: ["WOLF"])
    rec = _Recorder([])          # every row aged out of the window
    monkeypatch.setattr(pred, "db_conn", lambda: rec.ctx())

    out = pred.evaluate_kill_conditions()

    assert out["ok"] is True
    assert out["resolved_available"] == 0
    assert out["any_triggered"] is False
    by_name = {c["name"]: c for c in out["conditions"]}
    for name in ("win_rate", "brier", "expectancy"):
        assert by_name[name]["status"] == "insufficient", name
        assert by_name[name]["triggered"] is False, name


def test_recency_bound_does_not_move_any_threshold(monkeypatch):
    """Guards the line between 'which evidence counts' and 'what counts as
    failing'. The thresholds must read identically with the floor on or off."""
    monkeypatch.setattr(pred, "_kill_symbol_universe", lambda: ["WOLF"])
    rows = [(0.9, "LOSS", -1.0)] * 30

    thresholds = {}
    for days in (0, 14):
        monkeypatch.setattr(pred, "_kill_cfg", lambda d=days: _cfg(recency_days=d))
        rec = _Recorder(list(rows))
        monkeypatch.setattr(pred, "db_conn", lambda r=rec: r.ctx())
        out = pred.evaluate_kill_conditions()
        thresholds[days] = {c["name"]: c["threshold"] for c in out["conditions"]}

    assert thresholds[0] == thresholds[14]


# --------------------------------------------------- pause-start timestamp --

def _enforce_with(monkeypatch, prev, tripped_rows):
    """Run enforce_kill_conditions against a fixed prior pause state."""
    monkeypatch.setattr(pred, "_kill_cfg", lambda: _cfg())
    monkeypatch.setattr(pred, "_kill_symbol_universe", lambda: ["WOLF"])
    monkeypatch.setattr(pred, "engine_pause_state", lambda: prev)
    monkeypatch.setattr(
        pred, "evaluate_kill_conditions",
        lambda **kw: {"ok": True, "conditions": tripped_rows},
    )
    monkeypatch.setattr(pred, "ensure_ghost_state", lambda cur: None)

    writes = []

    class Cur:
        def execute(self, sql, params=None):
            writes.append((sql, params))

        def fetchone(self):
            return None

        def fetchall(self):
            return []

    class Conn:
        def cursor(self):
            return Cur()

        def commit(self):
            pass

        def rollback(self):
            pass

    class Ctx:
        def __enter__(self):
            return Conn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(pred, "db_conn", lambda: Ctx())
    out = pred.enforce_kill_conditions()
    written_keys = [
        (p[0] if p else None) for sql, p in writes
        if p and "ghost_state" in sql and isinstance(p, tuple) and len(p) == 2
    ]
    return out, written_keys


_BRIER_TRIP = [{
    "name": "brier", "action": "degrade_watching", "triggered": True,
    "status": "red", "window": 30, "samples": 30, "current": 0.36,
    "threshold": 0.35, "comparator": ">",
}]


def test_pause_ts_is_written_on_a_new_trip(monkeypatch):
    out, keys = _enforce_with(monkeypatch, {"paused": False}, _BRIER_TRIP)

    assert out["paused"] is True
    assert "engine_pause_ts" in keys


def test_pause_ts_is_not_rewritten_while_the_same_pause_persists(monkeypatch):
    """The regression: a latched pause reported a forever-advancing 'since'."""
    started = int(time.time()) - 7432        # the live +2h04m drift observed
    prev = {
        "paused": True,
        "reason": "brier->degrade_watching",
        "since": started,
        "latched": True,
    }

    out, keys = _enforce_with(monkeypatch, prev, _BRIER_TRIP)

    assert out["paused"] is True
    assert "engine_pause_ts" not in keys, "pause start time was overwritten"
    assert out["since"] == started, "reported 'since' drifted off the real start"


def test_pause_ts_is_rewritten_when_the_reason_changes(monkeypatch):
    """A different trip reason is a genuinely new pause, so it restamps."""
    prev = {
        "paused": True,
        "reason": "expectancy->halt_manual_review",
        "since": int(time.time()) - 5000,
        "latched": True,
    }

    out, keys = _enforce_with(monkeypatch, prev, _BRIER_TRIP)

    assert "engine_pause_ts" in keys
    assert out["since"] > prev["since"]
