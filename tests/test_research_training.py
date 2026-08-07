"""Focused candidate-bundle identity tests for research training."""
from __future__ import annotations

import base64
import json
import pickle

from core.research_training import train_research_candidate


def test_candidate_preserves_production_precision_and_gate_metrics(monkeypatch):
    import core.signal_engine as signal_engine

    raw_model = pickle.dumps({"model": "candidate"}, protocol=5)
    encoded_model = base64.b64encode(raw_model).decode("ascii")
    detail = {
        "holdout_acc": 0.74,
        "edge": 0.12,
        "natural_rate": 0.55,
        "no_skill_accuracy": 0.55,
        "wf_fold_count": 5,
        "wf_acc_mean": 0.71,
        "wf_acc_min": 0.64,
        "wf_edge_mean": 0.09,
        "wf_edge_min": 0.04,
        "calibration": {
            "gate_brier": 0.18,
            "precision_gate": {"ok": True, "threshold": 0.72},
        },
    }
    meta = {
        "feature_cols": ["rsi", "macd"],
        "feature_inversions": ["macd"],
    }
    monkeypatch.setattr(
        signal_engine,
        "backtest_symbol",
        lambda symbol, asset_type: ([{"row": index} for index in range(50)], []),
    )
    monkeypatch.setattr(signal_engine, "_active_feature_cols", lambda: ["rsi", "macd"])
    monkeypatch.setenv("V3_POOL_TRAINING", "off")
    monkeypatch.setattr(
        signal_engine,
        "_train_one_direction",
        lambda *args, **kwargs: (True, detail, encoded_model, json.dumps(meta)),
    )

    candidate = train_research_candidate("WOLF", "UP")

    assert candidate is not None
    assert candidate["contract_compatible"] is False
    assert candidate["precision_gate"] == {"ok": True, "threshold": 0.72}
    assert candidate["calibration_proof"] == {
        "ok": True,
        "threshold": 0.72,
    }
    assert candidate["holdout"] == {
        "holdout_acc": 0.74,
        "edge": 0.12,
        "natural_rate": 0.55,
        "no_skill_accuracy": 0.55,
        "wf_fold_count": 5,
        "wf_acc_mean": 0.71,
        "wf_acc_min": 0.64,
        "wf_edge_mean": 0.09,
        "wf_edge_min": 0.04,
        "gate_brier": 0.18,
        "gate_n": None,
        "feature_inversions": ["macd"],
    }


def test_candidate_threads_production_pool_metadata(monkeypatch):
    import core.signal_engine as signal_engine

    raw_model = pickle.dumps({"model": "pooled"}, protocol=5)
    encoded_model = base64.b64encode(raw_model).decode("ascii")
    captured = {}

    monkeypatch.setenv("V3_POOL_TRAINING", "on")
    monkeypatch.setattr(
        signal_engine,
        "backtest_symbol",
        lambda symbol, asset_type: ([{"row": index} for index in range(50)], []),
    )
    monkeypatch.setattr(signal_engine, "_active_feature_cols", lambda: ["rsi"])
    monkeypatch.setattr(
        signal_engine,
        "_collect_peer_rows",
        lambda symbol: ({"UP": [{"peer": 1}], "DOWN": []}, [{"symbol": "PEER", "n": 1}]),
    )

    def fake_train(rows, symbol, direction, active_cols, peer_rows, peers_used, pool_info):
        captured.update(
            peer_rows=peer_rows,
            peers_used=peers_used,
            pool_info=pool_info,
        )
        detail = {
            "holdout_acc": 0.8,
            "wf_edge_mean": 0.1,
            "wf_fold_count": 5,
            "calibration": {"precision_gate": {}},
        }
        return True, detail, encoded_model, json.dumps({"feature_cols": ["rsi"]})

    monkeypatch.setattr(signal_engine, "_train_one_direction", fake_train)

    assert train_research_candidate("WOLF", "UP") is not None
    assert captured["peer_rows"] == [{"peer": 1}]
    assert captured["peers_used"] == [{"symbol": "PEER", "n": 1}]
    assert captured["pool_info"]["enabled"] is True
    assert captured["pool_info"]["peer_sample_count"] == 1


def test_discovery_gates_use_normalized_candidate_metrics():
    from scripts.research_discovery import evaluate_candidate_gates

    candidate = {
        "holdout": {
            "holdout_acc": 0.74,
            "edge": -0.50,
            "wf_edge_mean": 0.09,
            "wf_fold_count": 5,
            "gate_n": 12,
            "gate_brier": 0.18,
            "natural_rate": 0.55,
        },
        "precision_gate": {
            "ok": True,
            "target": 0.70,
            "threshold": 0.72,
            "calib": {"wins": 20, "support": 20},
            "gate": {"wins": 20, "support": 20, "wilson_low": 0.8389},
        },
    }

    result = evaluate_candidate_gates(candidate)

    assert result["passed"] is True
    values = {gate["name"]: gate["value"] for gate in result["gates"]}
    assert values["wf_edge"] == 0.09
    assert values["wf_folds"] == 5
    assert values["precision_gate"] == 0.8389


def test_discovery_rejects_contract_incompatible_candidate():
    from scripts.research_discovery import evaluate_candidate_gates

    candidate = {
        "contract_compatible": False,
        "holdout": {
            "holdout_acc": 0.90,
            "wf_edge_mean": 0.30,
            "wf_fold_count": 5,
            "gate_brier": 0.10,
            "natural_rate": 0.50,
        },
        "precision_gate": {
            "ok": True,
            "target": 0.70,
            "threshold": 0.8,
            "calib": {"wins": 20, "support": 20},
            "gate": {"wins": 20, "support": 20, "wilson_low": 0.84},
        },
    }

    result = evaluate_candidate_gates(candidate)

    assert result["passed"] is False
    gate = next(g for g in result["gates"] if g["name"] == "contract_compatible")
    assert gate["passed"] is False


def test_discovery_preflight_rejects_transient_schema_change(monkeypatch):
    from scripts.research_discovery import _frozen_contract_compatibility

    monkeypatch.setenv("V3_POOL_TRAINING", "off")

    compatible, reason = _frozen_contract_compatibility()

    assert compatible is False
    assert "feature_schema" in reason


def test_discovery_recomputes_precision_proof_and_selects_one_finalist():
    from scripts.research_discovery import (
        evaluate_candidate_gates,
        select_forward_finalists,
    )

    forged = {
        "holdout": {
            "holdout_acc": 0.80,
            "wf_edge_mean": 0.20,
            "wf_fold_count": 5,
            "gate_brier": 0.10,
            "natural_rate": 0.50,
        },
        "precision_gate": {
            "ok": True,
            "target": 0.70,
            "threshold": 0.72,
            "calib": {"wins": 10, "support": 10},
            "gate": {"wins": 5, "support": 10, "wilson_low": 0.90},
        },
    }
    assert evaluate_candidate_gates(forged)["passed"] is False

    candidates = [
        {
            "type": "geometry",
            "variant": "geometry",
            "artifact_sha": "c" * 64,
            "gates": [{"name": "precision_sidak", "value": 0.99}],
        },
        {
            "type": "production",
            "variant": "second",
            "artifact_sha": "b" * 64,
            "wf_edge": 0.20,
            "holdout_acc": 0.80,
            "gates": [
                {"name": "precision_sidak", "value": 0.75},
                {"name": "gate_support", "value": 20},
            ],
        },
        {
            "type": "production",
            "variant": "first",
            "artifact_sha": "a" * 64,
            "wf_edge": 0.10,
            "holdout_acc": 0.70,
            "gates": [
                {"name": "precision_sidak", "value": 0.80},
                {"name": "gate_support", "value": 10},
            ],
        },
    ]

    selected = select_forward_finalists(candidates)

    assert len(selected) == 1
    assert selected[0]["variant"] == "first"


def test_discovery_environment_restoration_is_lossless(monkeypatch):
    from scripts.research_discovery import _apply_env, _restore_env

    monkeypatch.setenv("V3_POOL_TRAINING", "custom")
    previous = _apply_env({"V3_POOL_TRAINING": "0", "V3_ENSEMBLE_ENABLED": "1"})
    assert previous == {
        "V3_POOL_TRAINING": "custom",
        "V3_ENSEMBLE_ENABLED": None,
    }

    _restore_env(previous)

    assert __import__("os").environ["V3_POOL_TRAINING"] == "custom"
    assert "V3_ENSEMBLE_ENABLED" not in __import__("os").environ


def test_discovery_registers_one_finalist_with_family_evidence(monkeypatch, tmp_path):
    import json

    import core.research_artifacts as artifacts
    import core.research_forward as forward
    import core.research_training as training
    import scripts.research_discovery as discovery

    precision = {
        "ok": True,
        "target": 0.70,
        "threshold": 0.72,
        "calib": {"wins": 20, "support": 20},
        "gate": {"wins": 20, "support": 20, "wilson_low": 0.8389},
    }
    candidate = {
        "symbol": "WOLF",
        "direction": "UP",
        "model_bytes": "payload",
        "model_sha256": "b" * 64,
        "artifact_sha": "a" * 64,
        "feature_order": ("rsi",),
        "feature_schema": "features",
        "label_schema": "labels",
        "validation_schema": "validation",
        "hold_bars": 3,
        "calibration_proof": precision,
        "precision_gate": precision,
        "holdout": {
            "holdout_acc": 0.80,
            "edge": 0.30,
            "natural_rate": 0.50,
            "no_skill_accuracy": 0.50,
            "wf_fold_count": 5,
            "wf_acc_mean": 0.75,
            "wf_acc_min": 0.70,
            "wf_edge_mean": 0.25,
            "wf_edge_min": 0.20,
            "gate_brier": 0.16,
            "gate_n": 20,
            "feature_inversions": [],
        },
        "trained_at": 1_700_000_000,
    }
    monkeypatch.setattr(
        training, "train_research_candidate", lambda symbol, direction: dict(candidate),
    )
    monkeypatch.setattr(artifacts, "register_artifact", lambda meta, payload_bytes: True)
    registrations = []

    def _register(**kwargs):
        registrations.append(kwargs)
        return "fwd_test"

    monkeypatch.setattr(forward, "register_forward_experiment", _register)
    out_path = tmp_path / "discovery.json"

    report = discovery.run_discovery(
        symbols=["WOLF"], direction="UP", out_path=str(out_path),
    )

    assert report["persisted"] == 1
    assert report["finalist_count"] == 1
    assert len(registrations) == 1
    registration = registrations[0]
    assert report["family_size"] == 12
    assert registration["family_size"] == 12
    assert registration["family_correction"] == "sidak"
    assert registration["selection_evidence"]["hypothesis_count"] == 12
    assert registration["selection_evidence"]["selected_variant"] == "baseline_xgb"
    assert json.loads(out_path.read_text())["status"] == "FINALIST_SELECTED"


def test_discovery_reports_finalist_persistence_failure(monkeypatch, tmp_path):
    import core.research_artifacts as artifacts
    import core.research_forward as forward
    import core.research_training as training
    import scripts.research_discovery as discovery

    precision = {
        "ok": True,
        "target": 0.70,
        "threshold": 0.72,
        "calib": {"wins": 20, "support": 20},
        "gate": {"wins": 20, "support": 20, "wilson_low": 0.8389},
    }
    candidate = {
        "symbol": "WOLF",
        "direction": "UP",
        "model_bytes": "payload",
        "model_sha256": "b" * 64,
        "artifact_sha": "a" * 64,
        "feature_order": ("rsi",),
        "feature_schema": "features",
        "label_schema": "labels",
        "validation_schema": "validation",
        "hold_bars": 3,
        "calibration_proof": precision,
        "precision_gate": precision,
        "holdout": {
            "holdout_acc": 0.80,
            "edge": 0.30,
            "natural_rate": 0.50,
            "no_skill_accuracy": 0.50,
            "wf_fold_count": 5,
            "wf_acc_mean": 0.75,
            "wf_acc_min": 0.70,
            "wf_edge_mean": 0.25,
            "wf_edge_min": 0.20,
            "gate_brier": 0.16,
            "gate_n": 20,
            "feature_inversions": [],
        },
        "trained_at": 1_700_000_000,
    }
    monkeypatch.setattr(
        training, "train_research_candidate", lambda symbol, direction: dict(candidate),
    )
    monkeypatch.setattr(
        artifacts, "register_artifact", lambda meta, payload_bytes: True,
    )
    monkeypatch.setattr(forward, "register_forward_experiment", lambda **kwargs: None)
    out_path = tmp_path / "failed-discovery.json"

    report = discovery.run_discovery(
        symbols=["WOLF"], direction="UP", out_path=str(out_path),
    )

    assert report["status"] == "PERSISTENCE_FAILED"
    assert report["persisted"] == 0
    assert report["finalist_count"] == 1
    assert report["persistence_errors"] == [
        {"symbol": "WOLF", "error": "forward_registration_returned_no_id"},
    ]
