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

# Feature toggle
def auto_activation_enabled() -> bool:
    import os
    return os.getenv("RESEARCH_AUTO_ACTIVATION", "0") in ("1", "true", "TRUE")


def _lock_key(symbol: str, direction: str) -> int:
    return hash(f"ghost_v3_model:{symbol.upper()}:{direction.upper()}") & 0x7FFFFFFF


_DEFAULT_LEASE_WINDOW_S = 7 * 86400
_DEFAULT_LEASE_MIN_OBSERVATIONS = 3


def _lease_window_s() -> int:
    import os
    return max(86400, int(os.getenv("RESEARCH_LEASE_WINDOW_S", str(_DEFAULT_LEASE_WINDOW_S))))


def _lease_min_observations() -> int:
    import os
    return max(1, int(os.getenv("RESEARCH_LEASE_MIN_OBSERVATIONS", str(_DEFAULT_LEASE_MIN_OBSERVATIONS))))


def ensure_activation_tables(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_research_activation_events (
            id SERIAL PRIMARY KEY,
            event_type TEXT NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            artifact_sha TEXT NOT NULL,
            predecessor_artifact_sha TEXT,
            promotion_review_id INT,
            lease_expires_at BIGINT,
            reason TEXT DEFAULT '',
            metadata JSONB,
            created_at BIGINT NOT NULL
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_activation_symbol "
        "ON ghost_research_activation_events (symbol, direction, created_at DESC)"
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
        """SELECT event_type, lease_expires_at, created_at
           FROM ghost_research_activation_events
           WHERE symbol = %s AND direction = %s AND artifact_sha = %s AND event_type = 'ACTIVATED'
           ORDER BY created_at DESC LIMIT 1""",
        (symbol.upper(), direction.upper(), artifact_sha))
    row = cur.fetchone()
    if not row:
        return {"active": False, "reason": "no_activation_record"}
    lease_expires = int(row[1]) if row[1] else 0
    activated_at = int(row[2])
    cur.execute(
        """SELECT COUNT(*) FROM ghost_research_predictions p
           JOIN ghost_research_resolutions r ON r.prediction_id = p.id
           WHERE p.artifact_sha = %s AND p.symbol = %s AND p.direction = %s AND r.resolved_ts > %s""",
        (artifact_sha, symbol.upper(), direction.upper(), now - _lease_window_s()))
    recent_obs = int((cur.fetchone() or [0])[0])
    valid = lease_expires > now and recent_obs >= _lease_min_observations()
    return {"active": valid, "artifact_sha": artifact_sha, "symbol": symbol.upper(),
            "direction": direction.upper(), "activated_at": activated_at,
            "lease_expires_at": lease_expires, "lease_remaining_s": max(0, lease_expires - now),
            "recent_observations": recent_obs, "min_observations": _lease_min_observations(),
            "lease_window_s": _lease_window_s()}


def can_activate(*, artifact_sha: str, symbol: str, direction: str, cur=None) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    from core.research_contracts import get_contract_by_id, is_live_compatible
    from core.research_artifacts import get_artifact
    artifact = get_artifact(artifact_sha, cur=cur)
    if not artifact:
        return False, "artifact_not_found", None
    contract = get_contract_by_id(artifact.get("contract_id", ""))
    if not contract:
        return False, "contract_not_found", None
    if not is_live_compatible(contract):
        return False, "not_live_compatible", None
    if artifact.get("status") != "ACTIVE":
        return False, f"artifact_not_active: {artifact.get('status')}", None
    return True, "eligible", artifact


def activate_artifact(*, symbol: str, direction: str, artifact_sha: str,
                      registration_id: str, review_id: Optional[str] = None, cur=None) -> Dict[str, Any]:
    if not auto_activation_enabled():
        return {"ok": False, "reason": "auto_activation_disabled"}
    symbol = symbol.upper()
    direction = direction.upper()
    if cur is not None:
        return _activate_impl(cur, symbol, direction, artifact_sha, registration_id, review_id)
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(symbol, direction),))
        result = _activate_impl(c, symbol, direction, artifact_sha, registration_id, review_id)
        conn.commit()
        return result


def _activate_impl(cur, symbol, direction, artifact_sha, registration_id, review_id) -> Dict[str, Any]:
    now = int(time.time())
    from core.research_artifacts import get_artifact
    artifact = get_artifact(artifact_sha, cur=cur)
    if not artifact:
        return {"ok": False, "reason": f"artifact_not_found: {artifact_sha[:16]}"}
    if artifact.get("status") != "ACTIVE":
        return {"ok": False, "reason": f"artifact_not_active: {artifact.get('status')}"}
    from core.research_forward import evaluate_forward_proof
    proof = evaluate_forward_proof(registration_id, cur=cur)
    if proof.get("status") != "PROVEN":
        return {"ok": False, "reason": f"forward_proof_not_proven: {proof.get('status')}"}
    payload = artifact.get("payload_bytes")
    if not payload:
        return {"ok": False, "reason": "no_model_payload"}
    import base64, binascii, pickle
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
    predecessor_sha = None
    if incumbent:
        cur.execute(
            """INSERT INTO ghost_research_activation_predecessors
               (symbol, direction, artifact_sha, model_bytes, meta_json, stored_at)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (symbol, direction) DO UPDATE
               SET artifact_sha = EXCLUDED.artifact_sha, model_bytes = EXCLUDED.model_bytes,
                   meta_json = EXCLUDED.meta_json, stored_at = EXCLUDED.stored_at""",
            (symbol, direction, "incumbent", incumbent[0], "{}", now))
        predecessor_sha = hashlib.sha256(
            incumbent[0].encode() if isinstance(incumbent[0], str) else incumbent[0]).hexdigest()[:16]
    meta = {"tier": "proven", "direction": direction, "model_sha256": expected_sha,
            "label_type": "tp_sl", "label_schema": artifact.get("label_schema", ""),
            "feature_schema": artifact.get("feature_schema", ""),
            "validation_schema": artifact.get("validation_schema", ""),
            "label_hold_bars": artifact.get("hold_bars", 3),
            "feature_cols": list(artifact.get("feature_order", [])),
            "trained_at": artifact.get("trained_at", now),
            "holdout_acc": artifact.get("calibration_proof", {}).get("holdout_acc"),
            "edge": artifact.get("calibration_proof", {}).get("edge"),
            "activation_proof": {"registration_id": registration_id, "wins": proof.get("wins"),
                                 "n": proof.get("n"), "wilson_low": proof.get("wilson", {}).get("exact_low")}}
    model_key = f"model_{symbol}_{direction.lower()}"
    meta_key = f"meta_{symbol}_{direction.lower()}"
    cur.execute("INSERT INTO ghost_v3_model(key, value) VALUES(%s, %s) ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value",
                (model_key, payload))
    cur.execute("INSERT INTO ghost_v3_model(key, value) VALUES(%s, %s) ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value",
                (meta_key, json.dumps(meta)))
    cur.execute(
        """INSERT INTO ghost_research_activation_log
           (event_type, symbol, direction, artifact_sha, registration_id, review_id,
            predecessor_artifact_sha, proof_snapshot, reason, created_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        ("ACTIVATED", symbol, direction, artifact_sha, registration_id, review_id,
         predecessor_sha, json.dumps(proof), "forward_proof_passed", now))
    from core.signal_engine import invalidate_model_cache
    invalidate_model_cache()
    LOGGER.info("Activated %s/%s -> %s (proof: %s/%s wins)", symbol, direction, artifact_sha[:16],
                proof.get("wins"), proof.get("n"))
    return {"ok": True, "symbol": symbol, "direction": direction, "artifact_sha": artifact_sha,
            "registration_id": registration_id, "predecessor_sha": predecessor_sha, "proof_snapshot": proof}


def rollback_artifact(*, symbol: str, direction: str, reason: str = "", cur=None) -> Dict[str, Any]:
    symbol = symbol.upper()
    direction = direction.upper()
    if cur is not None:
        return _rollback_impl(cur, symbol, direction, reason)
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(symbol, direction),))
        result = _rollback_impl(c, symbol, direction, reason)
        conn.commit()
        return result


def _rollback_impl(cur, symbol, direction, reason) -> Dict[str, Any]:
    now = int(time.time())
    cur.execute("""SELECT artifact_sha, model_bytes, meta_json
                   FROM ghost_research_activation_predecessors
                   WHERE symbol = %s AND direction = %s""", (symbol, direction))
    pred = cur.fetchone()
    if not pred:
        _hard_pause_engine(cur, symbol, direction, "no_predecessor_for_rollback")
        return {"ok": False, "reason": "no_predecessor", "engine_paused": True}
    pred_sha, model_bytes, meta_json = pred
    import base64, binascii, pickle
    try:
        raw = base64.b64decode(model_bytes, validate=True)
        pickle.loads(raw)
    except Exception as e:
        _hard_pause_engine(cur, symbol, direction, f"predecessor_unpickle_failed: {str(e)[:80]}")
        return {"ok": False, "reason": "predecessor_corrupt", "engine_paused": True}
    model_key = f"model_{symbol}_{direction.lower()}"
    meta_key = f"meta_{symbol}_{direction.lower()}"
    cur.execute("INSERT INTO ghost_v3_model(key, value) VALUES(%s, %s) ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value",
                (model_key, model_bytes))
    cur.execute("INSERT INTO ghost_v3_model(key, value) VALUES(%s, %s) ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value",
                (meta_key, meta_json))
    cur.execute(
        """INSERT INTO ghost_research_activation_log
           (event_type, symbol, direction, artifact_sha, predecessor_artifact_sha, reason, created_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        ("ROLLED_BACK", symbol, direction, pred_sha, None, reason, now))
    from core.signal_engine import invalidate_model_cache
    invalidate_model_cache()
    LOGGER.warning("Rolled back %s/%s to predecessor %s: %s", symbol, direction, pred_sha[:16], reason)
    return {"ok": True, "symbol": symbol, "direction": direction, "restored_artifact_sha": pred_sha, "reason": reason}


def rollback_if_degraded(*, symbol: str, direction: str, cur=None) -> Dict[str, Any]:
    symbol = symbol.upper()
    direction = direction.upper()
    if cur is not None:
        return _rollback_check_impl(cur, symbol, direction)
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        return _rollback_check_impl(c, symbol, direction)


def _rollback_check_impl(cur, symbol, direction) -> Dict[str, Any]:
    cur.execute("SELECT value FROM ghost_v3_model WHERE key = %s", (f"meta_{symbol}_{direction.lower()}",))
    row = cur.fetchone()
    if not row:
        return {"action": "none", "reason": "no_active_model"}
    try:
        meta = json.loads(row[0])
    except Exception:
        return {"action": "none", "reason": "invalid_meta_json"}
    activation_proof = meta.get("activation_proof") if isinstance(meta, dict) else None
    if not activation_proof:
        return {"action": "none", "reason": "not_an_activated_artifact"}
    wins = activation_proof.get("wins", 0)
    n = activation_proof.get("n", 0)
    from core.binomial_stats import wilson_pass
    if wilson_pass(wins, n, 0.70):
        return {"action": "none", "reason": "proof_still_valid"}
    return rollback_artifact(symbol=symbol, direction=direction, reason="proof_degraded", cur=cur)


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
            FROM ghost_research_activation_events WHERE 1=1 {where}
            ORDER BY created_at DESC LIMIT %s""",
        params + [limit])
    return [{"event_type": r[0], "symbol": r[1], "direction": r[2],
             "artifact_sha": r[3][:16] if r[3] else "",
             "predecessor_artifact_sha": r[4][:16] if r[4] else None,
             "lease_expires_at": r[5], "reason": r[6], "created_at": r[7]}
            for r in cur.fetchall()]


def _hard_pause_engine(cur, symbol, direction, reason) -> None:
    now = int(time.time())
    cur.execute(
        "INSERT INTO ghost_state(key, val) VALUES(%s, %s) ON CONFLICT(key) DO UPDATE SET val = EXCLUDED.val",
        (f"engine_pause_hard_{symbol}_{direction}", json.dumps({"reason": reason, "paused_at": now})))
    LOGGER.error("HARD PAUSE %s/%s: %s", symbol, direction, reason)
