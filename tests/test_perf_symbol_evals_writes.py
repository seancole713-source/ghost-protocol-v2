"""ghost_perf_symbol_evals write volume: each row is written once per cycle, and the
cycle path never runs the full-table backfill UPDATE or repeats the schema DDL."""
from __future__ import annotations

import core.performance_log as perf


def _cycle(cur, evals):
    return perf.log_prediction_cycle(
        cur, cycle_ts=1_790_000_000, duration_ms=1, scanned=len(evals), candidates=0, saved=0,
        dedup_blocked=0, would_fire=False, binding_skip=None, paused=False, pause_reason=None,
        suppressed=0, suppress_reason=None, skip_counts={}, near_miss=None, regime={},
        circuit_breaker={}, objective_mode={}, risk_block=None, saved_prediction_ids=[],
        symbol_evals=evals)


class _Cur:
    def __init__(self):
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))

    def fetchone(self):
        return (7,)


def test_a_silenced_row_carries_confidence_final_at_insert():
    # The engine sets confidence_final=None on non-fired rows; the backfill would later
    # rewrite the row with confidence. Write that value up front instead.
    ev = perf.symbol_eval_from_scan("WOLF", None, "below_floor",
                                    {"confidence": 0.53, "confidence_final": None}, 1)
    assert ev["confidence_final"] == 0.53 and ev["confidence"] == 0.53
    explicit = perf.symbol_eval_from_scan("WOLF", None, "x", {"confidence": 0.53, "confidence_final": 0.5}, 1)
    assert explicit["confidence_final"] == 0.5
    fired = perf.symbol_eval_from_scan("WOLF", {"confidence": 0.61}, None, {"confidence": 0.53}, 1)
    assert fired["confidence_final"] == 0.61


def test_the_cycle_never_runs_the_backfill_and_runs_ddl_once(monkeypatch):
    monkeypatch.setenv("GHOST_PERF_LOG", "on")
    monkeypatch.setattr(perf, "_PERF_TABLES_READY", False)
    monkeypatch.setattr(perf, "maybe_prune", lambda _c: None)
    ev = [perf.symbol_eval_from_scan("WOLF", None, "below_floor", {"confidence": 0.5, "confidence_final": None}, 1)]
    first, second = _Cur(), _Cur()
    _cycle(first, ev)
    _cycle(second, ev)
    for cur in (first, second):
        assert not any(s.startswith("UPDATE ghost_perf_symbol_evals") for s in cur.sql)
        assert sum(s.startswith("INSERT INTO ghost_perf_symbol_evals") for s in cur.sql) == 1
    assert any(s.startswith("ALTER TABLE ghost_perf_symbol_evals") for s in first.sql)
    assert not any(s.startswith(("ALTER TABLE", "CREATE TABLE", "CREATE INDEX")) for s in second.sql)


def test_boot_ensure_still_backfills_historical_rows():
    cur = _Cur()
    perf.ensure_perf_tables(cur)
    assert any(s.startswith("UPDATE ghost_perf_symbol_evals SET confidence_final=confidence") for s in cur.sql)
