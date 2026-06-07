"""Tests for backend performance log (core/performance_log.py + wolf API)."""
import time

import core.performance_log as perf
import wolf_app


def test_symbol_eval_from_scan_fired_pick():
    pick = {
        "direction": "UP",
        "confidence": 0.62,
        "entry_price": 10.0,
        "target_price": 10.6,
        "stop_price": 9.7,
    }
    scores = {
        "up_prob": 0.58,
        "model_meta": {"min_win_proba": 0.55},
        "regime": {"label": "Trend-up"},
    }
    ev = perf.symbol_eval_from_scan("WOLF", pick, None, scores, 1700000000)
    assert ev["symbol"] == "WOLF"
    assert ev["fired"] is True
    assert ev["skip_code"] is None
    assert ev["direction"] == "UP"
    assert ev["up_prob"] == 0.58
    assert ev["regime_label"] == "Trend-up"


def test_symbol_eval_from_scan_skip():
    scores = {
        "up_prob": 0.42,
        "confidence": 0.51,
        "confidence_floor": 0.55,
        "model_meta": {"min_win_proba": 0.55},
    }
    ev = perf.symbol_eval_from_scan("SPCE", None, "below_confidence_floor", scores, 1700000001)
    assert ev["fired"] is False
    assert ev["skip_code"] == "below_confidence_floor"
    assert ev["confidence"] == 0.51


def test_log_prediction_cycle_inserts_rows(monkeypatch):
    monkeypatch.setenv("GHOST_PERF_LOG", "on")
    executed = []

    class _Cur:
        def execute(self, sql, params=None):
            executed.append((sql.strip()[:80], params))

        def fetchone(self):
            return (99,)

    cur = _Cur()
    cycle_id = perf.log_prediction_cycle(
        cur,
        cycle_ts=int(time.time()),
        duration_ms=120,
        scanned=2,
        candidates=1,
        saved=1,
        dedup_blocked=0,
        would_fire=True,
        binding_skip=None,
        paused=False,
        pause_reason=None,
        suppressed=0,
        suppress_reason=None,
        skip_counts={},
        near_miss=None,
        regime={"reason": "OK"},
        circuit_breaker={"active": False},
        objective_mode={"mode": "normal"},
        risk_block=None,
        saved_prediction_ids=[1234],
        symbol_evals=[
            perf.symbol_eval_from_scan(
                "WOLF",
                {"direction": "UP", "confidence": 0.6, "entry_price": 1, "target_price": 2, "stop_price": 0.9},
                None,
                {"up_prob": 0.6},
                int(time.time()),
            )
        ],
    )
    assert cycle_id == 99
    sqls = " ".join(s[0] for s in executed)
    assert "ghost_perf_cycles" in sqls
    assert "ghost_perf_symbol_evals" in sqls
    assert "ghost_perf_events" in sqls


def test_perf_log_disabled_skips_write(monkeypatch):
    monkeypatch.setenv("GHOST_PERF_LOG", "off")

    class _Cur:
        def execute(self, *a, **k):
            raise AssertionError("should not write when disabled")

    assert perf.log_prediction_cycle(
        _Cur(),
        cycle_ts=1,
        duration_ms=1,
        scanned=0,
        candidates=0,
        saved=0,
        dedup_blocked=0,
        would_fire=False,
        binding_skip=None,
        paused=False,
        pause_reason=None,
        suppressed=0,
        suppress_reason=None,
        skip_counts={},
        near_miss=None,
        regime={},
        circuit_breaker={},
        objective_mode={},
        risk_block=None,
        saved_prediction_ids=[],
        symbol_evals=[],
    ) is None


def test_wolf_perf_log_cycles_endpoint(monkeypatch):
    monkeypatch.setattr(
        perf,
        "fetch_cycles",
        lambda **kw: {"total": 1, "limit": 50, "offset": 0, "cycles": [{"id": 1, "saved": 0}]},
    )
    out = wolf_app.wolf_perf_log_cycles()
    assert out["ok"] is True
    assert out["total"] == 1
    assert out["cycles"][0]["id"] == 1


def test_wolf_perf_log_progress_endpoint(monkeypatch):
    monkeypatch.setattr(
        perf,
        "fetch_progress_summary",
        lambda days=7: {"days": days, "open_count": 2, "cycles": {"count": 10}},
    )
    out = wolf_app.wolf_perf_log_progress(days=14)
    assert out["ok"] is True
    assert out["days"] == 14
    assert out["open_count"] == 2


def test_wolf_perf_log_cycle_detail_not_found(monkeypatch):
    monkeypatch.setattr(perf, "fetch_cycle_detail", lambda cid, **kw: None)
    resp = wolf_app.wolf_perf_log_cycle_detail(999)
    assert resp.status_code == 404
