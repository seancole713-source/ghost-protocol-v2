"""core/research_activation.py — lock-first activation, leases, and rollback.

Acquires a per-symbol/direction PostgreSQL advisory lock before reading the
incumbent or checking eligibility. Revalidates the exact proof under that lock,
atomically writes model and metadata, and invalidates caches only after commit.

Key invariants:
  - Never clear an existing engine pause.
  - Never create a pick during activation.
  - Rollback only to a predecessor that still passes every current check.
  - Activation grants storage only; live gates still control firing.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

LOGGER = logging.getLogger("ghost.research_activation")
ACTIVATION_LEASE_MAX_S = 30 * 86400
_ROLLBACK_FRESHNESS_RESERVE_S = 86400

# Feature toggle
def auto_activation_enabled() -> bool:
    import os
    return os.getenv("RESEARCH_AUTO_ACTIVATION", "0") in ("1", "true", "TRUE")


def _lock_name(symbol: str, direction: str) -> str:
    return f"ghost_v3_model:{symbol.upper()}:{direction.upper()}"


def _acquire_model_lock(cur, symbol: str, direction: str) -> None:
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        (_lock_name(symbol, direction),),
    )


_DEFAULT_LEASE_WINDOW_S = 7 * 86400
_DEFAULT_LEASE_MIN_OBSERVATIONS = 3


def _lease_window_s() -> int:
    import os
    return min(
        ACTIVATION_LEASE_MAX_S,
        max(86400, int(os.getenv("RESEARCH_LEASE_WINDOW_S", str(_DEFAULT_LEASE_WINDOW_S)))),
    )


def _lease_min_observations() -> int:
    import os
    return max(1, int(os.getenv("RESEARCH_LEASE_MIN_OBSERVATIONS", str(_DEFAULT_LEASE_MIN_OBSERVATIONS))))


def ensure_activation_tables(cur) -> None:
    """Create activation tables if they don't exist.

    The central schema (core/research_schema.py) owns ghost_research_activation_log
    and ghost_research_activation_predecessors. This function is kept for backward
    compatibility with tests that call it directly.
    """
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_research_activation_log (
            id SERIAL PRIMARY KEY,
            event_type TEXT NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            artifact_sha TEXT NOT NULL,
            registration_id TEXT,
            review_id TEXT,
            predecessor_artifact_sha TEXT,
            lease_expires_at BIGINT,
            proof_snapshot JSONB,
            reason TEXT,
            created_at BIGINT NOT NULL
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_activation_symbol_dir "
        "ON ghost_research_activation_log (symbol, direction, created_at DESC)"
    )
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_research_activation_predecessors (
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            artifact_sha TEXT NOT NULL,
            model_bytes TEXT NOT NULL,
            meta_json TEXT NOT NULL,
            stored_at BIGINT NOT NULL,
            UNIQUE (symbol, direction)
        )
    """)


def compute_evidence_lease(*, artifact_sha: str, symbol: str, direction: str, cur=None) -> Dict[str, Any]:
    now = int(time.time())
    if cur is not None:
        return _lease_impl(cur, artifact_sha, symbol, direction, now)
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        return _lease_impl(c, artifact_sha, symbol, direction, now)


def _lease_impl(cur, artifact_sha, symbol, direction, now) -> Dict[str, Any]:
    cur.execute(
        """SELECT event_type, artifact_sha, lease_expires_at, created_at
           FROM ghost_research_activation_log
           WHERE symbol = %s AND direction = %s
           ORDER BY id DESC LIMIT 1""",
        (symbol.upper(), direction.upper()))
    row = cur.fetchone()
    if not row:
        return {"active": False, "reason": "no_activation_record"}
    if row[0] != "ACTIVATED" or str(row[1]) != artifact_sha:
        return {"active": False, "reason": "not_current_activation"}
    lease_expires = int(row[2]) if row[2] else 0
    activated_at = int(row[3])
    cur.execute(
        """SELECT
               COUNT(*) FILTER (WHERE r.outcome IN ('WIN','LOSS','EXPIRED')),
               COUNT(*) FILTER (WHERE r.outcome = 'WIN'),
               COUNT(*) FILTER (WHERE r.outcome NOT IN ('WIN','LOSS','EXPIRED'))
           FROM ghost_research_predictions p
           JOIN ghost_research_resolutions r ON r.prediction_id = p.id
           WHERE p.artifact_sha = %s AND p.symbol = %s AND p.direction = %s
             AND p.issued_ts > %s AND r.evidence_available_ts <= %s""",
        (artifact_sha, symbol.upper(), direction.upper(), activated_at, now))
    evidence = cur.fetchone() or (0, 0, 0)
    recent_obs = int(evidence[0] or 0)
    recent_wins = int(evidence[1] or 0)
    recent_invalid = int(evidence[2] or 0)
    recent_total = recent_obs + recent_invalid
    invalid_rate = recent_invalid / recent_total if recent_total else 0.0
    from core.binomial_stats import V2_TARGET, wilson_upper_bound
    recent_upper = (
        wilson_upper_bound(recent_wins, recent_obs) if recent_obs else 1.0
    )
    reason = "lease_active_collecting"
    active = True
    if lease_expires <= now:
        active = False
        reason = "activation_lease_expired"
    elif recent_total >= _lease_min_observations() and invalid_rate > 0.10:
        active = False
        reason = "post_activation_invalid_rate"
    elif recent_obs >= _lease_min_observations() and recent_upper < V2_TARGET:
        active = False
        reason = "post_activation_evidence_futile"
    elif recent_obs >= _lease_min_observations():
        reason = "lease_active_evidence_acceptable"
    return {"active": active, "reason": reason,
            "artifact_sha": artifact_sha, "symbol": symbol.upper(),
            "direction": direction.upper(), "activated_at": activated_at,
            "lease_expires_at": lease_expires, "lease_remaining_s": max(0, lease_expires - now),
            "recent_observations": recent_obs, "recent_wins": recent_wins,
            "recent_invalid": recent_invalid, "recent_invalid_rate": invalid_rate,
            "recent_wilson_upper": recent_upper,
            "min_observations": _lease_min_observations(),
            "lease_window_s": _lease_window_s()}


def can_activate(*, artifact_sha: str, symbol: str, direction: str, cur=None) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    from core.research_contracts import get_contract_by_id, is_live_compatible
    from core.research_artifacts import artifact_integrity_error, get_artifact
    artifact = get_artifact(artifact_sha, cur=cur)
    if not artifact:
        return False, "artifact_not_found", None
    integrity_error = artifact_integrity_error(artifact)
    if integrity_error:
        return False, f"artifact_integrity_failed:{integrity_error}", None
    contract = get_contract_by_id(artifact.get("contract_id", ""))
    if not contract:
        return False, "contract_not_found", None
    if not is_live_compatible(contract):
        return False, "not_live_compatible", None
    if artifact.get("status") != "ACTIVE":
        return False, f"artifact_not_active: {artifact.get('status')}", None
    symbol = str(symbol or "").upper()
    direction = str(direction or "").upper()
    output_domain = {str(value).upper() for value in artifact.get("output_domain") or ()}
    if output_domain != {direction}:
        return False, "artifact_direction_mismatch", None
    symbol_scope = {str(value).upper() for value in artifact.get("symbol_scope") or ()}
    if "__UNIVERSE__" not in symbol_scope and symbol not in symbol_scope:
        return False, "artifact_symbol_scope_mismatch", None
    if artifact.get("feature_schema") != contract.feature_schema:
        return False, "artifact_feature_schema_mismatch", None
    if artifact.get("evidence_schema") != contract.evidence_schema:
        return False, "artifact_evidence_schema_mismatch", None
    if artifact.get("validation_schema") != contract.validation_schema:
        return False, "artifact_validation_schema_mismatch", None
    if artifact.get("horizon_bars") != contract.horizon_bars:
        return False, "artifact_horizon_mismatch", None
    return True, "eligible", artifact


def activate_artifact(*, symbol: str, direction: str, artifact_sha: str,
                      registration_id: str, review_id: Optional[str] = None, cur=None) -> Dict[str, Any]:
    if not auto_activation_enabled():
        return {"ok": False, "reason": "auto_activation_disabled"}
    symbol = symbol.upper()
    direction = direction.upper()
    if cur is not None:
        _acquire_model_lock(cur, symbol, direction)
        return _activate_impl(cur, symbol, direction, artifact_sha, registration_id, review_id)
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        _acquire_model_lock(c, symbol, direction)
        result = _activate_impl(c, symbol, direction, artifact_sha, registration_id, review_id)
        conn.commit()
        if result.get("ok"):
            from core.signal_engine import invalidate_model_cache
            invalidate_model_cache(symbol)
        return result


def _activate_impl(cur, symbol, direction, artifact_sha, registration_id, review_id) -> Dict[str, Any]:
    now = int(time.time())

    # Re-validate eligibility under lock
    eligible, reason, artifact = can_activate(
        artifact_sha=artifact_sha, symbol=symbol, direction=direction, cur=cur)
    if not eligible:
        return {"ok": False, "reason": reason}
    if artifact is None:
        return {"ok": False, "reason": "artifact_not_found"}

    from core.research_forward import evaluate_forward_proof
    proof = evaluate_forward_proof(registration_id, cur=cur)
    if proof.get("status") != "PROVEN":
        return {"ok": False, "reason": f"forward_proof_not_proven: {proof.get('status')}"}
    if proof.get("persisted_status") != "PROVEN" or not proof.get("closed_at_ts"):
        return {"ok": False, "reason": "forward_proof_not_persisted"}

    # ── Identity binding: registration must match artifact, symbol, direction ──
    reg_artifact_sha = proof.get("artifact_sha", "")
    reg_direction = proof.get("direction", "")
    reg_contract_id = proof.get("contract_id", "")
    if reg_artifact_sha != artifact_sha:
        return {"ok": False, "reason": f"registration_artifact_mismatch: reg={reg_artifact_sha[:16]}, req={artifact_sha[:16]}"}
    if reg_direction.upper() != direction.upper():
        return {"ok": False, "reason": f"registration_direction_mismatch: reg={reg_direction}, req={direction}"}
    if reg_contract_id != artifact.get("contract_id", ""):
        return {"ok": False, "reason": f"registration_contract_mismatch: reg={reg_contract_id[:16]}, artifact={artifact.get('contract_id', '')[:16]}"}
    cur.execute(
        """SELECT 1 FROM ghost_research_activation_log
           WHERE event_type = 'ACTIVATED' AND symbol = %s AND direction = %s
             AND artifact_sha = %s LIMIT 1""",
        (symbol, direction, artifact_sha),
    )
    if cur.fetchone():
        return {"ok": False, "reason": "artifact_activation_lease_already_used"}

    payload = artifact.get("payload_bytes")
    if not payload:
        return {"ok": False, "reason": "no_model_payload"}
    import base64
    import binascii
    import pickle
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as e:
        return {"ok": False, "reason": f"invalid_base64: {str(e)[:80]}"}
    expected_sha = artifact.get("model_sha256", "")
    if expected_sha:
        actual_sha = hashlib.sha256(raw).hexdigest()
        if actual_sha != expected_sha:
            return {"ok": False, "reason": "model_sha_mismatch"}
    try:
        pickle.loads(raw)
    except Exception as e:
        return {"ok": False, "reason": f"unpickle_failed: {str(e)[:80]}"}
    cur.execute("SELECT value FROM ghost_v3_model WHERE key = %s", (f"model_{symbol}_{direction.lower()}",))
    incumbent = cur.fetchone()
    if not incumbent:
        return {"ok": False, "reason": "incumbent_missing"}
    cur.execute("SELECT value FROM ghost_v3_model WHERE key = %s",
                (f"meta_{symbol}_{direction.lower()}",))
    meta_row = cur.fetchone()
    if not meta_row:
        return {"ok": False, "reason": "incumbent_metadata_missing"}
    incumbent_meta, predecessor_error = _validated_live_model(
        incumbent[0], meta_row[0], direction,
    )
    if predecessor_error or incumbent_meta is None:
        return {
            "ok": False,
            "reason": (
                "incumbent_not_serveable: "
                f"{predecessor_error or 'metadata_missing'}"
            ),
        }
    if incumbent_meta.get("activation_proof") is not None:
        return {"ok": False, "reason": "activated_incumbent_not_restorable"}
    predecessor_sha = str(
        incumbent_meta.get("activation_artifact_sha")
        or incumbent_meta.get("model_sha256")
    )
    gate_proof = artifact.get("gate_proof")
    if not isinstance(gate_proof, dict):
        return {"ok": False, "reason": "artifact_gate_proof_missing"}
    calibration_proof = artifact.get("calibration_proof")
    if not isinstance(calibration_proof, dict):
        return {"ok": False, "reason": "artifact_calibration_proof_missing"}
    from core.precision_gate import validate_fire_proof
    if not validate_fire_proof(calibration_proof):
        return {"ok": False, "reason": "artifact_precision_gate_invalid"}
    lease_expires_at = now + _lease_window_s()
    predecessor_serveable_until = _model_serveable_until(incumbent_meta)
    if predecessor_serveable_until < lease_expires_at + _ROLLBACK_FRESHNESS_RESERVE_S:
        return {"ok": False, "reason": "incumbent_freshness_insufficient_for_lease"}
    cur.execute(
        """INSERT INTO ghost_research_activation_predecessors
           (symbol, direction, artifact_sha, model_bytes, meta_json, stored_at)
           VALUES (%s,%s,%s,%s,%s,%s)
           ON CONFLICT (symbol, direction) DO UPDATE
           SET artifact_sha = EXCLUDED.artifact_sha, model_bytes = EXCLUDED.model_bytes,
               meta_json = EXCLUDED.meta_json, stored_at = EXCLUDED.stored_at""",
        (symbol, direction, predecessor_sha, incumbent[0], meta_row[0], now),
    )
    from core.signal_engine import LABEL_TYPE, model_serve_guard
    meta = {"tier": "proven", "direction": direction, "model_sha256": expected_sha,
            "label_type": LABEL_TYPE, "label_schema": artifact.get("evidence_schema", ""),
            "feature_schema": artifact.get("feature_schema", ""),
            "validation_schema": artifact.get("validation_schema", ""),
            "label_hold_bars": artifact.get("horizon_bars"),
            "feature_cols": list(artifact.get("feature_order", [])),
            "trained_at": artifact.get("trained_at", now),
            "accuracy": gate_proof.get("holdout_acc"),
            "natural_rate": gate_proof.get("natural_rate"),
            "no_skill_accuracy": gate_proof.get("no_skill_accuracy"),
            "edge": gate_proof.get("edge"),
            "wf_fold_count": gate_proof.get("wf_fold_count"),
            "wf_acc_mean": gate_proof.get("wf_acc_mean"),
            "wf_acc_min": gate_proof.get("wf_acc_min"),
            "wf_edge_mean": gate_proof.get("wf_edge_mean"),
            "wf_edge_min": gate_proof.get("wf_edge_min"),
            "gate_brier": gate_proof.get("gate_brier"),
            "precision_gate": calibration_proof,
            "feature_inversions": list(gate_proof.get("feature_inversions") or ()),
            "model_payload_bytes": len(raw),
            "activation_artifact_sha": artifact_sha,
            "activated_at": now,
            "activation_lease_expires_at": lease_expires_at,
            "activation_proof": {"registration_id": registration_id, "wins": proof.get("wins"),
                                 "n": proof.get("n"), "wilson_low": proof.get("wilson", {}).get("exact_low"),
                                 "status": proof.get("status"),
                                 "persisted_status": proof.get("persisted_status"),
                                 "closed_at_ts": proof.get("closed_at_ts"),
                                 "all_secondary_pass": proof.get("all_secondary_pass")}}
    serve_reject = model_serve_guard(meta, expected_direction=direction)
    if serve_reject:
        return {"ok": False, "reason": f"activation_metadata_unserveable: {serve_reject}"}
    model_key = f"model_{symbol}_{direction.lower()}"
    meta_key = f"meta_{symbol}_{direction.lower()}"
    cur.execute(
        "INSERT INTO ghost_v3_model(key,value,updated_at) VALUES(%s,%s,%s) "
        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at",
        (model_key, payload, now),
    )
    cur.execute(
        "INSERT INTO ghost_v3_model(key,value,updated_at) VALUES(%s,%s,%s) "
        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at",
        (meta_key, json.dumps(meta), now),
    )
    cur.execute(
        """INSERT INTO ghost_research_activation_log
           (event_type, symbol, direction, artifact_sha, registration_id, review_id,
                predecessor_artifact_sha, lease_expires_at, proof_snapshot, reason, created_at)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        ("ACTIVATED", symbol, direction, artifact_sha, registration_id, review_id,
            predecessor_sha, lease_expires_at, json.dumps(proof), "forward_proof_passed", now))
    LOGGER.info("Activated %s/%s -> %s (proof: %s/%s wins)", symbol, direction, artifact_sha[:16],
                proof.get("wins"), proof.get("n"))
    return {"ok": True, "symbol": symbol, "direction": direction, "artifact_sha": artifact_sha,
            "registration_id": registration_id, "predecessor_sha": predecessor_sha,
            "lease_expires_at": lease_expires_at, "proof_snapshot": proof}


def rollback_artifact(*, symbol: str, direction: str, reason: str = "", cur=None) -> Dict[str, Any]:
    symbol = symbol.upper()
    direction = direction.upper()
    if cur is not None:
        _acquire_model_lock(cur, symbol, direction)
        return _rollback_impl(cur, symbol, direction, reason)
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        _acquire_model_lock(c, symbol, direction)
        result = _rollback_impl(c, symbol, direction, reason)
        conn.commit()
        if result.get("ok"):
            from core.signal_engine import invalidate_model_cache
            invalidate_model_cache(symbol)
        return result


def _rollback_impl(cur, symbol, direction, reason) -> Dict[str, Any]:
    now = int(time.time())
    cur.execute(
        "SELECT value FROM ghost_v3_model WHERE key = %s",
        (f"meta_{symbol}_{direction.lower()}",),
    )
    active_meta_row = cur.fetchone()
    active_artifact_sha = ""
    if active_meta_row:
        try:
            active_meta = json.loads(active_meta_row[0])
            if isinstance(active_meta, dict):
                active_artifact_sha = str(
                    active_meta.get("activation_artifact_sha") or ""
                )
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    if not active_artifact_sha:
        cur.execute(
            """SELECT artifact_sha FROM ghost_research_activation_log
               WHERE symbol = %s AND direction = %s AND event_type = 'ACTIVATED'
               ORDER BY id DESC LIMIT 1""",
            (symbol, direction),
        )
        active_event = cur.fetchone()
        active_artifact_sha = str(active_event[0]) if active_event else ""
    cur.execute("""SELECT artifact_sha, model_bytes, meta_json
                   FROM ghost_research_activation_predecessors
                   WHERE symbol = %s AND direction = %s""", (symbol, direction))
    pred = cur.fetchone()
    if not pred:
        _hard_pause_engine(cur, symbol, direction, "no_predecessor_for_rollback")
        return {"ok": False, "reason": "no_predecessor", "engine_paused": True}
    pred_sha, model_bytes, meta_json = pred
    _meta, predecessor_error = _validated_live_model(model_bytes, meta_json, direction)
    if predecessor_error:
        _hard_pause_engine(cur, symbol, direction, f"predecessor_invalid: {predecessor_error}")
        return {"ok": False, "reason": "predecessor_corrupt", "engine_paused": True}
    model_key = f"model_{symbol}_{direction.lower()}"
    meta_key = f"meta_{symbol}_{direction.lower()}"
    cur.execute(
        "INSERT INTO ghost_v3_model(key,value,updated_at) VALUES(%s,%s,%s) "
        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at",
        (model_key, model_bytes, now),
    )
    cur.execute(
        "INSERT INTO ghost_v3_model(key,value,updated_at) VALUES(%s,%s,%s) "
        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at",
        (meta_key, meta_json, now),
    )
    cur.execute(
        """INSERT INTO ghost_research_activation_log
           (event_type, symbol, direction, artifact_sha, predecessor_artifact_sha, reason, created_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
          ("ROLLED_BACK", symbol, direction, active_artifact_sha or pred_sha,
            pred_sha, reason, now))
    LOGGER.warning("Rolled back %s/%s to predecessor %s: %s", symbol, direction, pred_sha[:16], reason)
    return {"ok": True, "symbol": symbol, "direction": direction, "restored_artifact_sha": pred_sha, "reason": reason}


def rollback_if_degraded(*, symbol: str, direction: str, cur=None) -> Dict[str, Any]:
    symbol = symbol.upper()
    direction = direction.upper()
    if cur is not None:
        _acquire_model_lock(cur, symbol, direction)
        return _rollback_check_impl(cur, symbol, direction)
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        _acquire_model_lock(c, symbol, direction)
        result = _rollback_check_impl(c, symbol, direction)
        if result.get("ok"):
            conn.commit()
            from core.signal_engine import invalidate_model_cache
            invalidate_model_cache(symbol)
        return result


def review_active_leases() -> Dict[str, Any]:
    """Review each lane whose latest activation event is still active."""
    from core.db import db_conn

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT symbol, direction
               FROM (
                   SELECT DISTINCT ON (symbol, direction)
                          symbol, direction, event_type, id
                   FROM ghost_research_activation_log
                   ORDER BY symbol, direction, id DESC
               ) latest
               WHERE event_type = 'ACTIVATED'
               ORDER BY symbol, direction"""
        )
        lanes = [(str(row[0]), str(row[1])) for row in cur.fetchall()]

    results = []
    for symbol, direction in lanes:
        try:
            result = rollback_if_degraded(symbol=symbol, direction=direction)
        except Exception as exc:
            LOGGER.error(
                "Activation lease review failed for %s/%s: %s",
                symbol, direction, str(exc)[:120],
            )
            result = {
                "ok": False,
                "symbol": symbol,
                "direction": direction,
                "reason": str(exc)[:120],
            }
        results.append(result)
    return {
        "reviewed": len(lanes),
        "rolled_back": sum(
            1 for result in results
            if result.get("ok") is True and result.get("restored_artifact_sha")
        ),
        "failed": sum(1 for result in results if result.get("ok") is False),
        "results": results,
    }


def _rollback_check_impl(cur, symbol, direction) -> Dict[str, Any]:
    cur.execute(
        """SELECT event_type, artifact_sha FROM ghost_research_activation_log
           WHERE symbol = %s AND direction = %s ORDER BY id DESC LIMIT 1""",
        (symbol, direction),
    )
    activation_event = cur.fetchone()
    if not activation_event or activation_event[0] != "ACTIVATED":
        return {"action": "none", "reason": "no_current_activation"}
    logged_artifact_sha = str(activation_event[1])
    cur.execute(
        "SELECT value FROM ghost_v3_model WHERE key = %s",
        (f"model_{symbol}_{direction.lower()}",),
    )
    model_row = cur.fetchone()
    cur.execute(
        "SELECT value FROM ghost_v3_model WHERE key = %s",
        (f"meta_{symbol}_{direction.lower()}",),
    )
    meta_row = cur.fetchone()
    model_bytes = model_row[0] if model_row else None
    meta_json = meta_row[0] if meta_row else None
    if not model_bytes or not meta_json:
        return _rollback_impl(cur, symbol, direction, "active_model_state_missing")
    try:
        meta = json.loads(meta_json)
    except Exception:
        return _rollback_impl(cur, symbol, direction, "active_model_metadata_invalid")
    activation_proof = meta.get("activation_proof") if isinstance(meta, dict) else None
    if not activation_proof:
        return _rollback_impl(cur, symbol, direction, "activation_metadata_missing")
    artifact_sha = str(meta.get("activation_artifact_sha") or "")
    if artifact_sha != logged_artifact_sha:
        return _rollback_impl(cur, symbol, direction, "activation_artifact_mismatch")
    current_meta, current_error = _validated_live_model(model_bytes, meta, direction)
    if current_error and current_error != "activation_lease_expired":
        return _rollback_impl(cur, symbol, direction, f"active_model_invalid:{current_error}")
    lease = _lease_impl(cur, artifact_sha, symbol, direction, int(time.time()))
    if int(meta.get("activated_at") or 0) != int(lease.get("activated_at") or 0):
        return _rollback_impl(cur, symbol, direction, "activation_log_metadata_mismatch")
    if int(meta.get("activation_lease_expires_at") or 0) != int(lease.get("lease_expires_at") or 0):
        return _rollback_impl(cur, symbol, direction, "activation_log_metadata_mismatch")
    if lease.get("active"):
        return {"action": "none", "reason": lease["reason"], "lease": lease}
    return _rollback_impl(cur, symbol, direction, str(lease.get("reason") or "activation_lease_invalid"))


def _validated_live_model(
    model_bytes: Any, meta_json: Any, direction: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    import base64
    import binascii
    import pickle

    try:
        meta = json.loads(meta_json) if isinstance(meta_json, str) else meta_json
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, "invalid_meta_json"
    if not isinstance(meta, dict):
        return None, "invalid_meta_json"
    from core.signal_engine import model_serve_guard
    reject = model_serve_guard(meta, expected_direction=direction)
    if reject:
        return None, reject
    try:
        raw = base64.b64decode(model_bytes, validate=True)
    except (binascii.Error, TypeError, ValueError):
        return None, "invalid_base64"
    expected_sha = str(meta.get("model_sha256") or "").lower()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        return None, "model_sha_mismatch"
    try:
        pickle.loads(raw)
    except Exception:
        return None, "unpickle_failed"
    return meta, None


def _model_serveable_until(meta: Dict[str, Any]) -> int:
    if meta.get("activation_proof") is not None:
        return int(meta.get("activation_lease_expires_at") or 0)
    try:
        return int(float(meta["trained_at"]) + 14 * 86400)
    except (KeyError, TypeError, ValueError, OverflowError):
        return 0


def get_activation_history(*, symbol: Optional[str] = None, direction: Optional[str] = None,
                           limit: int = 50, cur=None) -> List[Dict[str, Any]]:
    if cur is not None:
        return _history_impl(cur, symbol, direction, limit)
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        return _history_impl(c, symbol, direction, limit)


def _history_impl(cur, symbol, direction, limit) -> List[Dict[str, Any]]:
    where = ""
    params: List[Any] = []
    if symbol:
        where += " AND symbol = %s"
        params.append(symbol.upper())
    if direction:
        where += " AND direction = %s"
        params.append(direction.upper())
    cur.execute(
        f"""SELECT event_type, symbol, direction, artifact_sha, predecessor_artifact_sha,
                   lease_expires_at, reason, created_at
            FROM ghost_research_activation_log WHERE 1=1 {where}
            ORDER BY created_at DESC LIMIT %s""",
        params + [limit])
    return [{"event_type": r[0], "symbol": r[1], "direction": r[2],
             "artifact_sha": r[3][:16] if r[3] else "",
             "predecessor_artifact_sha": r[4][:16] if r[4] else None,
             "lease_expires_at": r[5], "reason": r[6], "created_at": r[7]}
            for r in cur.fetchall()]


def _hard_pause_engine(cur, symbol, direction, reason) -> None:
    now = int(time.time())
    pause_reason = f"research_activation_safety:{symbol}/{direction}:{reason}"
    for key, value in (
        ("engine_paused", "1"),
        ("engine_pause_reason", pause_reason),
        ("engine_pause_ts", str(now)),
        ("engine_pause_latched", "1"),
    ):
        cur.execute(
            "INSERT INTO ghost_state(key, val) VALUES(%s, %s) "
            "ON CONFLICT(key) DO UPDATE SET val = EXCLUDED.val",
            (key, value),
        )
    cur.execute("DELETE FROM ghost_state WHERE key='engine_pause_auto_resume_at'")
    LOGGER.error("HARD PAUSE %s/%s: %s", symbol, direction, reason)
