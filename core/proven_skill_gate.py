"""core/proven_skill_gate.py — live-fire proven-skill blocker (PR #155).

Tightens real firing only. A symbol may have a calibrated/probability-valid model
and still be bad in forward shadow outcomes (GME/NOK/XPO class). This gate checks
resolved, real forward shadow outcomes before allowing a live fire. It never
loosens an existing gate and never changes research/shadow/wallet scoring.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional


def enabled() -> bool:
    return (os.getenv("V3_PROVEN_SKILL_GATE", "on") or "on").strip().lower() not in ("0", "off", "false", "no")


def min_resolved() -> int:
    return max(1, int(os.getenv("V3_PROVEN_SKILL_MIN_RESOLVED", "10")))


def min_tp_rate() -> float:
    return max(0.0, min(1.0, float(os.getenv("V3_PROVEN_SKILL_MIN_TP_RATE", "0.55"))))


def min_avg_pnl_pct() -> float:
    return float(os.getenv("V3_PROVEN_SKILL_MIN_AVG_PNL_PCT", "0.0"))


def review(symbol: str, *, resolved: int, wins: int, avg_pnl_pct: Optional[float]) -> Dict[str, Any]:
    """Pure proven-skill decision for a symbol's resolved shadow record."""
    sym = (symbol or "").upper()
    resolved = int(resolved or 0)
    wins = int(wins or 0)
    tp_rate = (wins / resolved) if resolved > 0 else None
    avg = float(avg_pnl_pct) if avg_pnl_pct is not None else None
    req_n = min_resolved()
    req_tp = min_tp_rate()
    req_avg = min_avg_pnl_pct()
    out = {
        "ok": False,
        "symbol": sym,
        "resolved": resolved,
        "wins": wins,
        "tp_rate": round(tp_rate, 4) if tp_rate is not None else None,
        "avg_pnl_pct": round(avg, 4) if avg is not None else None,
        "requirements": {"min_resolved": req_n, "min_tp_rate": req_tp, "min_avg_pnl_pct": req_avg},
    }
    if resolved < req_n:
        out["fail_reason"] = f"resolved<{req_n} ({resolved})"
        return out
    if tp_rate is None or tp_rate < req_tp:
        out["fail_reason"] = f"tp_rate<{req_tp:.2f} ({tp_rate or 0:.4f})"
        return out
    if avg is None or avg < req_avg:
        out["fail_reason"] = f"avg_pnl_pct<{req_avg:.2f} ({avg if avg is not None else 'none'})"
        return out
    out["ok"] = True
    return out


def symbol_review(symbol: str) -> Dict[str, Any]:
    """Read the live shadow-outcome track record for one symbol and decide."""
    if not enabled():
        return {"ok": True, "disabled": True, "symbol": (symbol or "").upper()}
    from core.db import db_conn
    sym = (symbol or "").upper()
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT
                  SUM(CASE WHEN outcome IN ('WIN','LOSS') THEN 1 ELSE 0 END) AS resolved,
                  SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) AS wins,
                  AVG(CASE WHEN outcome IN ('WIN','LOSS') THEN pnl_pct ELSE NULL END) AS avg_pnl
                FROM ghost_shadow_outcomes
                WHERE symbol=%s AND outcome IS NOT NULL
                """,
                (sym,),
            )
            row = cur.fetchone()
    except Exception as exc:
        return {"ok": False, "symbol": sym, "fail_reason": "skill_unavailable", "error": str(exc)[:120]}
    return review(sym, resolved=int((row and row[0]) or 0), wins=int((row and row[1]) or 0), avg_pnl_pct=(row[2] if row else None))
