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


def symbol_review(
    symbol: str, *, direction: str, model_sha256: str, feature_schema: str,
    label_schema: str, validation_schema: str, hold_bars: int,
) -> Dict[str, Any]:
    """Review only evidence from the exact current lane/model generation."""
    if not enabled():
        return {"ok": True, "disabled": True, "symbol": (symbol or "").upper()}
    from core.db import db_conn
    sym = (symbol or "").upper()
    lane = (direction or "").upper()
    try:
        horizon = int(hold_bars)
    except (TypeError, ValueError, OverflowError):
        horizon = 0
    if lane not in ("UP", "DOWN") or horizon < 1 or not all((
        model_sha256, feature_schema, label_schema, validation_schema,
    )):
        return {"ok": False, "symbol": sym, "fail_reason": "skill_identity_missing"}
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT
                  SUM(CASE WHEN outcome IN ('WIN','LOSS','EXPIRED') THEN 1 ELSE 0 END) AS resolved,
                  SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) AS wins,
                  AVG(CASE WHEN outcome IN ('WIN','LOSS','EXPIRED') THEN pnl_pct ELSE NULL END) AS avg_pnl
                FROM ghost_shadow_outcomes
                WHERE symbol=%s AND direction=%s AND model_sha256=%s
                  AND feature_schema=%s AND label_schema=%s
                  AND validation_schema=%s AND hold_bars=%s
                  AND outcome IN ('WIN','LOSS','EXPIRED')
                """,
                (sym, lane, model_sha256, feature_schema, label_schema,
                 validation_schema, horizon),
            )
            row = cur.fetchone()
    except Exception as exc:
        return {"ok": False, "symbol": sym, "fail_reason": "skill_unavailable", "error": str(exc)[:120]}
    return review(sym, resolved=int((row and row[0]) or 0), wins=int((row and row[1]) or 0), avg_pnl_pct=(row[2] if row else None))


def overconfidence_threshold() -> float:
    return max(0.5, min(0.99, float(os.getenv("V3_OVERCONFIDENCE_PROB_THRESHOLD", "0.70"))))


def overconfidence_enabled() -> bool:
    return (os.getenv("V3_OVERCONFIDENCE_GATE", "on") or "on").strip().lower() not in (
        "0", "off", "false", "no",
    )


def overconfidence_min_samples() -> int:
    return max(1, int(os.getenv("V3_OVERCONFIDENCE_MIN_SAMPLES", "20")))


def overconfidence_min_win_rate() -> float:
    """Minimum realized win rate required for the 70+ high-confidence bucket.

    This is part of the accuracy contract: in contract=70 the bucket must clear
    the 70% win test, and env may only TIGHTEN it. Before PR #133 follow-up it
    defaulted to 55%, which could allow a 70+ confidence bucket that was merely
    better-than-coin-flip, not actually contract-70.
    """
    from core.accuracy_contract import resolve_float
    return resolve_float("V3_OVERCONFIDENCE_MIN_WIN_RATE", "target_win_rate", lo=0.0, hi=1.0)


def calibration_review(*, prob: float, samples: int, wins: int) -> Dict[str, Any]:
    """Pure global high-probability calibration blocker.

    If the current probability is in a bucket that Watcher has shown is inverted
    (enough resolved samples but realized win rate below the floor), block the
    fire. This addresses the live finding: 70+ bucket mean up_prob ~0.79 but only
    40% realized wins (N=25). It only tightens otherwise-fireable picks.
    """
    p = float(prob or 0.0)
    thr = overconfidence_threshold()
    min_n = overconfidence_min_samples()
    min_wr = overconfidence_min_win_rate()
    out = {"ok": True, "prob": round(p, 4), "threshold": thr,
           "samples": int(samples or 0), "wins": int(wins or 0),
           "min_samples": min_n, "min_win_rate": min_wr}
    if p < thr:
        out["not_applicable"] = True
        return out
    n = int(samples or 0)
    w = int(wins or 0)
    wr = (w / n) if n else None
    out["win_rate"] = round(wr, 4) if wr is not None else None
    if n < min_n:
        out["ok"] = False
        out["fail_reason"] = f"high_prob_bucket_n<{min_n} ({n})"
        return out
    if wr is None or wr < min_wr:
        out["ok"] = False
        out["fail_reason"] = f"high_prob_bucket_wr<{min_wr:.2f} ({wr or 0:.4f})"
        return out
    return out


def global_calibration_review(
    prob: float, *, direction: str, model_sha256: str, feature_schema: str,
    label_schema: str, validation_schema: str, hold_bars: int,
) -> Dict[str, Any]:
    """Read high-probability evidence for the exact current model identity."""
    p = float(prob or 0.0)
    if not overconfidence_enabled():
        return {"ok": True, "disabled": True, "prob": round(p, 4)}
    thr = overconfidence_threshold()
    if p < thr:
        return calibration_review(prob=p, samples=0, wins=0)
    lane = (direction or "").upper()
    try:
        horizon = int(hold_bars)
    except (TypeError, ValueError, OverflowError):
        horizon = 0
    if lane not in ("UP", "DOWN") or horizon < 1 or not all((
        model_sha256, feature_schema, label_schema, validation_schema,
    )):
        return {"ok": False, "prob": round(p, 4), "fail_reason": "calibration_identity_missing"}
    from core.db import db_conn
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COUNT(*) AS samples,
                       SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) AS wins
                FROM ghost_shadow_outcomes
                WHERE outcome IN ('WIN','LOSS','EXPIRED') AND model_prob >= %s
                  AND direction=%s AND model_sha256=%s AND feature_schema=%s
                  AND label_schema=%s AND validation_schema=%s AND hold_bars=%s
                """,
                (thr, lane, model_sha256, feature_schema, label_schema,
                 validation_schema, horizon),
            )
            row = cur.fetchone()
    except Exception as exc:
        return {"ok": False, "prob": round(p, 4), "threshold": thr,
                "fail_reason": "calibration_unavailable", "error": str(exc)[:120]}
    return calibration_review(prob=p, samples=int((row and row[0]) or 0), wins=int((row and row[1]) or 0))
