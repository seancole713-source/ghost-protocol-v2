"""PR #162: live per-bin probability recalibration — the scoreboard feedback loop."""
import time

import numpy as np

import core.live_recalibration as lr
import core.signal_engine as _se


# ------------------------------------------------------------- pure math

def test_bin_for_matches_watcher_edges():
    assert lr.bin_for(0.30) == (0.0, 0.5)
    assert lr.bin_for(0.52) == (0.5, 0.55)
    assert lr.bin_for(0.57) == (0.55, 0.6)
    assert lr.bin_for(0.65) == (0.6, 0.7)
    assert lr.bin_for(0.70) == (0.7, 1.01)
    assert lr.bin_for(0.99) == (0.7, 1.01)
    assert lr.bin_for(1.0) == (0.7, 1.01)


def test_recalibrate_identity_with_no_evidence(monkeypatch):
    monkeypatch.setenv("V3_LIVE_RECAL_MIN_BIN_N", "0")
    out = lr.recalibrate(0.79, samples=0, wins=0, k=25)
    assert out["applied"] is True
    assert out["prob_adjusted"] == 0.79  # n=0 -> pure prior, untouched


def test_recalibrate_pulls_inverted_bin_down(monkeypatch):
    # The live finding: 70+ bin, 27 resolved, 13 wins (48.1%) vs 0.79 predicted.
    monkeypatch.delenv("V3_LIVE_RECAL_MIN_BIN_N", raising=False)
    out = lr.recalibrate(0.79, samples=27, wins=13, k=25)
    assert out["applied"] is True
    # adj = (13 + 25*0.79) / (27 + 25) = 32.75/52 ~ 0.6298
    assert abs(out["prob_adjusted"] - 0.6298) < 0.001
    assert out["shift"] < -0.15  # pulled hard toward the scoreboard


def test_recalibrate_barely_moves_calibrated_bin():
    # 60-70 bin: 40 resolved, 26 wins (65%) vs 0.64 predicted — healthy.
    out = lr.recalibrate(0.64, samples=40, wins=26, k=25)
    assert out["applied"] is True
    assert abs(out["prob_adjusted"] - out["prob_raw"]) < 0.01


def test_recalibrate_converges_to_realized_rate():
    # Massive evidence: scoreboard outvotes the model completely.
    out = lr.recalibrate(0.90, samples=10_000, wins=5_000, k=25)
    assert abs(out["prob_adjusted"] - 0.50) < 0.01


def test_recalibrate_respects_min_bin_samples(monkeypatch):
    monkeypatch.setenv("V3_LIVE_RECAL_MIN_BIN_N", "5")
    out = lr.recalibrate(0.79, samples=3, wins=0, k=25)
    assert out["applied"] is False
    assert out["prob_adjusted"] == out["prob_raw"]
    assert out["note"] == "insufficient_bin_evidence"


def test_recalibrate_monotone_in_raw_prob():
    # Two picks in the same bin: the stronger raw prob stays stronger.
    a = lr.recalibrate(0.72, samples=27, wins=13, k=25)["prob_adjusted"]
    b = lr.recalibrate(0.95, samples=27, wins=13, k=25)["prob_adjusted"]
    assert b > a


# ------------------------------------------------------ orchestrator scope

def test_down_lane_passthrough():
    out = lr.live_recalibrated_prob(0.80, direction="DOWN")
    assert out["applied"] is False
    assert out["prob_adjusted"] == 0.8
    assert out["note"] == "down_lane_unsupported"


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("V3_LIVE_RECALIBRATION", "off")
    out = lr.live_recalibrated_prob(0.80)
    assert out["applied"] is False and out.get("disabled") is True


def test_db_failure_is_failsafe(monkeypatch):
    monkeypatch.delenv("V3_LIVE_RECALIBRATION", raising=False)
    def _boom(lo, hi):
        raise RuntimeError("db down")
    monkeypatch.setattr(lr, "live_bin_stats", _boom)
    out = lr.live_recalibrated_prob(0.80)
    assert out["applied"] is False
    assert out["prob_adjusted"] == 0.8
    assert out["note"] == "stats_unavailable"


def test_live_bin_stats_counts_expired_as_resolved_non_win(monkeypatch):
    """The live recalibration scoreboard must use the same denominator as
    contract_70: WIN wins; LOSS and EXPIRED resolved non-wins."""
    captured = {}

    class _Cur:
        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params
        def fetchone(self):
            return (3, 1)  # WIN + LOSS + EXPIRED -> 3 resolved, 1 win

    class _Conn:
        def cursor(self): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    import core.db as db
    monkeypatch.setattr(db, "db_conn", lambda: _Conn())

    samples, wins = lr.live_bin_stats(0.7, 1.01)
    assert (samples, wins) == (3, 1)
    assert "'EXPIRED'" in captured["sql"]
    assert captured["params"] == (0.7, 1.01)


# ------------------------------------------- predict-time integration (UP lane)

def _uptrend_rows(n=220):
    rows = []
    for i in range(n):
        px = 100.0 + i * 0.4
        rows.append({"ts": "2026-05-20T%02d:00:00Z" % (i % 24),
                     "open": px - 0.2, "high": px + 0.5, "low": px - 0.5,
                     "close": px, "volume": 1000 + i * 5})
    return rows


class _Model:
    def __init__(self, p_win):
        self._p = p_win

    def predict_proba(self, X):
        return np.array([[1.0 - self._p, self._p]])


def _patch(monkeypatch, up_p):
    meta = {"edge": 0.3, "accuracy": 0.66, "wf_acc_mean": 0.64,
            "wf_edge_mean": 0.2, "wf_fold_count": 4, "trained_at": time.time(),
            "precision_gate": {"ok": True, "threshold": 0.60, "target": 0.70}}
    monkeypatch.setattr(
        _se, "load_model",
        lambda s, direction="UP": (_Model(up_p), _se.FEATURE_COLS, dict(meta))
        if direction == "UP" else (None, None, None))
    monkeypatch.setattr(_se, "_fetch_ohlcv",
                        lambda s, a, period="5d", interval="1h": _uptrend_rows())
    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "legacy")
    monkeypatch.setenv("GHOST_PREMARKET_SCAN", "0")
    for k, v in {"V3_MIN_WIN_PROBA": "0.55", "V3_MIN_EDGE": "0.0",
                 "V3_MIN_HOLDOUT_ACC": "0.0", "V3_MIN_WF_ACC_MEAN": "0.0"}.items():
        monkeypatch.setenv(k, v)
    import core.proven_skill_gate as _skill
    monkeypatch.setattr(_skill, "symbol_review",
                        lambda sym: {"ok": True, "symbol": sym, "test": True})
    monkeypatch.setattr(_skill, "global_calibration_review",
                        lambda prob: {"ok": True, "prob": prob, "test": True})


def test_inverted_bin_shrinks_prob_below_bar_and_blocks(monkeypatch):
    """A 0.79 pick in a badly inverted bin shrinks under the 0.60 proven bar
    and is refused as live_recal_prob_low — per-bin, evidence-weighted."""
    _patch(monkeypatch, up_p=0.79)
    monkeypatch.setattr(lr, "live_bin_stats", lambda lo, hi: (50, 10))  # 20% real
    sig, reason = _se.predict_live_ex("WOLF", "stock")
    assert sig is None
    assert reason == "live_recal_prob_low"


def test_healthy_bin_fires_with_adjusted_confidence(monkeypatch):
    """A healthy bin barely moves the prob; the pick still fires and the
    scores expose the recalibration working."""
    _patch(monkeypatch, up_p=0.79)
    monkeypatch.setattr(lr, "live_bin_stats", lambda lo, hi: (50, 40))  # 80% real
    scores = {}
    sig, reason = _se.predict_live_ex("WOLF", "stock", scores=scores)
    assert sig is not None and sig[0] == "UP"
    recal = scores["live_recalibration_up"]
    assert recal["applied"] is True
    assert abs(recal["prob_adjusted"] - recal["prob_raw"]) < 0.02


def test_research_mode_keeps_raw_probs(monkeypatch):
    """The evidence stream must stay unadjusted: research probes never touch
    the recalibration layer (no live_recalibration score entry)."""
    _patch(monkeypatch, up_p=0.79)
    def _boom(lo, hi):
        raise AssertionError("research mode must not read live bin stats")
    monkeypatch.setattr(lr, "live_bin_stats", _boom)
    scores = {}
    sig, reason = _se.predict_live_ex("WOLF", "stock", research_mode=True,
                                      scores=scores)
    assert sig is not None and sig[0] == "UP"
    assert "live_recalibration_up" not in scores
