"""Phase 3: precision-targeted firing gate — the 70% contract.

A model may only fire live picks above a probability threshold that
demonstrably produced >= V3_PRECISION_TARGET precision out-of-sample
(chosen on the calib slice, validated on the untouched gate slice).
No proof, no fire.
"""
import time

import numpy as np

import core.signal_engine as _se
from core.precision_gate import (
    precision_gate_enabled,
    select_fire_threshold,
    threshold_search,
    validate_fire_proof,
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
    gate_probs = [0.7] * 20
    gate_labels = [1] * 20              # Wilson lower bound clears 70%
    out = select_fire_threshold(calib_probs, calib_labels, gate_probs, gate_labels)
    assert out["ok"] is True
    assert out["threshold"] == 0.62
    assert out["gate"]["precision"] == 1.0
    assert out["gate"]["wilson_low"] >= 0.70


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
    assert "gate_wilson" in (out.get("fail_reason") or "")


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


def test_select_fire_threshold_uses_raw_wilson_not_display_rounding(monkeypatch):
    monkeypatch.setenv("V3_PRECISION_MIN_SUPPORT", "20")
    monkeypatch.setenv("V3_PRECISION_GATE_MIN_SUPPORT", "50")
    # 42/50 has Wilson lower ~= 0.714, which displays 0.71. Setting the target
    # one raw epsilon above that bound must fail even if display rounding ties.
    raw = wilson_lower_bound(42, 50)
    target = raw + 0.00001
    out = select_fire_threshold(
        [0.7] * 50, [1] * 50,
        [0.7] * 50, [1] * 42 + [0] * 8,
        target=target,
    )
    assert out["gate"]["wilson_low"] == round(raw, 4)
    assert out["ok"] is False
    assert "gate_wilson" in out["fail_reason"]


def test_precision_gate_enabled_flag(monkeypatch):
    monkeypatch.delenv("V3_PRECISION_GATE", raising=False)
    assert precision_gate_enabled() is True  # default ON
    monkeypatch.setenv("V3_PRECISION_GATE", "off")
    assert precision_gate_enabled() is False


def test_symbol_proof_rejects_non_integer_counts(monkeypatch):
    monkeypatch.setenv("V3_PRECISION_MIN_SUPPORT", "10")
    monkeypatch.setenv("V3_PRECISION_GATE_MIN_SUPPORT", "5")
    base = {
        "ok": True, "threshold": 0.68, "target": 0.70,
        "calib": {"support": 20, "wins": 20},
        "gate": {"support": 20, "wins": 20},
    }
    assert validate_fire_proof(base) is True
    for bad in (20.9, True, "20.9", float("nan"), float("inf"), None):
        proof = {
            **base,
            "calib": {"support": bad, "wins": 20},
        }
        assert validate_fire_proof(proof) is False


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


def _complete_proof(proof):
    if not proof or proof.get("ok") is not True:
        return proof
    return {
        **proof,
        "calib": {"support": 20, "wins": 20},
        "gate": {"support": 20, "wins": 20},
    }


def _patch(monkeypatch, up_p, precision_gate):
    meta = {"edge": 0.3, "accuracy": 0.66, "wf_acc_mean": 0.64,
            "wf_edge_mean": 0.2, "wf_fold_count": 4, "trained_at": time.time(),
            "model_sha256": "a" * 64, "label_schema": _se._v3_label_schema(),
            "validation_schema": _se._v3_validation_schema(),
            "label_hold_bars": _se.V3_LABEL_HOLD_BARS}
    if precision_gate is not None:
        meta["precision_gate"] = _complete_proof(precision_gate)
    monkeypatch.setattr(
        _se, "load_model",
        lambda s, direction="UP": (_Model(up_p), _se.FEATURE_COLS, dict(meta))
        if direction == "UP" else (None, None, None))
    monkeypatch.setattr(_se, "_fetch_ohlcv",
                        lambda s, a, period="5d", interval="1h": _uptrend_rows())
    # Legacy contract so these tests isolate precision-gate behavior from the
    # 70% contract floor clamps on training meta gates.
    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "legacy")
    # Hermetic: kill the live premarket overlay — during 4:00-9:30 AM CT it
    # stomps the synthetic fixture's last bar with the REAL symbol price and
    # flips the regime gate (time-of-day flake).
    monkeypatch.setenv("GHOST_PREMARKET_SCAN", "0")
    for k, v in {"V3_MIN_WIN_PROBA": "0.55", "V3_MIN_EDGE": "0.0",
                 "V3_MIN_HOLDOUT_ACC": "0.0", "V3_MIN_WF_ACC_MEAN": "0.0"}.items():
        monkeypatch.setenv(k, v)
    # Most precision-gate tests isolate precision behavior; explicit PR #155
    # tests below exercise the proven-skill blocker.
    import core.proven_skill_gate as _skill
    monkeypatch.setattr(
        _skill, "symbol_review",
        lambda sym, **identity: {"ok": True, "symbol": sym, "identity": identity, "test": True},
    )
    monkeypatch.setattr(
        _skill, "global_calibration_review",
        lambda prob, **identity: {"ok": True, "prob": prob, "identity": identity, "test": True},
    )


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


def test_research_mode_bypasses_precision_gate_under_legacy(monkeypatch):
    """Legacy contract only — 70% contract blocks unproven research picks."""
    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "legacy")
    monkeypatch.delenv("V3_PRECISION_GATE", raising=False)
    _patch(monkeypatch, up_p=0.60, precision_gate={"ok": False})
    sig, reason = _se.predict_live_ex("WOLF", "stock", research_mode=True)
    assert sig is not None and sig[0] == "UP"


def test_research_mode_blocked_when_contract_70(monkeypatch):
    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "70")
    monkeypatch.delenv("V3_PRECISION_GATE", raising=False)
    meta = {"edge": 0.3, "accuracy": 0.66, "wf_acc_mean": 0.70,
            "wf_edge_mean": 0.2, "wf_fold_count": 4, "trained_at": time.time(),
            "precision_gate": {"ok": False}}
    monkeypatch.setattr(
        _se, "load_model",
        lambda s, direction="UP": (_Model(0.60), _se.FEATURE_COLS, dict(meta))
        if direction == "UP" else (None, None, None))
    monkeypatch.setattr(_se, "_fetch_ohlcv",
                        lambda s, a, period="5d", interval="1h": _uptrend_rows())
    sig, reason = _se.predict_live_ex("WOLF", "stock", research_mode=True)
    assert sig is None
    assert reason == "precision_unproven"


def test_env_off_switch_restores_legacy_behavior(monkeypatch):
    monkeypatch.setenv("V3_PRECISION_GATE", "off")
    _patch(monkeypatch, up_p=0.95, precision_gate=None)
    sig, reason = _se.predict_live_ex("WOLF", "stock")
    assert sig is not None and sig[0] == "UP"


# ------------------------------------------------------- pooled global gate

def test_select_global_threshold_uses_independent_chronological_halves(monkeypatch):
    from core.precision_gate import select_global_threshold
    monkeypatch.setenv("V3_PRECISION_GLOBAL_MIN_SUPPORT", "20")
    # Selection half finds 0.65; weak validation half must reject it.
    probs = [0.65] * 30 + [0.4] * 30 + [0.65] * 30 + [0.4] * 30
    labels = ([1] * 24 + [0] * 6 + [0] * 30
              + [1] * 21 + [0] * 9 + [0] * 30)
    timestamps = list(range(1, len(probs) + 1))
    out = select_global_threshold(
        probs, labels, target=0.70, timestamps=timestamps,
    )
    assert out["ok"] is False
    assert "pooled_validation_wilson" in (out.get("fail_reason") or "")

    # Both chronological halves independently support the threshold, and the
    # untouched validation half's Wilson floor clears 70%.
    probs = ([0.65] * 300 + [0.4] * 100) * 2
    labels = ([1] * 240 + [0] * 60 + [0] * 100) * 2
    timestamps = list(range(1, len(probs) + 1))
    out = select_global_threshold(
        probs, labels, target=0.70, timestamps=timestamps,
    )
    assert out["ok"] is True
    assert out["threshold"] == 0.65
    assert out["validation"]["wilson_low"] >= 0.70


def test_global_threshold_requires_timestamps(monkeypatch):
    from core.precision_gate import select_global_threshold
    monkeypatch.setenv("V3_PRECISION_GLOBAL_MIN_SUPPORT", "2")
    out = select_global_threshold([0.8] * 4, [1] * 4, target=0.70)
    assert out["ok"] is False
    assert out["fail_reason"] == "pooled_timestamps_required"


def test_global_threshold_rejects_malformed_records(monkeypatch):
    from core.precision_gate import select_global_threshold
    monkeypatch.setenv("V3_PRECISION_GLOBAL_MIN_SUPPORT", "2")
    mismatch = select_global_threshold(
        [0.8] * 4, [1] * 3, target=0.70, timestamps=[1, 2, 3, 4],
    )
    assert mismatch["fail_reason"] == "pooled_record_lengths_mismatch"
    for bad in (float("nan"), float("inf"), -float("inf")):
        out = select_global_threshold(
            [0.8, 0.8, bad, 0.8], [1] * 4, target=0.70,
            timestamps=[1, 2, 3, 4],
        )
        assert out["ok"] is False
        assert out["fail_reason"] == "pooled_timestamps_invalid"


def _current_global_proof_blob(model_hash="sha-current"):
    import core.precision_gate as pg
    from core.engine_config import V3_LABEL_HOLD_BARS, _v3_feature_schema
    entry = {
        "ok": True, "threshold": 0.68,
        "target": pg.precision_target(), "support": 100, "wins": 90,
        "wilson_low": pg.wilson_lower_bound(90, 100),
        "proof_schema": pg._GLOBAL_PROOF_SCHEMA,
        "model_sha256s": [model_hash],
    }
    return {
        "proof_schema": pg._GLOBAL_PROOF_SCHEMA,
        "target": pg.precision_target(),
        "embargo_seconds": pg._required_global_embargo_seconds(),
        "validation_schema": _se._v3_validation_schema(),
        "label_schema": _se._v3_label_schema(),
        "feature_schema": _v3_feature_schema(),
        "label_hold_bars": V3_LABEL_HOLD_BARS,
        "UP": entry,
    }


def test_global_proof_requires_current_model_and_semantic_schemas(monkeypatch):
    import core.precision_gate as pg
    blob = _current_global_proof_blob()
    monkeypatch.setitem(pg._GLOBAL_CACHE, "val", blob)
    monkeypatch.setitem(pg._GLOBAL_CACHE, "ts", time.time())
    assert pg.load_global_threshold("UP", model_sha256="sha-current")["threshold"] == 0.68
    assert pg.load_global_threshold("UP", model_sha256="sha-other") is None
    assert pg.load_global_threshold("UP") is None

    for field in ("validation_schema", "label_schema", "feature_schema"):
        stale = _current_global_proof_blob()
        stale[field] = "stale"
        monkeypatch.setitem(pg._GLOBAL_CACHE, "val", stale)
        monkeypatch.setitem(pg._GLOBAL_CACHE, "ts", time.time())
        assert pg.load_global_threshold("UP", model_sha256="sha-current") is None


def test_global_proof_rejects_stale_horizon_and_embargo(monkeypatch):
    import core.precision_gate as pg
    for mutate in (
        lambda blob: blob.update(label_hold_bars=blob["label_hold_bars"] + 1),
        lambda blob: blob.update(embargo_seconds=blob["embargo_seconds"] - 1),
    ):
        blob = _current_global_proof_blob()
        mutate(blob)
        monkeypatch.setitem(pg._GLOBAL_CACHE, "val", blob)
        monkeypatch.setitem(pg._GLOBAL_CACHE, "ts", time.time())
        assert pg.load_global_threshold("UP", model_sha256="sha-current") is None


def test_global_proof_recomputes_wilson_and_current_support(monkeypatch):
    import core.precision_gate as pg
    monkeypatch.setenv("V3_PRECISION_GLOBAL_MIN_SUPPORT", "30")
    blob = _current_global_proof_blob()
    # Rounded telemetry claims success, but 21/30 has a raw Wilson floor well
    # below 70%; exact persisted counts must be the authority.
    blob["UP"].update(support=30, wins=21, wilson_low=0.99)
    monkeypatch.setitem(pg._GLOBAL_CACHE, "val", blob)
    monkeypatch.setitem(pg._GLOBAL_CACHE, "ts", time.time())
    assert pg.load_global_threshold("UP", model_sha256="sha-current") is None

    blob = _current_global_proof_blob()
    blob["UP"].update(support=29, wins=29, wilson_low=1.0)
    monkeypatch.setitem(pg._GLOBAL_CACHE, "val", blob)
    monkeypatch.setitem(pg._GLOBAL_CACHE, "ts", time.time())
    assert pg.load_global_threshold("UP", model_sha256="sha-current") is None


def test_global_proof_rejects_fractional_counts_and_target_mismatch(monkeypatch):
    import core.precision_gate as pg
    monkeypatch.setenv("V3_PRECISION_GLOBAL_MIN_SUPPORT", "30")
    for field, bad in (("support", 100.5), ("wins", "90.2")):
        blob = _current_global_proof_blob()
        blob["UP"][field] = bad
        monkeypatch.setitem(pg._GLOBAL_CACHE, "val", blob)
        monkeypatch.setitem(pg._GLOBAL_CACHE, "ts", time.time())
        assert pg.load_global_threshold("UP", model_sha256="sha-current") is None

    blob = _current_global_proof_blob()
    blob["UP"]["target"] = blob["target"] + 0.01
    monkeypatch.setitem(pg._GLOBAL_CACHE, "val", blob)
    monkeypatch.setitem(pg._GLOBAL_CACHE, "ts", time.time())
    assert pg.load_global_threshold("UP", model_sha256="sha-current") is None


def test_threshold_search_preserves_exact_probability_boundary():
    exact = 0.678912345
    out = threshold_search([exact] * 20, [1] * 20, 0.70, 5)
    assert out is not None
    assert out["threshold"] == exact
    stats = select_fire_threshold(
        [exact] * 20, [1] * 20, [exact] * 20, [1] * 20,
        target=0.70,
    )
    assert stats["ok"] is True
    assert stats["gate"]["support"] == 20


def test_global_threshold_does_not_split_timestamp_ties(monkeypatch):
    from core.precision_gate import select_global_threshold
    monkeypatch.setenv("V3_PRECISION_GLOBAL_MIN_SUPPORT", "2")
    out = select_global_threshold(
        [0.8] * 6, [1] * 6, target=0.70,
        timestamps=[100, 100, 100, 200, 200, 200],
    )
    assert out["ok"] is False
    # The three rows at 100 and three at 200 remain whole groups. The failure
    # is statistical Wilson evidence, never a partial same-time split.
    assert out["selection"]["support"] == 3
    assert out["validation"]["support"] == 3


def test_global_threshold_applies_embargo(monkeypatch):
    from core.precision_gate import select_global_threshold
    monkeypatch.setenv("V3_PRECISION_GLOBAL_MIN_SUPPORT", "2")
    out = select_global_threshold(
        [0.8] * 6, [1] * 6, target=0.70,
        timestamps=[100, 101, 102, 103, 104, 105], embargo_seconds=3,
    )
    assert out["ok"] is False
    assert out["fail_reason"].startswith("pooled_split_support")


def test_unproven_symbol_falls_back_to_global_pool(monkeypatch):
    """Symbol gate unproven + globally proven pool -> fires above pooled threshold."""
    import core.precision_gate as _pg
    monkeypatch.delenv("V3_PRECISION_GATE", raising=False)
    _patch(monkeypatch, up_p=0.72,
           precision_gate={"ok": False, "fail_reason": "no_calib_operating_point"})
    monkeypatch.setattr(
        _pg, "load_global_threshold",
        lambda d, **kw: {"ok": True, "threshold": 0.66, "target": 0.70})
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
        lambda d, **kw: {"ok": True, "threshold": 0.66, "target": 0.70})
    sig, reason = _se.predict_live_ex("WOLF", "stock")
    assert sig is None
    assert reason == "prob_low"


def test_live_serving_includes_exact_certified_threshold(monkeypatch):
    monkeypatch.delenv("V3_PRECISION_GATE", raising=False)
    _patch(
        monkeypatch, up_p=0.68,
        precision_gate={"ok": True, "threshold": 0.68, "target": 0.70},
    )
    sig, reason = _se.predict_live_ex("WOLF", "stock")
    assert reason is None
    assert sig is not None and sig[0] == "UP"


def test_no_global_pool_keeps_symbol_blocked(monkeypatch):
    import core.precision_gate as _pg
    monkeypatch.delenv("V3_PRECISION_GATE", raising=False)
    _patch(monkeypatch, up_p=0.95, precision_gate={"ok": False})
    monkeypatch.setattr(_pg, "load_global_threshold", lambda d, **kw: None)
    sig, reason = _se.predict_live_ex("WOLF", "stock")
    assert sig is None
    assert reason == "precision_unproven"


def test_proven_skill_gate_blocks_otherwise_valid_fire(monkeypatch):
    import core.proven_skill_gate as _skill
    monkeypatch.delenv("V3_PRECISION_GATE", raising=False)
    _patch(monkeypatch, up_p=0.72,
           precision_gate={"ok": True, "threshold": 0.68, "target": 0.70})
    monkeypatch.setattr(_skill, "symbol_review", lambda sym, **identity: {
        "ok": False, "symbol": sym, "resolved": 30, "wins": 10,
        "tp_rate": 0.3333, "fail_reason": "tp_rate<0.55 (0.3333)",
    })
    scores = {}
    sig, reason = _se.predict_live_ex("WOLF", "stock", scores=scores)
    assert sig is None
    assert reason == "skill_unproven"
    assert scores["proven_skill_gate_up"]["fail_reason"].startswith("tp_rate<")


def test_research_mode_bypasses_proven_skill_gate(monkeypatch):
    import core.proven_skill_gate as _skill
    monkeypatch.delenv("V3_PRECISION_GATE", raising=False)
    _patch(monkeypatch, up_p=0.72,
           precision_gate={"ok": True, "threshold": 0.68, "target": 0.70})
    monkeypatch.setattr(
        _skill, "symbol_review", lambda sym, **identity: {"ok": False, "fail_reason": "x"},
    )
    sig, reason = _se.predict_live_ex("WOLF", "stock", research_mode=True)
    assert sig is not None and sig[0] == "UP"


def test_overconfidence_gate_blocks_otherwise_valid_high_prob(monkeypatch):
    import core.proven_skill_gate as _skill
    monkeypatch.delenv("V3_PRECISION_GATE", raising=False)
    _patch(monkeypatch, up_p=0.82,
           precision_gate={"ok": True, "threshold": 0.68, "target": 0.70})
    monkeypatch.setattr(
        _skill, "symbol_review", lambda sym, **identity: {"ok": True, "symbol": sym},
    )
    monkeypatch.setattr(_skill, "global_calibration_review", lambda prob, **identity: {
        "ok": False, "prob": prob, "samples": 25, "wins": 10,
        "fail_reason": "high_prob_bucket_wr<0.55 (0.4000)",
    })
    scores = {}
    sig, reason = _se.predict_live_ex("WOLF", "stock", scores=scores)
    assert sig is None
    assert reason == "calibration_unproven"
    assert scores["overconfidence_gate_up"]["fail_reason"].startswith("high_prob_bucket_wr<")
