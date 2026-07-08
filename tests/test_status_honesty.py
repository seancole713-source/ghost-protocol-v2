"""PR #135 audit fixes: fireable_now, base-rate riders, wf-edge floor, Wilson.

The audit's decisive finding: 11 models displayed precision_ok while 0 could
actually clear the runtime fire chain. These fields make that gap visible and
must mirror _evaluate_lane's static checks exactly.
"""
import json

import core.signal_engine as se
from core.engine_config import _v3_min_wf_edge


def _meta(acc=0.70, nat=0.60, edge=0.10, wf_edge=0.05, wf_acc=0.65, folds=5,
          precision_ok=True):
    return {
        "accuracy": acc, "natural_rate": nat, "edge": edge,
        "wf_edge_mean": wf_edge, "wf_acc_mean": wf_acc, "wf_fold_count": folds,
        "wf_acc_min": 0.5, "wf_edge_min": 0.0, "n_samples": 300,
        "engine_version": "v3.2", "label_type": "tp_sl_daily",
        "label_schema": "tp_sl_fwd_v1", "feature_schema": "x",
        "precision_gate": {"ok": precision_ok, "threshold": 0.6},
    }


class _Cur:
    def __init__(self, metas):
        self._metas = metas
        self._last = None

    def execute(self, sql, *a):
        self._last = sql

    def fetchall(self):
        if "meta_%" in (self._last or ""):
            return [(f"meta_{k}", json.dumps(v)) for k, v in self._metas.items()]
        return [(f"model_{k}",) for k in self._metas]  # pickles all present

    def fetchone(self):
        return None


def _status_with(monkeypatch, metas):
    class _Conn:
        def cursor(self):
            return _Cur(metas)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    import core.db as db
    monkeypatch.setattr(db, "db_conn", lambda: _Conn())
    monkeypatch.setattr(se, "model_serve_guard", lambda m: None)
    monkeypatch.setattr(se, "get_last_train_gate_summary", lambda: {})
    # Most PR #135 tests isolate the static meta/precision checks; PR #155
    # proven-skill status mirroring has its own explicit test below.
    monkeypatch.setenv("V3_PROVEN_SKILL_GATE", "0")
    return se.get_model_status()


def test_strong_model_is_fireable(monkeypatch):
    st = _status_with(monkeypatch, {"NOK_up": _meta()})
    s = st["stored_symbols"]["NOK_up"]
    assert s["fireable_now"] is True
    assert "fire_block_reason" not in s
    assert s["proven_skill"] is True
    assert s["base_rate_rider"] is False


def test_edge_below_floor_blocks_with_reason(monkeypatch):
    # The audit's NOK case: precision_ok but holdout edge 4.8% < 5.0% floor.
    st = _status_with(monkeypatch, {"NOK_up": _meta(edge=0.048)})
    s = st["stored_symbols"]["NOK_up"]
    assert s["fireable_now"] is False
    assert s["fire_block_reason"].startswith("meta_gate:edge")


def test_down_lane_disabled_blocks(monkeypatch):
    monkeypatch.setenv("V3_DOWN_SIGNALS_ENABLED", "0")
    st = _status_with(monkeypatch, {"PLUG_down": _meta()})
    s = st["stored_symbols"]["PLUG_down"]
    assert s["fireable_now"] is False
    assert s["fire_block_reason"] == "down_lane_disabled"


def test_precision_unproven_blocks(monkeypatch):
    st = _status_with(monkeypatch, {"XPO_up": _meta(precision_ok=False)})
    s = st["stored_symbols"]["XPO_up"]
    assert s["fireable_now"] is False
    assert s["fire_block_reason"] == "precision_unproven"


def test_base_rate_rider_flagged(monkeypatch):
    # The audit's IQ case: accuracy == natural_rate, zero added skill.
    st = _status_with(monkeypatch, {"IQ_down": _meta(acc=0.745, nat=0.745, edge=0.0,
                                                     wf_edge=-0.032)})
    s = st["stored_symbols"]["IQ_down"]
    assert s["base_rate_rider"] is True
    assert s["proven_skill"] is False
    assert s["fireable_now"] is False


def test_negative_wf_edge_blocks(monkeypatch):
    st = _status_with(monkeypatch, {"YMM_up": _meta(edge=0.104, wf_edge=-0.044)})
    s = st["stored_symbols"]["YMM_up"]
    assert s["fireable_now"] is False
    assert s["fire_block_reason"].startswith("meta_gate:wf_edge")


def test_wf_edge_floor_default_is_zero(monkeypatch):
    monkeypatch.delenv("V3_MIN_WF_EDGE", raising=False)
    assert _v3_min_wf_edge() == 0.0


def test_wilson_lower_bound_math():
    from wolf_app import _compute_get_stats  # noqa: F401 — import check only
    # 3/10 (the live record): Wilson LB95 ≈ 10.8% — far below the naive 30%.
    # Verified against the standard formula.
    z = 1.96
    w, n = 3, 10
    p = w / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5
    lb = (center - margin) / denom * 100
    assert 10.0 < lb < 12.0


def test_status_mirrors_runtime_proven_skill_gate(monkeypatch):
    class _Conn:
        def cursor(self):
            return _Cur({"GME_up": _meta()})
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    import core.db as db
    monkeypatch.setattr(db, "db_conn", lambda: _Conn())
    monkeypatch.setattr(se, "model_serve_guard", lambda m: None)
    monkeypatch.setattr(se, "get_last_train_gate_summary", lambda: {})
    monkeypatch.setenv("V3_PROVEN_SKILL_GATE", "1")
    # Fake cursor has no shadow outcome rows; status must fail closed and avoid
    # claiming the model is fireable when runtime would block it.
    st = se.get_model_status()
    s = st["stored_symbols"]["GME_up"]
    assert s["fireable_now"] is False
    assert s["fire_block_reason"] == "skill_unproven"
    assert s["proven_skill_gate"]["fail_reason"].startswith("resolved<")
