"""Feature drift monitor — compare recent vs baseline feature snapshots (Phase 2)."""
from __future__ import annotations

import math
import os
from typing import Any, Dict, List, Optional

_MIN_FEATURE_SUPPORT = 5
_TRACKED = ("rsi", "macd_hist", "pct_b", "volume_ratio", "mom_4h", "atr_pct", "adx")


def _enabled() -> bool:
    return os.getenv("GHOST_FEATURE_DRIFT", "1").strip().lower() in ("1", "true", "yes", "on")


def compute_drift(symbol: str = "WOLF", *, window: int = 14) -> Dict[str, Any]:
    """PSI-like z-shift on journaled features from ghost_feature_snapshots if present."""
    if not _enabled():
        return {"ok": True, "enabled": False, "alerts": []}
    try:
        from core.db import db_conn

        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT payload FROM ghost_feature_snapshots
                WHERE symbol = %s AND payload IS NOT NULL
                ORDER BY feature_asof_ts DESC
                LIMIT %s
                """,
                (symbol.upper(), max(window * 3, 30)),
            )
            rows = cur.fetchall()
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160], "alerts": []}

    payloads = [r[0] for r in rows if r and r[0]]
    window = max(1, int(window))
    min_support = max(_MIN_FEATURE_SUPPORT, window // 2)
    if len(payloads) < max(6, window * 2):
        # Need a full recent window *and* a full, non-overlapping baseline.
        return {
            "ok": True,
            "enabled": True,
            "symbol": symbol.upper(),
            "samples": len(payloads),
            "required_samples": max(6, window * 2),
            "status": "insufficient_data",
            "alerts": [],
        }

    recent = payloads[:window]
    baseline = payloads[window : window * 2]
    alerts: List[Dict[str, Any]] = []
    evaluated: List[str] = []
    insufficient: List[str] = []
    z_alert = float(os.getenv("GHOST_DRIFT_Z_ALERT", "2.0"))

    for key in _TRACKED:
        r_vals = _finite_values(recent, key)
        b_vals = _finite_values(baseline, key)
        if len(r_vals) < min_support or len(b_vals) < min_support:
            insufficient.append(key)
            continue
        evaluated.append(key)
        r_mean = sum(r_vals) / len(r_vals)
        b_mean = sum(b_vals) / len(b_vals)
        b_std = (sum((x - b_mean) ** 2 for x in b_vals) / len(b_vals)) ** 0.5
        if b_std <= 1e-9:
            # Constant baseline: any move off the constant is a shift with an
            # undefined (infinite) z-score; no move is stable.
            if abs(r_mean - b_mean) > 1e-9:
                alerts.append({
                    "feature": key,
                    "z_shift": None,
                    "reason": "zero_variance_baseline_shift",
                    "recent_mean": round(r_mean, 4),
                    "baseline_mean": round(b_mean, 4),
                })
            continue
        z = abs(r_mean - b_mean) / b_std
        if z >= z_alert:
            alerts.append({
                "feature": key,
                "z_shift": round(z, 2),
                "recent_mean": round(r_mean, 4),
                "baseline_mean": round(b_mean, 4),
            })

    if alerts:
        status = "alert"
    elif not evaluated:
        status = "insufficient_data"
    else:
        status = "stable"
    return {
        "ok": True,
        "enabled": True,
        "symbol": symbol.upper(),
        "samples": len(payloads),
        "status": status,
        "features_evaluated": evaluated,
        "features_insufficient": insufficient,
        "min_feature_support": min_support,
        "alerts": alerts,
    }


def _finite_values(payloads: List[Any], key: str) -> List[float]:
    values: List[float] = []
    for payload in payloads:
        if not isinstance(payload, dict) or payload.get(key) is None:
            continue
        try:
            value = float(payload[key])
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            values.append(value)
    return values
