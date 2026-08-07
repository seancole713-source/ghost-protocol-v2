"""core/research_training.py — production-parity candidate training.

Trains models using the same backtest_symbol, purged splits, walk-forward
validation, calibration, ensemble, precision-gate, feature audit, and model
serialization as production. Writes only immutable research artifacts — never
touches ghost_v3_model, live predictions, shadow outcomes, wallets, or
performance logs.

The training function returns a candidate bundle (model bytes, metadata,
proof) that the caller decides whether to register as a research artifact.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import pickle
import time
from typing import Any, Dict, Optional

LOGGER = logging.getLogger("ghost.research_training")


def train_research_candidate(
    symbol: str,
    direction: str = "UP",
    *,
    asset_type: str = "stock",
    include_failed: bool = False,
) -> Optional[Dict[str, Any]]:
    """Train one candidate model using the production pipeline.

    Returns a candidate bundle with model_bytes, model_sha256, artifact_sha,
    metadata, and proof — or None if training fails any gate.

    This function does NOT write to any database. The caller is responsible
    for registering the artifact and persisting the model payload.
    """
    from core.signal_engine import (
        backtest_symbol,
        _active_feature_cols,
        _collect_peer_rows,
        _train_one_direction,
    )
    from core.engine_config import _v3_pool_training_enabled, _v3_wolf_sample_weight

    direction = direction.upper()
    if direction not in ("UP", "DOWN"):
        LOGGER.warning("Invalid direction: %s", direction)
        return None

    # 1. Backtest to get labeled rows
    up_rows, down_rows = backtest_symbol(symbol, asset_type)
    rows = up_rows if direction == "UP" else down_rows
    if not rows or len(rows) < 50:
        LOGGER.warning("Insufficient rows for %s/%s: %s", symbol, direction, len(rows) if rows else 0)
        return None

    # 2. Get active feature columns
    active_cols = _active_feature_cols()
    if not active_cols:
        LOGGER.warning("No active feature columns")
        return None

    # 3. Build the same direction-specific peer pool as production. The
    # no-pooling discovery variant reaches this path through its env override.
    pool_enabled = _v3_pool_training_enabled()
    peer_pools: Dict[str, list] = {"UP": [], "DOWN": []}
    peers_used = []
    if pool_enabled:
        peer_pools, peers_used = _collect_peer_rows(symbol)
    peer_rows = peer_pools.get(direction) or []
    pool_info = {
        "enabled": pool_enabled,
        "peer_sample_count": len(peer_rows),
        "peer_sample_count_down": len(peer_pools.get("DOWN") or []),
        "peers": peers_used,
        "wolf_sample_weight": _v3_wolf_sample_weight(),
    }

    # 4. Train using the production pipeline
    try:
        passed, detail, model_bytes, meta_json = _train_one_direction(
            rows, symbol, direction, active_cols,
            peer_rows, peers_used, pool_info,
        )
    except Exception as e:
        LOGGER.warning("Training failed for %s/%s: %s", symbol, direction, str(e)[:120])
        return None

    if not passed:
        LOGGER.info("Training gate failed for %s/%s: %s",
                     symbol, direction, detail.get("fail_reason", "unknown"))
        if include_failed:
            calibration = detail.get("calibration")
            precision_gate = (
                calibration.get("precision_gate", {})
                if isinstance(calibration, dict)
                else {}
            )
            return {
                "symbol": symbol,
                "direction": direction,
                "training_passed": False,
                "fail_reason": detail.get("fail_reason", "unknown"),
                "detail": detail,
                "precision_gate": precision_gate,
                "holdout": {
                    key: detail.get(key)
                    for key in (
                        "holdout_acc", "edge", "natural_rate", "no_skill_accuracy",
                        "wf_fold_count", "wf_acc_mean", "wf_acc_min",
                        "wf_edge_mean", "wf_edge_min",
                    )
                },
            }
        return None

    # 5. Compute model SHA-256 from raw bytes
    # model_bytes is a base64-encoded string from _train_one_direction.
    # Hash the raw bytes, not the base64 string.
    raw_model_bytes = base64.b64decode(model_bytes, validate=True)
    model_sha256 = hashlib.sha256(raw_model_bytes).hexdigest()

    # 6. Parse metadata
    meta = json.loads(meta_json) if isinstance(meta_json, str) else meta_json
    feature_order = tuple(meta.get("feature_cols", active_cols))

    # 7. Compute artifact package SHA using the canonical contract ID
    from core.research_artifacts import compute_artifact_sha
    from core.research_contracts import CURRENT_LIVE_CONTRACT_VERSION, get_contract
    from core.signal_engine import (
        _v3_feature_schema,
        _v3_label_schema,
        _v3_validation_schema,
        V3_LABEL_HOLD_BARS,
    )

    # Bind every variant to the preregistered task identity. A transient
    # feature-schema override may make the candidate incompatible with that
    # contract; discovery records and rejects that condition below rather than
    # silently changing contract identity mid-family.
    contract = get_contract("tp_sl_swing", CURRENT_LIVE_CONTRACT_VERSION)
    if contract is None:
        raise RuntimeError("current_research_contract_not_registered")
    contract_id = contract.contract_id()
    feature_schema = _v3_feature_schema()
    label_schema = _v3_label_schema()
    validation_schema = _v3_validation_schema()
    contract_compatible = (
        feature_schema == contract.feature_schema
        and label_schema == contract.evidence_schema
        and validation_schema == contract.validation_schema
        and V3_LABEL_HOLD_BARS == contract.horizon_bars
    )
    try:
        trained_at = int(float(meta["trained_at"]))
    except (KeyError, TypeError, ValueError, OverflowError):
        trained_at = int(time.time())

    calibration = detail.get("calibration")
    if not isinstance(calibration, dict):
        calibration = {}
    precision_info = calibration.get("precision_gate")
    if not isinstance(precision_info, dict):
        precision_info = {}
    feature_inversions = sorted({
        str(value) for value in (meta.get("feature_inversions") or ())
        if str(value) in feature_order
    })
    gate_proof = {
        key: detail.get(key)
        for key in (
            "holdout_acc", "edge", "natural_rate", "no_skill_accuracy",
            "wf_fold_count", "wf_acc_mean", "wf_acc_min",
            "wf_edge_mean", "wf_edge_min",
        )
    }
    gate_proof["gate_brier"] = calibration.get("gate_brier")
    gate_proof["gate_n"] = calibration.get("gate_n")
    gate_proof["feature_inversions"] = feature_inversions
    artifact_sha = compute_artifact_sha(
        model_sha256=model_sha256,
        contract_id=contract_id,
        direction=direction,
        policy_lineage_id=f"{symbol}/{direction}",
        policy_lineage_version=1,
        feature_order=feature_order,
        feature_schema=feature_schema,
        label_schema=label_schema,
        validation_schema=validation_schema,
        hold_bars=V3_LABEL_HOLD_BARS,
        calibration_proof=precision_info,
        gate_proof=gate_proof,
        symbol_scope=(symbol.upper(),),
        trained_at=trained_at,
    )

    # 8. Build candidate bundle
    return {
        "symbol": symbol,
        "direction": direction,
        "training_passed": True,
        "model_bytes": model_bytes,
        "model_sha256": model_sha256,
        "artifact_sha": artifact_sha,
        "feature_order": feature_order,
        "feature_schema": feature_schema,
        "label_schema": label_schema,
        "validation_schema": validation_schema,
        "hold_bars": V3_LABEL_HOLD_BARS,
        "contract_compatible": contract_compatible,
        "meta": meta,
        "detail": detail,
        "calibration_proof": precision_info,
        "precision_gate": precision_info,
        "holdout": gate_proof,
        "trained_at": trained_at,
    }


def serialize_model_bytes(model) -> bytes:
    """Serialize a fitted model to bytes using pickle protocol 5."""
    return pickle.dumps(model, protocol=5)


def encode_model_base64(model_bytes: bytes) -> str:
    """Base64-encode model bytes for storage (matches production format)."""
    return base64.b64encode(model_bytes).decode("ascii")


def decode_model_base64(encoded: str) -> bytes:
    """Base64-decode a stored model payload with validation."""
    import binascii
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"Invalid base64 model payload: {e}") from e


def verify_model_sha256(model_bytes: bytes, expected_sha256: str) -> bool:
    """Verify that raw model bytes match the expected SHA-256."""
    actual = hashlib.sha256(model_bytes).hexdigest()
    return actual == expected_sha256
