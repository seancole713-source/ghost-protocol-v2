"""Super Ghost Promotion Gate (PR #99).

Turns shadow/lab evidence into conservative promotion decisions.

Ghost now has:
- Truth Ledger outcomes
- Learning Brain
- Champion/Challenger Lab
- Feature Attribution Memory
- Shadow Model Runner

This module answers: when is a challenger proven enough to deserve more trust?

It does NOT auto-promote, auto-trade, or mutate production model weights. It
creates a durable promotion review: PROMOTE_CANDIDATE / KEEP_CHAMPION /
KEEP_SHADOWING / RETIRE_CANDIDATE / INSUFFICIENT_EVIDENCE.
"""
from __future__ import annotations

import json
import logging
import math
import time
from typing import Any, Dict, List, Optional, Tuple

LOGGER = logging.getLogger("ghost.super_ghost_promotion")

DECISIONS = {
    "PROMOTE_CANDIDATE",
    "KEEP_CHAMPION",
    "KEEP_SHADOWING",
    "RETIRE_CANDIDATE",
    "INSUFFICIENT_EVIDENCE",
}

DEFAULT_REQUIREMENTS = {
    "min_resolved_rows": 50,
    "min_actionable_calls": 15,
    "min_profit_factor": 1.25,
    "min_win_rate_delta": 0.05,
    "min_expected_value_delta_pct": 0.20,
    "max_false_positive_rate": 0.42,
    "max_drawdown_worse_by_pct": 2.0,
}


def _now() -> int:
    return int(time.time())


def _f(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        out = float(v)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _jsonb(v: Any) -> str:
    return json.dumps(v, default=str)


def _coerce_json(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return v


def ensure_promotion_tables(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS super_ghost_promotion_reviews (
            id SERIAL PRIMARY KEY,
            created_at BIGINT NOT NULL,
            candidate_id VARCHAR(120) NOT NULL,
            candidate_type VARCHAR(40) NOT NULL,
            champion_id VARCHAR(120),
            symbol VARCHAR(20),
            horizon_days INT NOT NULL,
            sample_count INT NOT NULL,
            actionable_count INT NOT NULL,
            candidate_metrics_json JSONB,
            champion_metrics_json JSONB,
            requirements_json JSONB,
            decision VARCHAR(40) NOT NULL,
            reason TEXT,
            approved_for_promotion BOOLEAN NOT NULL DEFAULT FALSE,
            requires_more_shadowing BOOLEAN NOT NULL DEFAULT TRUE,
            created_by VARCHAR(80) DEFAULT 'ghost_promotion_gate'
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sg_promotion_time ON super_ghost_promotion_reviews (created_at DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sg_promotion_candidate ON super_ghost_promotion_reviews (candidate_id, horizon_days, created_at DESC)")


def _metric(metrics: Dict[str, Any], *names: str) -> Optional[float]:
    for n in names:
        if n in metrics:
            return _f(metrics.get(n))
    return None


def normalize_metrics(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize lab/shadow metrics to a common schema."""
    m = dict(raw or {})
    sample_count = int(m.get("rows_evaluated") or m.get("sample_count") or 0)
    actionable = int(m.get("actionable_count") or 0)
    wins = int(m.get("wins") or 0)
    losses = int(m.get("losses") or 0)
    win_rate = _metric(m, "win_rate")
    if win_rate is None and actionable:
        win_rate = wins / actionable
    fpr = _metric(m, "false_positive_rate")
    if fpr is None and actionable:
        fpr = losses / actionable
    avg_return = _metric(m, "avg_signed_return_pct", "avg_return_pct")
    net_return = _metric(m, "net_return_pct")
    profit_factor = _metric(m, "profit_factor")
    max_dd = _metric(m, "max_drawdown_pct")
    score = _metric(m, "score")
    return {
        "id": m.get("candidate") or m.get("model_id") or m.get("candidate_id") or "unknown",
        "sample_count": sample_count,
        "actionable_count": actionable,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "false_positive_rate": fpr,
        "avg_signed_return_pct": avg_return,
        "net_return_pct": net_return,
        "profit_factor": profit_factor,
        "max_drawdown_pct": max_dd,
        "score": score,
        "raw": m,
    }


def review_promotion(
    candidate_metrics: Dict[str, Any],
    champion_metrics: Optional[Dict[str, Any]] = None,
    *,
    requirements: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Pure promotion decision gate.

    Conservative by design: no promotion unless enough rows, enough actionable
    calls, profit factor, false-positive, drawdown, win-rate delta, and expected
    value delta clear. If evidence is thin: KEEP_SHADOWING / INSUFFICIENT.
    """
    req = dict(DEFAULT_REQUIREMENTS)
    if requirements:
        req.update({k: v for k, v in requirements.items() if v is not None})
    cand = normalize_metrics(candidate_metrics)
    champ = normalize_metrics(champion_metrics or {}) if champion_metrics else None

    reasons: List[str] = []
    sample_count = cand["sample_count"]
    actionable = cand["actionable_count"]

    if sample_count < int(req["min_resolved_rows"]):
        return {
            "ok": True,
            "decision": "INSUFFICIENT_EVIDENCE",
            "approved_for_promotion": False,
            "requires_more_shadowing": True,
            "reason": f"Need at least {req['min_resolved_rows']} resolved rows; candidate has {sample_count}.",
            "candidate_metrics": cand,
            "champion_metrics": champ,
            "requirements": req,
        }
    if actionable < int(req["min_actionable_calls"]):
        return {
            "ok": True,
            "decision": "KEEP_SHADOWING",
            "approved_for_promotion": False,
            "requires_more_shadowing": True,
            "reason": f"Need at least {req['min_actionable_calls']} actionable calls; candidate has {actionable}.",
            "candidate_metrics": cand,
            "champion_metrics": champ,
            "requirements": req,
        }

    wr = cand.get("win_rate")
    pf = cand.get("profit_factor")
    fpr = cand.get("false_positive_rate")
    avg = cand.get("avg_signed_return_pct")
    dd = cand.get("max_drawdown_pct") or 0.0

    # Absolute safety gates first.
    if pf is not None and pf < float(req["min_profit_factor"]):
        reasons.append(f"profit factor {pf:.2f} below {req['min_profit_factor']}")
    if fpr is not None and fpr > float(req["max_false_positive_rate"]):
        reasons.append(f"false-positive rate {fpr:.2%} above {float(req['max_false_positive_rate']):.0%}")
    if avg is not None and avg <= 0:
        reasons.append(f"expected value {avg:.2f}% is not positive")

    if reasons:
        # If the candidate is actively poor with enough sample, retire; otherwise keep shadowing.
        decision = "RETIRE_CANDIDATE" if (wr is not None and wr < 0.40 and actionable >= int(req["min_actionable_calls"])) else "KEEP_SHADOWING"
        return {
            "ok": True,
            "decision": decision,
            "approved_for_promotion": False,
            "requires_more_shadowing": decision != "RETIRE_CANDIDATE",
            "reason": "; ".join(reasons),
            "candidate_metrics": cand,
            "champion_metrics": champ,
            "requirements": req,
        }

    if champ and champ.get("sample_count", 0) > 0:
        champ_wr = champ.get("win_rate") or 0.0
        champ_avg = champ.get("avg_signed_return_pct") or 0.0
        champ_dd = champ.get("max_drawdown_pct") or 0.0
        wr_delta = (wr or 0.0) - champ_wr
        ev_delta = (avg or 0.0) - champ_avg
        dd_worse = (dd or 0.0) - (champ_dd or 0.0)
        if wr_delta < float(req["min_win_rate_delta"]):
            reasons.append(f"win-rate delta {wr_delta:.2%} below {float(req['min_win_rate_delta']):.0%}")
        if ev_delta < float(req["min_expected_value_delta_pct"]):
            reasons.append(f"expected-value delta {ev_delta:.2f}% below {req['min_expected_value_delta_pct']}%")
        if dd_worse > float(req["max_drawdown_worse_by_pct"]):
            reasons.append(f"drawdown worse by {dd_worse:.2f}%")
        if reasons:
            return {
                "ok": True,
                "decision": "KEEP_CHAMPION",
                "approved_for_promotion": False,
                "requires_more_shadowing": True,
                "reason": "; ".join(reasons),
                "candidate_metrics": cand,
                "champion_metrics": champ,
                "requirements": req,
            }

    return {
        "ok": True,
        "decision": "PROMOTE_CANDIDATE",
        "approved_for_promotion": True,
        "requires_more_shadowing": False,
        "reason": "Candidate cleared sample, actionability, risk, profit-factor, and improvement gates. Human review still required before production change.",
        "candidate_metrics": cand,
        "champion_metrics": champ,
        "requirements": req,
    }


def _best_candidate_from_lab(lab: Dict[str, Any], candidate_id: Optional[str]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    results = list(lab.get("results") or [])
    if not results:
        return None, None
    champion = next((r for r in results if r.get("candidate") == "production_champion"), None)
    if candidate_id:
        cand = next((r for r in results if r.get("candidate") == candidate_id), None)
    else:
        non_champ = [r for r in results if r.get("candidate") != "production_champion"]
        cand = max(non_champ, key=lambda r: (r.get("score") is not None, r.get("score") or -999), default=None)
    return cand, champion


def run_promotion_review(*, symbol: Optional[str] = None, horizon: int = 5, candidate_id: Optional[str] = None, source: str = "lab", persist: bool = True) -> Dict[str, Any]:
    """Run and optionally persist a promotion review."""
    src = (source or "lab").lower()
    candidate_type = "lab_policy" if src == "lab" else "shadow_model"
    champion = None
    candidate = None
    evidence = None
    if src == "shadow":
        try:
            from core.super_ghost_shadow import shadow_model_profiles
            evidence = shadow_model_profiles()
            profiles = list(evidence.get("profiles") or [])
            if candidate_id:
                candidate = next((p for p in profiles if p.get("model_id") == candidate_id), None)
            else:
                candidate = max(profiles, key=lambda p: (p.get("avg_signed_return_pct") is not None, p.get("avg_signed_return_pct") or -999), default=None)
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:160]}
    else:
        try:
            from core.super_ghost_lab import latest_lab_summary
            evidence = latest_lab_summary(symbol=symbol, horizon=horizon)
            candidate, champion = _best_candidate_from_lab(evidence, candidate_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:160]}

    if not candidate:
        review = {
            "ok": True,
            "decision": "INSUFFICIENT_EVIDENCE",
            "approved_for_promotion": False,
            "requires_more_shadowing": True,
            "reason": "No candidate metrics available yet.",
            "candidate_metrics": None,
            "champion_metrics": champion,
            "requirements": DEFAULT_REQUIREMENTS,
        }
    else:
        review = review_promotion(candidate, champion)
    review.update({
        "source": src,
        "candidate_type": candidate_type,
        "candidate_id": (candidate or {}).get("candidate") or (candidate or {}).get("model_id") or candidate_id,
        "champion_id": (champion or {}).get("candidate") or (champion or {}).get("model_id") or ("production_champion" if champion else None),
        "symbol": (symbol or "ALL").upper(),
        "horizon_days": horizon,
        "evidence_available": bool(evidence and evidence.get("ok")),
    })

    if persist:
        try:
            from core.db import db_conn
            with db_conn() as conn:
                cur = conn.cursor()
                ensure_promotion_tables(cur)
                cur.execute(
                    """
                    INSERT INTO super_ghost_promotion_reviews (
                        created_at, candidate_id, candidate_type, champion_id, symbol, horizon_days,
                        sample_count, actionable_count, candidate_metrics_json, champion_metrics_json,
                        requirements_json, decision, reason, approved_for_promotion, requires_more_shadowing, created_by
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s)
                    RETURNING id
                    """,
                    (
                        _now(), review.get("candidate_id") or "unknown", candidate_type, review.get("champion_id"), review.get("symbol"), horizon,
                        int((review.get("candidate_metrics") or {}).get("sample_count") or 0),
                        int((review.get("candidate_metrics") or {}).get("actionable_count") or 0),
                        _jsonb(review.get("candidate_metrics")), _jsonb(review.get("champion_metrics")), _jsonb(review.get("requirements")),
                        review.get("decision"), review.get("reason"), bool(review.get("approved_for_promotion")), bool(review.get("requires_more_shadowing")), "ghost_promotion_gate",
                    ),
                )
                row = cur.fetchone()
                review["review_id"] = int(row[0]) if row else None
        except Exception as exc:
            review["persist_error"] = str(exc)[:160]
    return review


def latest_promotion_reviews(*, symbol: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_promotion_tables(cur)
            where = "1=1"
            params: List[Any] = []
            if symbol:
                where += " AND symbol=%s"
                params.append(symbol.upper())
            cur.execute(
                f"""
                SELECT id, created_at, candidate_id, candidate_type, champion_id, symbol, horizon_days,
                       sample_count, actionable_count, candidate_metrics_json, champion_metrics_json,
                       requirements_json, decision, reason, approved_for_promotion, requires_more_shadowing, created_by
                FROM super_ghost_promotion_reviews
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT %s
                """,
                params + [max(1, min(100, int(limit)))],
            )
            rows = cur.fetchall()
        return {"ok": True, "reviews": [
            {"id": r[0], "created_at": r[1], "candidate_id": r[2], "candidate_type": r[3], "champion_id": r[4], "symbol": r[5], "horizon_days": r[6], "sample_count": r[7], "actionable_count": r[8], "candidate_metrics": _coerce_json(r[9]), "champion_metrics": _coerce_json(r[10]), "requirements": _coerce_json(r[11]), "decision": r[12], "reason": r[13], "approved_for_promotion": r[14], "requires_more_shadowing": r[15], "created_by": r[16]}
            for r in rows
        ], "requirements": DEFAULT_REQUIREMENTS}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160], "reviews": [], "requirements": DEFAULT_REQUIREMENTS}
