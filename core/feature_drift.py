"""Feature drift monitor — compare recent vs baseline feature snapshots (Phase 2)."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

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
    if len(payloads) < max(6, window):
        return {
            "ok": True,
            "enabled": True,
            "symbol": symbol.upper(),
            "samples": len(payloads),
            "status": "insufficient_samples",
            "alerts": [],
        }

    recent = payloads[:window]
    baseline = payloads[window : window * 2] or payloads[window:]
    alerts: List[Dict[str, Any]] = []

    for key in _TRACKED:
        r_vals = [float(p[key]) for p in recent if isinstance(p, dict) and p.get(key) is not None]
        b_vals = [float(p[key]) for p in baseline if isinstance(p, dict) and p.get(key) is not None]
        if len(r_vals) < 3 or len(b_vals) < 3:
            continue
        r_mean = sum(r_vals) / len(r_vals)
        b_mean = sum(b_vals) / len(b_vals)
        b_std = (sum((x - b_mean) ** 2 for x in b_vals) / len(b_vals)) ** 0.5
        if b_std <= 1e-9:
            continue
        z = abs(r_mean - b_mean) / b_std
        if z >= float(os.getenv("GHOST_DRIFT_Z_ALERT", "2.0")):
            alerts.append({
                "feature": key,
                "z_shift": round(z, 2),
                "recent_mean": round(r_mean, 4),
                "baseline_mean": round(b_mean, 4),
            })

    status = "alert" if alerts else "stable"
    return {
        "ok": True,
        "enabled": True,
        "symbol": symbol.upper(),
        "samples": len(payloads),
        "status": status,
        "alerts": alerts,
    }
