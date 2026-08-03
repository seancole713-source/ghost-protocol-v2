"""core/research_runner.py — isolated research scoring lane.

Loads and verifies research artifacts without consulting ghost_v3_model.
Builds the same point-in-time feature snapshot as production, scores the
exact artifact, applies its frozen selector/threshold, and appends at most
one prediction per artifact/symbol/trading date to the research ledger.

Never calls live pick saving, Telegram, wallet, P&L, or live model writers.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("ghost.research_runner")

# Feature toggle — must be explicitly enabled
def research_runner_enabled() -> bool:
    import os
    return os.getenv("RESEARCH_RUNNER_ENABLED", "0") in ("1", "true", "TRUE")


def score_research_artifact(
    *,
    artifact_sha: str,
    symbol: str,
    direction: str = "UP",
    asset_type: str = "stock",
) -> Optional[Dict[str, Any]]:
    """Score one symbol with one research artifact.

    Returns a prediction dict suitable for log_research_prediction, or None
    if the artifact cannot be loaded, the symbol has no data, or the
    probability is below the artifact's frozen threshold.
    """
    from core.research_artifacts import get_artifact
    from core.signal_engine import _fetch_ohlcv, _calculate_features, _active_feature_cols
    from core.feature_schema import attach_feature_asof
    from core.feature_audit import apply_inversions_to_features
    import numpy as np
    import pickle

    if not research_runner_enabled():
        return None

    # 1. Load artifact metadata
    artifact = get_artifact(artifact_sha)
    if not artifact:
        LOGGER.warning("Artifact not found: %s", artifact_sha[:16])
        return None
    if artifact.get("status") != "ACTIVE":
        LOGGER.warning("Artifact not active: %s status=%s", artifact_sha[:16], artifact.get("status"))
        return None

    # 2. Load model bytes
    payload = artifact.get("payload_bytes")
    if not payload:
        LOGGER.warning("Artifact has no payload: %s", artifact_sha[:16])
        return None

    try:
        import base64, binascii, hashlib
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as e:
        LOGGER.warning("Invalid base64 payload for %s: %s", artifact_sha[:16], str(e)[:80])
        return None

    # Verify model SHA
    expected_sha = artifact.get("model_sha256", "")
    if expected_sha:
        actual_sha = hashlib.sha256(raw).hexdigest()
        if actual_sha != expected_sha:
            LOGGER.warning("Model SHA mismatch for %s", artifact_sha[:16])
            return None

    try:
        model = pickle.loads(raw)
    except Exception as e:
        LOGGER.warning("Failed to unpickle model %s: %s", artifact_sha[:16], str(e)[:80])
        return None

    # 3. Fetch OHLCV data
    rows = _fetch_ohlcv(symbol, asset_type, period="1y", interval="1d")
    if not rows or len(rows) < 30:
        return None

    # 4. Build features (same path as production)
    features = _calculate_features(rows)
    attach_feature_asof(features, rows[-1].get("ts") if rows else None, default_now=True)

    # 5. Get feature order from artifact
    feature_order = list(artifact.get("feature_order", _active_feature_cols()))

    # 6. Apply feature inversions from artifact metadata
    meta = artifact.get("meta", {}) or {}
    inversions = meta.get("feature_inversions") or []
    if inversions:
        features = apply_inversions_to_features(dict(features), inversions)

    # 7. Build feature vector
    X = np.array([[features.get(c, 0.0) for c in feature_order]])

    # 8. Score
    try:
        proba = model.predict_proba(X)[0]
        prob = float(proba[1])
    except Exception as e:
        LOGGER.warning("Scoring failed for %s/%s: %s", symbol, artifact_sha[:16], str(e)[:80])
        return None

    # 9. Get frozen threshold
    threshold = artifact.get("calibration_proof", {}).get("threshold", 0.55) if isinstance(artifact.get("calibration_proof"), dict) else 0.55

    # 10. Check threshold
    if prob < threshold:
        return None

    # 11. Build prediction context
    now = int(time.time())
    feature_available_ts = features.get("feature_asof_ts", now)

    return {
        "contract_sha": artifact.get("contract_id", ""),
        "artifact_sha": artifact_sha,
        "policy_lineage_id": artifact.get("policy_lineage_id", ""),
        "symbol": symbol.upper(),
        "direction": direction,
        "issued_ts": now,
        "feature_available_ts": int(feature_available_ts) if feature_available_ts else now,
        "output": direction,
        "calibrated_prob": round(prob, 6),
        "threshold": threshold,
        "source_snapshot_sha": "",
        "feature_snapshot_sha": "",
        "selector_decision": {"passed": True, "threshold": threshold, "prob": round(prob, 6)},
        "context": {
            "feature_order": feature_order,
            "feature_schema": artifact.get("feature_schema", ""),
        },
    }


def run_research_cycle(
    symbols: Optional[List[str]] = None,
    artifact_sha: Optional[str] = None,
    direction: str = "UP",
) -> Dict[str, int]:
    """Run one research scoring cycle across symbols.

    Returns counts: {scored, issued, skipped_no_data, skipped_below_threshold, errors}
    """
    if not research_runner_enabled():
        return {"scored": 0, "issued": 0, "skipped_no_data": 0, "skipped_below_threshold": 0, "errors": 0}

    from core.research_ledger import log_research_prediction
    from config.symbols import OFFICIAL_WATCHLIST

    if symbols is None:
        symbols = list(OFFICIAL_WATCHLIST)

    counts = {"scored": 0, "issued": 0, "skipped_no_data": 0, "skipped_below_threshold": 0, "errors": 0}

    for sym in symbols:
        try:
            result = score_research_artifact(
                artifact_sha=artifact_sha or "",
                symbol=sym,
                direction=direction,
            )
            counts["scored"] += 1
            if result is None:
                counts["skipped_below_threshold"] += 1
                continue

            pred_id = log_research_prediction(**result)
            if pred_id:
                counts["issued"] += 1
        except Exception as e:
            LOGGER.warning("Research cycle error for %s: %s", sym, str(e)[:120])
            counts["errors"] += 1

    return counts
