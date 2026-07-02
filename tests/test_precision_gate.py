"""Phase 3: precision-targeted firing gate — the 70% contract.

A model may only fire live picks above a probability threshold that
demonstrably produced >= V3_PRECISION_TARGET precision out-of-sample
(chosen on the calib slice, validated on the untouched gate slice).
No proof, no fire.
"""
import time

import numpy as np
import pytest

import core.signal_engine as _se
from core.precision_gate import (
    precision_gate_enabled,
    select_fire_threshold,
    threshold_search,
    wilson_lower_bound,
)


# ---------------------------------------------------------------- unit level

def test_wilson_lower_bound_sanity():
    assert wilson_lower_bound(0, 0) == 0.0
    # 7/10 wins: the floor must be meaningfully below 0.7 but above 0.35
    lb = wilson_lower_bound(7, 10)
    assert 0.35 < lb < 0.70
    # More samples at the same rate tighten the bound
    assert wilson_lower_bound(70, 100) > lb


def test_threshold_search_finds_lowest_valid_threshold():
    # probs above 0.6 win 3/4 = 75%; below win 1/4
    probs = [0.3, 0.4, 0.5, 0.55, 0.62, 0.7, 0.8, 0.9]
    labels = [0, 1, 0, 0, 0, 1, 1, 1]
    got = threshold_search(probs, labels, target=0.70, min_support=4)
    assert got is not None
    assert got["threshold"] == 0.62
    assert got["support"] == 4
    assert got["precision"] == 0.75


def test_threshold_search_no_operating_point():
    probs = [0.5, 0.6, 0.7, 0.8]
    labels = [0, 0, 0, 1]  # even the top slice never reaches 70%
    assert threshold_search(probs, labels, target=0.70, min_support=2) is None


def test_threshold_search_respects_min_support():
    # Only the top 2 picks hit the target — support 2 < min_support 3 -> None
    probs = [0.5, 0.6, 0.9, 0.95]
    labels = [0, 0, 1, 1]
    assert threshold_search(probs, labels, target=0.70, min_support=3) is None
    got = threshold_search(probs, labels, target=0.70, min_support=2)
    assert got is not None and got["threshold"] == 0.9


def test_threshold_search_tie_handling():
    # Duplicate probs: "prob >= t" always includes ALL duplicates of t
    probs = [0.6, 0.6, 0.6, 0.6]
    labels = [1, 1, 1, 0]
    got = threshold_search(probs, labels, target=0.70, min_support=2)
    assert got is not None
    assert got["threshold"] == 0.6
    assert got["support"] == 4  # never a partial slice of the tie group


def test_select_fire_threshold_ok_path(monkeypatch):
    monkeypatch.setenv("V3_PRECISION_MIN_SUPPORT", "4")
    monkeypatch.setenv("V3_PRECISION_GATE_MIN_SUPPORT", "3")
    calib_probs = [0.3, 0.4, 0.62, 0.7, 0.8, 0.9]
    calib_labels = [0, 0, 1, 1, 0, 1]  # >=0.62: 3/4 = 75%
    gate_probs = [0.5, 0.65, 0.7, 0.9]
    gate_labels = [0, 1, 1, 1]         # >=0.62: 3/3 = 100%
    out = select_fire_threshold(calib_probs, calib_labels, gate_probs, gate_labels)
    assert out["ok"] is True
    assert out["threshold"] == 0.62
    assert out["gate"]["precision"] == 1.0


def test_select_fire_threshold_fails_gate_validation(monkeypatch):
    monkeypatch.setenv("V3_PRECISION_MIN_SUPPORT", "4")
    monkeypatch.setenv("V3_PRECISION_GATE_MIN_SUPPORT", "3")
    monkeypatch.setenv("V3_PRECISION_GATE_SLACK", "0.05")
    calib_probs = [0.3, 0.4, 0.62, 0.7, 0.8, 0.9]
    calib_labels = [0, 0, 1, 1, 1, 1]  # calib looks great
    gate_probs = [0.65, 0.7, 0.8, 0.9]
    gate_labels = [0, 0, 0, 1]         # gate slice: 25% — model is overfit
    out = select_fire_threshold(calib_probs, calib_labels, gate_probs, gate_labels)
    assert out["ok"] is False
    assert "gate_precision" in (out.get("fail_reason") or "")


def test_select_fire_threshold_fails_gate_support(monkeypatch):
    monkeypatch.setenv("V3_PRECISION_MIN_SUPPORT", "4")
    monkeypatch.setenv("V3_PRECISION_GATE_MIN_SUPPORT", "5")
    calib_probs = [0.62, 0.7, 0.8, 0.9]
    calib_labels = [1, 1, 1, 1]
    gate_probs = [0.7, 0.9]
    gate_labels = [1, 1]  # only 2 gate picks — cannot validate
    out = select_fire_threshold(calib_probs, calib_labels, gate_probs, gate_labels)
    assert out["ok"] is False
    assert "gate_support" in (out.get("fail_reason") or "")


def test_precision_gate_enabled_flag(monkeypatch):
    monkeypatch.delenv("V3_PRECISION_GATE", raising=False)
    assert precision_gate_enabled() is True  # default ON
    monkeypatch.setenv("V3_PRECISION_GATE", "off")
    assert precision_gate_enabled() is False


# ---------------------------------------------------------- predict-time gate

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


def _patch(monkeypatch, up_p, precision_gate):
    meta = {"edge": 0.3, "accuracy": 0.66, "wf_acc_mean": 0.64,
            "wf_edge_mean": 0.2, "wf_fold_count": 4, "trained_at": time.time()}
    if precision_gate is not None:
        meta["precision_gate"] = precision_gate
    monkeypatch.setattr(
        _se, "load_model",
        lambda s, direction="UP": (_Model(up_p), _se.FEATURE_COLS, dict(meta))
        if direction == "UP" else (None, None, None))
    monkeypatch.setattr(_se, "_fetch_ohlcv",
                        lambda s, a, period="5d", interval="1h": _uptrend_rows())
    for k, v in {"V3_MIN_WIN_PROBA": "0.55", "V3_MIN_EDGE": "0.0",
                 "V3_MIN_HOLDOUT_ACC": "0.0", "V3_MIN_WF_ACC_MEAN": "0.0"}.items():
        monkeypatch.setenv(k, v)


def test_unproven_model_cannot_fire(monkeypatch):
    """No proven >=target operating point -> no live fire, even at prob 0.95."""
    monkeypatch.delenv("V3_PRECISION_GATE", raising=False)
    _patch(monkeypatch, up_p=0.95, precision_gate={"ok": False, "fail_reason": "x"})
    sig, reason = _se.predict_live_ex("WOLF", "stock")
    assert sig is None
    assert reason == "precision_unproven"


def test_legacy_model_without_precision_meta_cannot_fire(monkeypatch):
    """Pre-Phase-3 models carry no precision proof -> blocked until retrain."""
    monkeypatch.delenv("V3_PRECISION_GATE", raising=False)
    _patch(monkeypatch, up_p=0.95, precision_gate=None)
    sig, reason = _se.predict_live_ex("WOLF", "stock")
    assert sig is None
    assert reason == "precision_unproven"


def test_proven_threshold_raises_the_firing_bar(monkeypatch):
    """prob 0.60 clears V3_MIN_WIN_PROBA 0.55 but NOT the proven 0.68 threshold."""
    monkeypatch.delenv("V3_PRECISION_GATE", raising=False)
    _patch(monkeypatch, up_p=0.60,
           precision_gate={"ok": True, "threshold": 0.68, "target": 0.70})
    sig, reason = _se.predict_live_ex("WOLF", "stock")
    assert sig is None
    assert reason == "prob_low"


def test_proven_model_fires_above_threshold(monkeypatch):
    monkeypatch.delenv("V3_PRECISION_GATE", raising=False)
    _patch(monkeypatch, up_p=0.72,
           precision_gate={"ok": True, "threshold": 0.68, "target": 0.70})
    scores = {}
    sig, reason = _se.predict_live_ex("WOLF", "stock", scores=scores)
    assert sig is not None and sig[0] == "UP"
    assert scores["precision_gate_up"]["ok"] is True
    assert scores["precision_gate_up"]["threshold"] == 0.68


def test_research_mode_bypasses_precision_gate(monkeypatch):
    """Research picks are the exploration lane — excluded from accuracy stats."""
    monkeypatch.delenv("V3_PRECISION_GATE", raising=False)
    _patch(monkeypatch, up_p=0.60, precision_gate={"ok": False})
    sig, reason = _se.predict_live_ex("WOLF", "stock", research_mode=True)
    assert sig is not None and sig[0] == "UP"


def test_env_off_switch_restores_legacy_behavior(monkeypatch):
    monkeypatch.setenv("V3_PRECISION_GATE", "off")
    _patch(monkeypatch, up_p=0.95, precision_gate=None)
    sig, reason = _se.predict_live_ex("WOLF", "stock")
    assert sig is not None and sig[0] == "UP"


# ------------------------------------------------------- pooled global gate

def test_select_global_threshold_needs_wilson_bound(monkeypatch):
    from core.precision_gate import select_global_threshold
    monkeypatch.setenv("V3_PRECISION_GLOBAL_MIN_SUPPORT", "20")
    monkeypatch.setenv("V3_PRECISION_GLOBAL_WILSON_SLACK", "0.05")
    # 30 picks at 73% precision above 0.6: wilson_low ~= 0.55 -> NOT enough
    probs = [0.65] * 30 + [0.4] * 30
    labels = [1] * 22 + [0] * 8 + [0] * 30
    out = select_global_threshold(probs, labels, target=0.70)
    assert out["ok"] is False
    assert "wilson" in (out.get("fail_reason") or "")
    # 300 picks at 76%: wilson_low ~= 0.71 -> proven
    probs = [0.65] * 300 + [0.4] * 100
    labels = [1] * 228 + [0] * 72 + [0] * 100
    out = select_global_threshold(probs, labels, target=0.70)
    assert out["ok"] is True
    assert out["threshold"] == 0.65


def test_unproven_symbol_falls_back_to_global_pool(monkeypatch):
    """Symbol gate unproven + globally proven pool -> fires above pooled threshold."""
    import core.precision_gate as _pg
    monkeypatch.delenv("V3_PRECISION_GATE", raising=False)
    _patch(monkeypatch, up_p=0.72,
           precision_gate={"ok": False, "fail_reason": "no_calib_operating_point"})
    monkeypatch.setattr(
        _pg, "load_global_threshold",
        lambda d: {"ok": True, "threshold": 0.66, "target": 0.70})
    # predict imports from core.precision_gate inside the lane — patch there
    scores = {}
    sig, reason = _se.predict_live_ex("WOLF", "stock", scores=scores)
    assert sig is not None and sig[0] == "UP"
    assert scores["precision_gate_up"]["source"] == "global_pool"
    assert scores["precision_gate_up"]["threshold"] == 0.66


def test_global_pool_threshold_still_blocks_below(monkeypatch):
    import core.precision_gate as _pg
    monkeypatch.delenv("V3_PRECISION_GATE", raising=False)
    _patch(monkeypatch, up_p=0.60, precision_gate={"ok": False})
    monkeypatch.setattr(
        _pg, "load_global_threshold",
        lambda d: {"ok": True, "threshold": 0.66, "target": 0.70})
    sig, reason = _se.predict_live_ex("WOLF", "stock")
    assert sig is None
    assert reason == "prob_low"


def test_no_global_pool_keeps_symbol_blocked(monkeypatch):
    import core.precision_gate as _pg
    monkeypatch.delenv("V3_PRECISION_GATE", raising=False)
    _patch(monkeypatch, up_p=0.95, precision_gate={"ok": False})
    monkeypatch.setattr(_pg, "load_global_threshold", lambda d: None)
    sig, reason = _se.predict_live_ex("WOLF", "stock")
    assert sig is None
    assert reason == "precision_unproven"
