"""Precision-targeted firing gate — the 70% contract (Phase 3).

A model may only fire live picks above a probability threshold that
DEMONSTRABLY produced >= target precision (win rate among fired picks) on
out-of-sample data. "70% accuracy" stops being a hope and becomes a per-model
admission requirement:

  * At train time the threshold is CHOSEN on the calibration slice (lowest
    threshold whose picks won >= target with enough support) and VALIDATED on
    the untouched gate slice. Both must clear or the model is marked unproven.
  * At predict time (core.signal_engine._evaluate_lane) an unproven model
    cannot fire live picks at all — it still journals shadow probabilities and
    still serves research picks, which are excluded from accuracy stats.

No proof, no fire. Selectivity is the lever: a symbol whose model can't
demonstrate a >=70%-precision operating point out-of-sample contributes
nothing to live accuracy except risk.

Env knobs (read at call time so ops can retune without deploy):
  V3_PRECISION_GATE              on|off (default on)
  V3_PRECISION_TARGET            default 0.70
  V3_PRECISION_MIN_SUPPORT       min calib-slice picks at threshold (default 10)
  V3_PRECISION_GATE_MIN_SUPPORT  min gate-slice picks at threshold (default 5)

Proof is strict: the untouched gate slice's 95% Wilson lower bound must clear
V3_PRECISION_TARGET. Small raw win rates are never presented as proof.
"""
import logging
import math
import numbers
import os
import re
import threading
from typing import Any, Dict, Optional, Sequence
from core.db import ensure_ghost_state

LOGGER = logging.getLogger("ghost.precision_gate")
_SYMBOL_PROOF_SCHEMA = "effective_market_sessions_v1"


def _exact_int(value: Any) -> Optional[int]:
    """Parse persisted JSON counts without truncating fractional evidence."""
    if isinstance(value, bool):
        return None
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        numeric = float(value)
        return int(numeric) if math.isfinite(numeric) and numeric.is_integer() else None
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        try:
            return int(value.strip())
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def precision_gate_enabled() -> bool:
    return (os.getenv("V3_PRECISION_GATE", "on") or "on").strip().lower() not in (
        "0", "off", "false", "no",
    )


def precision_target() -> float:
    from core.accuracy_contract import resolve_float
    return resolve_float("V3_PRECISION_TARGET", "precision_target", lo=0.50, hi=0.95)


def _min_support_calib() -> int:
    return max(1, int(os.getenv("V3_PRECISION_MIN_SUPPORT", "10")))


def _min_support_gate() -> int:
    return max(1, int(os.getenv("V3_PRECISION_GATE_MIN_SUPPORT", "5")))


def wilson_lower_bound(wins: int, n: int, z: float = 1.96) -> float:
    """95% Wilson score lower bound on a win rate — the honest small-sample floor."""
    if n <= 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def threshold_search(
    probs: Sequence[float],
    labels: Sequence[int],
    target: float,
    min_support: int,
) -> Optional[Dict[str, Any]]:
    """Lowest threshold whose picks (prob >= t) won >= target with enough support.

    Lowest valid threshold maximizes coverage; precision is not monotonic in t,
    so every observed probability is tried as a candidate. Returns None when no
    operating point reaches the target.
    """
    n = min(len(probs), len(labels))
    if n == 0:
        return None
    pairs = sorted(zip(
        (float(prob) for prob in probs),
        (int(bool(label)) for label in labels),
    ))
    best = None
    # Suffix sums over probs sorted ascending: picks at threshold pairs[i][0]
    # are pairs[i:]. Walk from the lowest candidate up; first valid wins.
    total_wins = sum(label for _, label in pairs)
    remaining = n
    wins = total_wins
    for i, (p, _l) in enumerate(pairs):
        # Ties: "prob >= p" always selects from the FIRST occurrence of p, so
        # later duplicates are not valid evaluation points.
        is_first_occurrence = i == 0 or pairs[i - 1][0] != p
        support = remaining
        if support < min_support:
            break
        if is_first_occurrence:
            precision = wins / support
            if precision >= target:
                raw_wilson = wilson_lower_bound(wins, support)
                best = {
                    # Preserve exact values used for admission. Display-rounded
                    # values are telemetry only and must never turn a statistical
                    # failure into a pass at the contract boundary.
                    "threshold": float(p),
                    "precision": round(precision, 4),
                    "support": support,
                    "wins": wins,
                    "wilson_low": round(raw_wilson, 4),
                    "_wilson_low_raw": raw_wilson,
                }
                break
        wins -= pairs[i][1]
        remaining -= 1
    return best


def _hold_bars(value: Optional[int] = None) -> int:
    from core.engine_config import V3_LABEL_HOLD_BARS
    return max(1, int(V3_LABEL_HOLD_BARS if value is None else value))


def _effective_session_stats(
    labels: Sequence[int], timestamps: Optional[Sequence[int]], hold_bars: int,
) -> Optional[Dict[str, Any]]:
    """Conservative support for clustered, overlapping daily labels."""
    if timestamps is None or len(timestamps) != len(labels) or not labels:
        return None
    sessions = set()
    try:
        for value in timestamps:
            if isinstance(value, bool):
                return None
            parsed = float(value)
            if not math.isfinite(parsed) or parsed <= 0 or not parsed.is_integer():
                return None
            sessions.add(int(parsed) // 86400)
    except (TypeError, ValueError, OverflowError):
        return None
    distinct_sessions = len(sessions)
    horizon = max(1, int(hold_bars))
    effective_support = max(1, distinct_sessions // horizon)
    raw_wins = sum(int(bool(label)) for label in labels)
    effective_wins = min(
        effective_support,
        int(math.floor((raw_wins / len(labels)) * effective_support + 1e-12)),
    )
    raw_wilson = wilson_lower_bound(effective_wins, effective_support)
    return {
        "distinct_sessions": distinct_sessions,
        "hold_bars": horizon,
        "effective_support": effective_support,
        "effective_wins": effective_wins,
        "effective_wilson_low": round(raw_wilson, 4),
        "_effective_wilson_low_raw": raw_wilson,
    }


def _slice_stats(
    probs, labels, threshold: float, *,
    session_timestamps: Optional[Sequence[int]] = None,
    hold_bars: Optional[int] = None,
) -> Dict[str, Any]:
    timestamp_values = (
        list(session_timestamps) if session_timestamps is not None else None
    )
    if timestamp_values is not None and len(timestamp_values) != min(len(probs), len(labels)):
        timestamp_values = None
    picked = []
    for idx, (prob, label) in enumerate(zip(probs, labels)):
        if float(prob) < threshold:
            continue
        session_ts = timestamp_values[idx] if timestamp_values is not None else None
        picked.append((float(prob), int(bool(label)), session_ts))
    support = len(picked)
    wins = sum(label for _, label, _ in picked)
    raw_wilson = wilson_lower_bound(wins, support) if support else None
    out = {
        "support": support,
        "wins": wins,
        "precision": round(wins / support, 4) if support else None,
        "wilson_low": round(raw_wilson, 4) if raw_wilson is not None else None,
        "_wilson_low_raw": raw_wilson,
    }
    effective = _effective_session_stats(
        [label for _, label, _ in picked],
        [ts for _, _, ts in picked] if timestamp_values is not None else None,
        _hold_bars(hold_bars),
    )
    if effective:
        out.update(effective)
    return out


def validate_fire_proof(proof: Any) -> bool:
    """Revalidate persisted symbol proof from exact integer evidence.

    Persisted booleans and rounded Wilson telemetry are not authority. A proof
    remains serveable only when its support/win counts reproduce admission under
    the current (never weaker) runtime contract.
    """
    if (
        not isinstance(proof, dict)
        or proof.get("ok") is not True
        or proof.get("proof_schema") != _SYMBOL_PROOF_SCHEMA
    ):
        return False
    try:
        threshold = float(proof["threshold"])
        stored_target = float(proof["target"])
        calib = proof["calib"]
        gate = proof["gate"]
        calib_support = _exact_int(calib["support"])
        calib_wins = _exact_int(calib["wins"])
        gate_support = _exact_int(gate["support"])
        gate_wins = _exact_int(gate["wins"])
        distinct_sessions = _exact_int(gate["distinct_sessions"])
        hold_bars = _exact_int(gate["hold_bars"])
        effective_support = _exact_int(gate["effective_support"])
        effective_wins = _exact_int(gate["effective_wins"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    numeric = (threshold, stored_target)
    if not all(math.isfinite(value) for value in numeric):
        return False
    if not 0.0 <= threshold <= 1.0 or not 0.5 <= stored_target <= 0.95:
        return False
    if any(value is None for value in (
        calib_support, calib_wins, gate_support, gate_wins,
        distinct_sessions, hold_bars, effective_support, effective_wins,
    )):
        return False
    target = max(stored_target, precision_target())
    if calib_support < _min_support_calib() or not 0 <= calib_wins <= calib_support:
        return False
    if gate_support < _min_support_gate() or not 0 <= gate_wins <= gate_support:
        return False
    current_hold = _hold_bars()
    if (
        hold_bars != current_hold
        or not 1 <= distinct_sessions <= gate_support
        or effective_support != max(1, distinct_sessions // current_hold)
        or effective_support < _min_support_gate()
        or not 0 <= effective_wins <= effective_support
    ):
        return False
    expected_effective_wins = int(math.floor(
        (gate_wins / gate_support) * effective_support + 1e-12,
    ))
    if effective_wins != expected_effective_wins:
        return False
    if calib_wins / calib_support < target:
        return False
    return (
        wilson_lower_bound(gate_wins, gate_support) >= target
        and wilson_lower_bound(effective_wins, effective_support) >= target
    )


def select_fire_threshold(
    calib_probs: Sequence[float],
    calib_labels: Sequence[int],
    gate_probs: Sequence[float],
    gate_labels: Sequence[int],
    target: Optional[float] = None,
    *,
    gate_timestamps: Optional[Sequence[int]] = None,
    hold_bars: Optional[int] = None,
) -> Dict[str, Any]:
    """Choose the fire threshold on the calib slice, validate on the gate slice.

    Returns a dict stored in model meta as `precision_gate`:
      ok:        True only when a threshold cleared the target on calib AND held
                 (within slack) on the untouched gate slice with enough support.
      threshold: the chosen operating point (present even when ok=False if a
                 calib candidate existed, for observability).
    """
    tgt = precision_target() if target is None else float(target)
    out: Dict[str, Any] = {
        "ok": False,
        "target": round(tgt, 4),
        "proof_schema": _SYMBOL_PROOF_SCHEMA,
    }
    candidate = threshold_search(calib_probs, calib_labels, tgt, _min_support_calib())
    if candidate is None:
        out["fail_reason"] = "no_calib_operating_point"
        out["calib_n"] = int(min(len(calib_probs), len(calib_labels)))
        return out
    thr = float(candidate["threshold"])
    out["threshold"] = thr
    out["calib"] = candidate
    gate_stats = _slice_stats(
        gate_probs, gate_labels, thr,
        session_timestamps=gate_timestamps,
        hold_bars=hold_bars,
    )
    out["gate"] = gate_stats
    min_gate = _min_support_gate()
    if gate_stats["support"] < min_gate:
        out["fail_reason"] = f"gate_support<{min_gate} ({gate_stats['support']})"
        return out
    if "effective_support" not in gate_stats:
        out["fail_reason"] = "gate_session_timestamps_required"
        return out
    if gate_stats["effective_support"] < min_gate:
        out["fail_reason"] = (
            f"gate_effective_support<{min_gate} ({gate_stats['effective_support']})"
        )
        return out
    if (gate_stats["_wilson_low_raw"] or 0.0) < tgt:
        out["fail_reason"] = (
            f"gate_wilson_low<{tgt:.2f} ({gate_stats['wilson_low']})"
        )
        return out
    if (gate_stats["_effective_wilson_low_raw"] or 0.0) < tgt:
        out["fail_reason"] = (
            f"gate_effective_wilson_low<{tgt:.2f} "
            f"({gate_stats['effective_wilson_low']})"
        )
        return out
    out["ok"] = True
    return out


# ------------------------------------------------------------------ global pool

def _global_min_support() -> int:
    return max(1, int(os.getenv("V3_PRECISION_GLOBAL_MIN_SUPPORT", "30")))


def global_fallback_enabled() -> bool:
    return (os.getenv("V3_PRECISION_GLOBAL", "on") or "on").strip().lower() not in (
        "0", "off", "false", "no",
    )


_GLOBAL_PROOF_SCHEMA = "chronological_embargo_effective_sessions_v3"
_GLOBAL_STATE_KEY = "v3_global_fire_threshold_v3"
_GLOBAL_CACHE: Dict[str, Any] = {"ts": 0.0, "val": None}
_GLOBAL_CACHE_TTL_S = 300
_GLOBAL_CACHE_LOCK = threading.Lock()


def _required_global_embargo_seconds() -> int:
    """Conservative calendar embargo derived from the active daily-bar horizon.

    Each forward trading bar is budgeted three calendar days (weekends and
    common exchange holidays). Operators may only tighten this via env; a lower
    override cannot weaken the label-horizon floor.
    """
    from core.engine_config import V3_LABEL_HOLD_BARS
    derived = max(1, int(V3_LABEL_HOLD_BARS)) * 3 * 86400
    try:
        configured = int(os.getenv("V3_GLOBAL_PROOF_EMBARGO_SECONDS", str(derived)))
    except (TypeError, ValueError):
        configured = derived
    return max(derived, configured)


def select_global_threshold(
    probs: Sequence[float],
    labels: Sequence[int],
    target: Optional[float] = None,
    *,
    timestamps: Optional[Sequence[int]] = None,
    session_timestamps: Optional[Sequence[int]] = None,
    embargo_seconds: int = 0,
) -> Dict[str, Any]:
    """Select on older timestamp groups and certify on an embargoed newer set.

    New live proof requires valid timestamps. The split never divides equal
    timestamps, and validation begins strictly after the requested embargo.
    """
    tgt = precision_target() if target is None else float(target)
    n = int(len(probs))
    out: Dict[str, Any] = {
        "ok": False, "target": round(tgt, 4), "pool_n": n,
        "proof_schema": _GLOBAL_PROOF_SCHEMA,
    }
    if timestamps is None or n == 0:
        out["fail_reason"] = "pooled_timestamps_required"
        return out
    if session_timestamps is None:
        out["fail_reason"] = "pooled_session_timestamps_required"
        return out
    if len(labels) != n or len(timestamps) != n or len(session_timestamps) != n:
        out["fail_reason"] = "pooled_record_lengths_mismatch"
        return out
    records = []
    try:
        for i in range(n):
            ts_value = timestamps[i]
            if isinstance(ts_value, bool):
                raise ValueError("invalid pooled timestamp")
            ts_raw = float(ts_value)
            prob = float(probs[i])
            label_raw = labels[i]
            if (
                not math.isfinite(ts_raw) or ts_raw <= 0 or not ts_raw.is_integer()
                or not math.isfinite(prob) or not 0.0 <= prob <= 1.0
                or label_raw not in (0, 1, False, True)
            ):
                raise ValueError("invalid pooled record")
            session_value = session_timestamps[i]
            if isinstance(session_value, bool):
                raise ValueError("invalid pooled session timestamp")
            session_raw = float(session_value)
            if (
                not math.isfinite(session_raw) or session_raw <= 0
                or not session_raw.is_integer()
            ):
                raise ValueError("invalid pooled session timestamp")
            records.append((int(ts_raw), int(session_raw), prob, int(label_raw)))
    except (TypeError, ValueError, OverflowError):
        out["fail_reason"] = "pooled_timestamps_invalid"
        return out
    records.sort(key=lambda row: row[0])
    min_support = _global_min_support()
    if n < 2 * min_support:
        out["fail_reason"] = f"pooled_split_support<{min_support}"
        return out
    unique_ts = sorted({row[0] for row in records})
    if len(unique_ts) < 2:
        out["fail_reason"] = "pooled_distinct_timestamps<2"
        return out
    split_ts = unique_ts[len(unique_ts) // 2]
    selection = [row for row in records if row[0] < split_ts]
    validation = [
        row for row in records
        if row[0] >= split_ts and row[0] > selection[-1][0] + max(0, int(embargo_seconds))
    ] if selection else []
    if len(selection) < min_support or len(validation) < min_support:
        out["fail_reason"] = f"pooled_split_support<{min_support}"
        return out
    candidate = threshold_search(
        [r[2] for r in selection], [r[3] for r in selection], tgt, min_support,
    )
    if candidate is None:
        out["fail_reason"] = "no_pooled_selection_operating_point"
        return out
    threshold = float(candidate["threshold"])
    validation = _slice_stats(
        [r[2] for r in validation], [r[3] for r in validation], threshold,
        session_timestamps=[r[1] for r in validation],
    )
    out["selection"] = candidate
    out["validation"] = validation
    out["threshold"] = threshold
    if validation["support"] < min_support:
        out["fail_reason"] = f"pooled_validation_support<{min_support} ({validation['support']})"
        return out
    if validation.get("effective_support", 0) < min_support:
        out["fail_reason"] = (
            f"pooled_validation_effective_support<{min_support} "
            f"({validation.get('effective_support', 0)})"
        )
        return out
    if (validation["_wilson_low_raw"] or 0.0) < tgt:
        out["fail_reason"] = (
            f"pooled_validation_wilson_low<{tgt:.2f} ({validation['wilson_low']})"
        )
        return out
    if (validation.get("_effective_wilson_low_raw") or 0.0) < tgt:
        out["fail_reason"] = (
            f"pooled_validation_effective_wilson_low<{tgt:.2f} "
            f"({validation.get('effective_wilson_low')})"
        )
        return out
    out.update({
        "precision": validation["precision"],
        "support": validation["support"],
        "wins": validation["wins"],
        "wilson_low": validation["wilson_low"],
        "distinct_sessions": validation["distinct_sessions"],
        "hold_bars": validation["hold_bars"],
        "effective_support": validation["effective_support"],
        "effective_wins": validation["effective_wins"],
        "effective_wilson_low": validation["effective_wilson_low"],
        "ok": True,
    })
    return out


def store_global_thresholds(
    pools: Dict[str, Dict[str, Sequence]], *, validation_schema: str,
    label_schema: str, feature_schema: str,
) -> Dict[str, Any]:
    """Compute + persist pooled thresholds from one exact model generation.

    Each chronological record is ``(resolved_ts, session_ts, probability,
    label, model_sha)``.
    Proof is bound to the active semantic schemas and to the participating model
    hashes, so a replacement/non-participant cannot inherit stale evidence.
    """
    import json as _json
    import time as _time
    from core.engine_config import V3_LABEL_HOLD_BARS
    result: Dict[str, Any] = {
        "ts": int(_time.time()), "proof_schema": _GLOBAL_PROOF_SCHEMA,
        "target": round(precision_target(), 4),
        "validation_schema": str(validation_schema),
        "label_schema": str(label_schema),
        "feature_schema": str(feature_schema),
        "label_hold_bars": int(V3_LABEL_HOLD_BARS),
    }
    embargo_seconds = _required_global_embargo_seconds()
    result["embargo_seconds"] = embargo_seconds
    for direction in ("UP", "DOWN"):
        pool = pools.get(direction) or {}
        raw_records = pool.get("records") or []
        valid_shape = bool(raw_records) and all(
            isinstance(row, (list, tuple)) and len(row) == 5
            for row in raw_records
        )
        records = sorted(raw_records, key=lambda row: row[0]) if valid_shape else []
        probs = [row[2] for row in records]
        labels = [row[3] for row in records]
        timestamps = [row[0] for row in records]
        session_timestamps = [row[1] for row in records]
        model_hashes = sorted({str(row[4]) for row in records if str(row[4])})
        entry = select_global_threshold(
            probs, labels, timestamps=timestamps,
            session_timestamps=session_timestamps,
            embargo_seconds=embargo_seconds,
        )
        entry["model_sha256s"] = model_hashes
        if not valid_shape:
            entry["ok"] = False
            entry["fail_reason"] = "pooled_record_shape_invalid"
        result[direction] = entry
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_ghost_state(cur)
            cur.execute(
                "INSERT INTO ghost_state (key, val) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET val = EXCLUDED.val",
                (_GLOBAL_STATE_KEY, _json.dumps(result)),
            )
        with _GLOBAL_CACHE_LOCK:
            _GLOBAL_CACHE["ts"] = 0.0
            _GLOBAL_CACHE["val"] = None
        LOGGER.info(
            "precision_gate global thresholds stored: UP=%s DOWN=%s",
            (result["UP"].get("threshold") if result["UP"].get("ok") else "unproven"),
            (result["DOWN"].get("threshold") if result["DOWN"].get("ok") else "unproven"),
        )
    except Exception as e:
        LOGGER.warning("precision_gate global threshold store failed: %s", str(e)[:120])
        result["store_error"] = str(e)[:120]
    return result


def load_global_threshold(
    direction: str, *, model_sha256: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Read pooled proof only for a participating current model generation."""
    import json as _json
    import time as _time
    with _GLOBAL_CACHE_LOCK:
        if _GLOBAL_CACHE["val"] is not None and (_time.time() - _GLOBAL_CACHE["ts"]) < _GLOBAL_CACHE_TTL_S:
            blob = _GLOBAL_CACHE["val"]
        else:
            blob = None
    if blob is None:
        try:
            from core.db import db_conn
            with db_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT val FROM ghost_state WHERE key = %s", (_GLOBAL_STATE_KEY,))
                row = cur.fetchone()
            blob = _json.loads(row[0]) if row and row[0] else {}
        except Exception as e:
            LOGGER.debug("precision_gate global threshold load failed: %s", str(e)[:120])
            return None
        with _GLOBAL_CACHE_LOCK:
            _GLOBAL_CACHE["ts"] = _time.time()
            _GLOBAL_CACHE["val"] = blob
    from core.engine_config import V3_LABEL_HOLD_BARS, _v3_feature_schema
    from core.signal_engine import _v3_label_schema, _v3_validation_schema
    required_embargo = _required_global_embargo_seconds()
    try:
        if not isinstance(blob, dict):
            return None
        stored_target = float(blob.get("target"))
        stored_embargo = _exact_int(blob.get("embargo_seconds"))
        stored_hold = _exact_int(blob.get("label_hold_bars"))
        current_target = precision_target()
        if not math.isfinite(stored_target):
            return None
        if (
            blob.get("proof_schema") != _GLOBAL_PROOF_SCHEMA
            or stored_target < current_target
            or stored_embargo is None or stored_embargo < required_embargo
            or blob.get("validation_schema") != _v3_validation_schema()
            or blob.get("label_schema") != _v3_label_schema()
            or blob.get("feature_schema") != _v3_feature_schema()
            or stored_hold != int(V3_LABEL_HOLD_BARS)
        ):
            return None
        entry = blob.get((direction or "").upper())
        if not isinstance(entry, dict):
            return None
        member_hashes = entry.get("model_sha256s")
        threshold = float(entry.get("threshold"))
        entry_target = float(entry.get("target"))
        support = _exact_int(entry.get("support"))
        wins = _exact_int(entry.get("wins"))
        distinct_sessions = _exact_int(entry.get("distinct_sessions"))
        hold_bars = _exact_int(entry.get("hold_bars"))
        effective_support = _exact_int(entry.get("effective_support"))
        effective_wins = _exact_int(entry.get("effective_wins"))
        if (
            entry.get("ok") is not True
            or entry.get("proof_schema") != _GLOBAL_PROOF_SCHEMA
            or not model_sha256
            or not isinstance(member_hashes, list)
            or str(model_sha256) not in member_hashes
            or not all(math.isfinite(v) for v in (threshold, entry_target))
            or not 0.0 <= threshold <= 1.0
            or entry_target != stored_target
            or entry_target < current_target
            or support is None or wins is None
            or distinct_sessions is None or hold_bars is None
            or effective_support is None or effective_wins is None
            or support < _global_min_support()
            or wins < 0 or wins > support
        ):
            return None
        required_target = max(current_target, stored_target, entry_target)
        raw_wilson = wilson_lower_bound(wins, support)
        if raw_wilson + 1e-12 < required_target:
            return None
        current_hold = _hold_bars()
        if (
            hold_bars != current_hold
            or not 1 <= distinct_sessions <= support
            or effective_support != max(1, distinct_sessions // current_hold)
            or effective_support < _global_min_support()
            or effective_wins != int(math.floor(
                (wins / support) * effective_support + 1e-12,
            ))
            or wilson_lower_bound(effective_wins, effective_support) + 1e-12
            < required_target
        ):
            return None
        # Rounded persisted Wilson telemetry is not proof authority. Return a
        # normalized copy so downstream diagnostics cannot repeat stale data.
        validated = dict(entry)
        validated["wilson_low"] = round(raw_wilson, 4)
        validated["effective_wilson_low"] = round(
            wilson_lower_bound(effective_wins, effective_support), 4,
        )
        return validated
    except (TypeError, ValueError, OverflowError):
        return None


def invalidate_global_threshold_persistent(cur) -> None:
    """Delete pooled proof using the caller's model-mutation transaction."""
    ensure_ghost_state(cur)
    cur.execute("DELETE FROM ghost_state WHERE key = %s", (_GLOBAL_STATE_KEY,))


def invalidate_global_threshold_cache() -> None:
    """Clear process-local proof only after its DB transaction committed."""
    with _GLOBAL_CACHE_LOCK:
        _GLOBAL_CACHE["ts"] = 0.0
        _GLOBAL_CACHE["val"] = None
