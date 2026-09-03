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


def research_scoring_window_open(now=None) -> bool:
    """True only when the current US cash-session bar is safely complete."""
    from core.market_hours import AFTERHOURS_END_MIN, RTH_CLOSE_MIN, session_hm

    current, minute = session_hm(now)
    return (
        current.weekday() < 5
        and RTH_CLOSE_MIN + 60 <= minute < AFTERHOURS_END_MIN
    )


def completed_session_evaluation_date(bar_ts: Any, now=None) -> Optional[str]:
    """Return the CT session date only when the latest bar is today's close."""
    if not research_scoring_window_open(now):
        return None
    from core.market_hours import session_hm
    from core.tp_sl_resolve import _date_key

    current, _ = session_hm(now)
    session_date = current.date().isoformat()
    return session_date if _date_key(bar_ts) == session_date else None


def _artifact_direction(artifact: Dict[str, Any]) -> Optional[str]:
    """Return one unambiguous direction for a direction-specific artifact."""
    outputs = {
        str(value).upper()
        for value in (artifact.get("output_domain") or ())
        if str(value).upper() in ("UP", "DOWN")
    }
    if len(outputs) == 1:
        return next(iter(outputs))
    lineage_direction = str(artifact.get("policy_lineage_id") or "").rsplit("/", 1)[-1].upper()
    if lineage_direction in ("UP", "DOWN"):
        return lineage_direction
    return None


def _symbols_in_artifact_scope(symbols: List[str], symbol_scope: Any) -> List[str]:
    """Intersect explicit scopes; expand only the exact pooled-scope sentinel."""
    scope = {
        str(value).strip().upper()
        for value in (symbol_scope or ())
        if str(value).strip()
    }
    if scope == {"__UNIVERSE__"}:
        return list(symbols)
    if not scope or "__UNIVERSE__" in scope:
        return []
    return [symbol for symbol in symbols if str(symbol).strip().upper() in scope]

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

    Returns a frozen evaluation dict, including below-threshold abstentions,
    or None when no valid eligible score can be formed.
    """
    from core.research_artifacts import get_artifact
    from core.signal_engine import _fetch_ohlcv, _calculate_features, _active_feature_cols
    from core.feature_schema import attach_feature_asof
    from core.feature_audit import apply_inversions_to_features
    import numpy as np
    import pickle

    if not research_runner_enabled():
        return None
    if not research_scoring_window_open():
        return None

    # 1. Load artifact metadata
    artifact = get_artifact(artifact_sha)
    if not artifact:
        LOGGER.warning("Artifact not found: %s", artifact_sha[:16])
        return None
    if artifact.get("status") != "ACTIVE":
        LOGGER.warning("Artifact not active: %s status=%s", artifact_sha[:16], artifact.get("status"))
        return None
    artifact_direction = _artifact_direction(artifact)
    if artifact_direction is None or direction.upper() != artifact_direction:
        LOGGER.warning(
            "Artifact direction mismatch: %s requested=%s declared=%s",
            artifact_sha[:16], direction, artifact_direction,
        )
        return None

    # 2. Load model bytes
    payload = artifact.get("payload_bytes")
    if not payload:
        LOGGER.warning("Artifact has no payload: %s", artifact_sha[:16])
        return None

    try:
        import base64
        import binascii
        import hashlib
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
    evaluation_date = completed_session_evaluation_date(rows[-1].get("ts"))
    if evaluation_date is None:
        return None

    # 4. Build features (same path as production)
    # Windowed to the same trailing bar count training used, so ema200 /
    # ema_trend_bullish take the same code branch they did at fit time —
    # see core.signal_engine._serving_feature_bars.
    from core.signal_engine import _serving_feature_bars
    features = _calculate_features(_serving_feature_bars(rows))
    attach_feature_asof(features, rows[-1].get("ts") if rows else None, default_now=True)

    # 5. Get feature order from artifact
    feature_order = list(artifact.get("feature_order", _active_feature_cols()))

    # 6. Apply the exact feature transform frozen with the artifact.
    gate_proof = artifact.get("gate_proof", {}) or {}
    inversions = gate_proof.get("feature_inversions") or []
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

    # 10. Build frozen evaluation context
    now = int(time.time())
    feature_available_ts = features.get("feature_asof_ts", now)
    try:
        entry_price = float(rows[-1].get("close") or 0.0)
    except (TypeError, ValueError, OverflowError):
        entry_price = 0.0
    if entry_price <= 0.0:
        return None
    from core.tp_sl_resolve import tp_sl_prices_from_vol
    from core.vol_targets import base_vol_pct
    hold_bars = int(artifact.get("horizon_bars") or 0)
    if hold_bars < 1:
        return None
    target_price, stop_price = tp_sl_prices_from_vol(
        entry_price,
        base_vol_pct(symbol, asset_type),
        artifact_direction,
    )

    fired = prob >= threshold
    return {
        "contract_id": artifact.get("contract_id", ""),
        "artifact_sha": artifact_sha,
        "policy_lineage_id": artifact.get("policy_lineage_id", ""),
        "symbol": symbol.upper(),
        "direction": artifact_direction,
        "issued_ts": now,
        "evaluation_date": evaluation_date,
        "feature_available_ts": int(feature_available_ts) if feature_available_ts else now,
        "output": artifact_direction,
        "calibrated_prob": round(prob, 6),
        "threshold": threshold,
        "source_snapshot_sha": "",
        "feature_snapshot_sha": "",
        "selector_decision": {
            "passed": fired,
            "threshold": threshold,
            "prob": round(prob, 6),
            "reason": "threshold_pass" if fired else "below_threshold",
        },
        "context": {
            "asset_type": asset_type,
            "entry_price": entry_price,
            "target_price": target_price,
            "stop_price": stop_price,
            "hold_bars": hold_bars,
            "feature_order": feature_order,
            "feature_schema": artifact.get("feature_schema", ""),
            "feature_inversions": inversions,
        },
    }


def run_research_cycle(
    symbols: Optional[List[str]] = None,
    artifact_sha: Optional[str] = None,
    direction: str = "UP",
) -> Dict[str, int]:
    """Run one research scoring cycle across symbols.

    If no artifact_sha is provided, scores all ACTIVE research artifacts.
    Returns counts for eligible evaluations, issuance, skips, and errors.
    """
    if not research_runner_enabled():
        return {"scored": 0, "issued": 0, "skipped_no_data": 0,
                "skipped_below_threshold": 0, "skipped_already_evaluated": 0,
                "skipped_outside_window": 0, "errors": 0}
    if not research_scoring_window_open():
        return {"scored": 0, "issued": 0, "skipped_no_data": 0,
                "skipped_below_threshold": 0, "skipped_already_evaluated": 0,
                "skipped_outside_window": 1, "errors": 0}

    from core.research_ledger import log_research_evaluation, log_research_prediction
    from core.research_artifacts import list_artifacts
    from config.symbols import OFFICIAL_WATCHLIST

    if symbols is None:
        symbols = list(OFFICIAL_WATCHLIST)

    # If no specific artifact, score all ACTIVE artifacts
    if not artifact_sha:
        active_artifacts = list_artifacts(status="ACTIVE")
        artifact_shas = [a["artifact_sha"] for a in active_artifacts]
    else:
        artifact_shas = [artifact_sha]

    counts = {"scored": 0, "issued": 0, "skipped_no_data": 0,
              "skipped_below_threshold": 0, "skipped_already_evaluated": 0,
              "skipped_outside_window": 0, "errors": 0}

    for art_sha in artifact_shas:
        # Load artifact to get symbol_scope and output_domain
        from core.research_artifacts import get_artifact
        art = get_artifact(art_sha)
        if not art:
            continue
        art_symbols = _symbols_in_artifact_scope(symbols, art.get("symbol_scope"))
        artifact_direction = _artifact_direction(art)
        if artifact_direction is None:
            LOGGER.warning("Skipping artifact with ambiguous direction: %s", art_sha[:16])
            counts["errors"] += 1
            continue
        for sym in art_symbols:
            try:
                result = score_research_artifact(
                    artifact_sha=art_sha,
                    symbol=sym,
                    direction=artifact_direction,
                )
                if result is None:
                    counts["skipped_no_data"] += 1
                    continue
                counts["scored"] += 1
                decision = result.get("selector_decision") or {}
                fired = bool(decision.get("passed"))
                evaluated_ts = int(result["issued_ts"])
                evaluation_date = str(result.pop("evaluation_date"))
                from core.db import db_conn
                with db_conn() as conn:
                    cur = conn.cursor()
                    inserted = log_research_evaluation(
                        contract_id=result["contract_id"],
                        artifact_sha=result["artifact_sha"],
                        symbol=result["symbol"],
                        direction=result["direction"],
                        evaluation_date=evaluation_date,
                        evaluated_ts=evaluated_ts,
                        feature_available_ts=int(result["feature_available_ts"]),
                        calibrated_prob=float(result["calibrated_prob"]),
                        threshold=float(result["threshold"]),
                        eligible=True,
                        fired=fired,
                        reason=str(decision.get("reason") or ""),
                        metadata={"feature_schema": result["context"].get("feature_schema", "")},
                        cur=cur,
                    )
                    if not inserted:
                        counts["skipped_already_evaluated"] += 1
                        continue
                    if fired:
                        pred_id = log_research_prediction(**result, cur=cur)
                        if pred_id:
                            counts["issued"] += 1
                    else:
                        counts["skipped_below_threshold"] += 1
                    conn.commit()
            except Exception as e:
                LOGGER.warning("Research cycle error for %s/%s: %s", sym, art_sha[:16], str(e)[:120])
                counts["errors"] += 1

    return counts
