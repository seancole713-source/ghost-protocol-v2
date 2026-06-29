"""Super Ghost Champion/Challenger Lab (PR #95).

A permanent shadow-research lab for the evolving Ghost intelligence system.

Purpose
-------
Production Ghost should not blindly replace itself after a good idea or one lucky
run. Every alternative decision policy must compete against the current
production policy on resolved Truth Ledger rows. Only statistically useful,
out-of-sample evidence should eventually justify promotion.

This module implements the first durable slice:
- deterministic challenger policies evaluated against resolved ledger outcomes;
- risk/return/calibration-style metrics per candidate;
- promotion recommendations that are conservative and evidence-gated;
- persistent lab run/results tables for long-term research memory;
- read-only summaries for UI/API.

It does NOT auto-trade and does NOT auto-promote a model. It creates evidence.
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

LOGGER = logging.getLogger("ghost.super_ghost_lab")

HORIZONS = (1, 5, 20)
MIN_ROWS_FOR_RECOMMENDATION = 30
MIN_ACTIONABLE_FOR_RECOMMENDATION = 8
MIN_SCORE_IMPROVEMENT = 0.20  # expectancy points per actionable call
MIN_WIN_RATE_IMPROVEMENT = 0.03


Decision = str  # UP | DOWN | HOLD | SKIP


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


def ensure_lab_tables(cur) -> None:
    """Create durable experiment-memory tables. Safe and non-destructive."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS super_ghost_lab_runs (
            id SERIAL PRIMARY KEY,
            created_at BIGINT NOT NULL,
            symbol VARCHAR(20),
            horizon_days INT NOT NULL,
            min_rows INT NOT NULL,
            rows_evaluated INT NOT NULL,
            champion_candidate VARCHAR(80) NOT NULL,
            recommended_candidate VARCHAR(80),
            recommendation_status VARCHAR(40) NOT NULL,
            recommendation_reason TEXT,
            payload_json JSONB
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS super_ghost_lab_results (
            id SERIAL PRIMARY KEY,
            run_id INT REFERENCES super_ghost_lab_runs(id) ON DELETE CASCADE,
            candidate VARCHAR(80) NOT NULL,
            rows_evaluated INT NOT NULL,
            actionable_count INT NOT NULL,
            skip_count INT NOT NULL,
            wins INT NOT NULL,
            losses INT NOT NULL,
            win_rate FLOAT,
            false_positive_rate FLOAT,
            avg_signed_return_pct FLOAT,
            net_return_pct FLOAT,
            profit_factor FLOAT,
            max_drawdown_pct FLOAT,
            score FLOAT,
            payload_json JSONB
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_sg_lab_runs_time ON super_ghost_lab_runs (created_at DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_sg_lab_results_run ON super_ghost_lab_results (run_id, candidate)"
    )


def _direction_correct(decision: Decision, ret_pct: Optional[float]) -> Optional[bool]:
    if ret_pct is None:
        return None
    d = (decision or "").upper()
    if d == "UP":
        return ret_pct > 0
    if d == "DOWN":
        return ret_pct < 0
    if d in ("HOLD", "SKIP"):
        return abs(ret_pct) < 3.0
    return None


def _signed_return(decision: Decision, ret_pct: Optional[float]) -> Optional[float]:
    if ret_pct is None:
        return None
    d = (decision or "").upper()
    if d == "UP":
        return float(ret_pct)
    if d == "DOWN":
        return -float(ret_pct)
    return 0.0


def _has_direction(row: Dict[str, Any]) -> bool:
    return str(row.get("direction") or "").upper() in ("UP", "DOWN")


def _actionable_action(row: Dict[str, Any]) -> bool:
    action = str(row.get("action") or "").upper()
    return "HIGH-CONVICTION" in action or "WATCHLIST" in action


def _champion(row: Dict[str, Any]) -> Decision:
    """Production behavior recorded in the ledger."""
    direction = str(row.get("direction") or "HOLD").upper()
    if direction in ("UP", "DOWN") and _actionable_action(row):
        return direction
    return "HOLD"


def _coverage_gate(row: Dict[str, Any]) -> Decision:
    direction = str(row.get("direction") or "HOLD").upper()
    coverage = int(row.get("checklist_coverage") or 0)
    if direction in ("UP", "DOWN") and coverage >= 18:
        return direction
    return "SKIP"


def _strict_confidence(row: Dict[str, Any]) -> Decision:
    direction = str(row.get("direction") or "HOLD").upper()
    conf = _f(row.get("confidence")) or 0.0
    coverage = int(row.get("checklist_coverage") or 0)
    if direction in ("UP", "DOWN") and coverage >= 18 and conf >= 0.70:
        return direction
    return "SKIP"


def _grade_b_or_better(row: Dict[str, Any]) -> Decision:
    direction = str(row.get("direction") or "HOLD").upper()
    grade = str(row.get("accuracy_grade") or "").upper()
    if direction in ("UP", "DOWN") and grade in ("A+", "A", "B+", "B"):
        return direction
    return "SKIP"


def _regime_aligned(row: Dict[str, Any]) -> Decision:
    direction = str(row.get("direction") or "HOLD").upper()
    regime = str(row.get("regime_risk_state") or "").lower()
    coverage = int(row.get("checklist_coverage") or 0)
    if direction == "UP" and regime == "risk_off":
        return "SKIP"
    if direction == "DOWN" and regime == "risk_on":
        return "SKIP"
    if direction in ("UP", "DOWN") and coverage >= 18:
        return direction
    return "SKIP"


def _edge_score_policy(row: Dict[str, Any]) -> Decision:
    edge = _f(row.get("edge_score"))
    coverage = int(row.get("checklist_coverage") or 0)
    if edge is None or coverage < 18:
        return "SKIP"
    if edge >= 12.0:
        return "UP"
    if edge <= -12.0:
        return "DOWN"
    return "SKIP"


@dataclass(frozen=True)
class Candidate:
    name: str
    description: str
    fn: Callable[[Dict[str, Any]], Decision]


CANDIDATES: Tuple[Candidate, ...] = (
    Candidate("production_champion", "Recorded production Ghost decision/action from the Truth Ledger.", _champion),
    Candidate("coverage_gate", "Only act when checklist coverage is >=18/25; otherwise skip.", _coverage_gate),
    Candidate("strict_confidence", "Only act when coverage >=18/25 and confidence >=70%.", _strict_confidence),
    Candidate("grade_b_or_better", "Only act on A/B grade directional calls.", _grade_b_or_better),
    Candidate("regime_aligned", "Skip longs in risk-off and shorts in risk-on; require coverage >=18/25.", _regime_aligned),
    Candidate("edge_score_policy", "Ignore recorded direction and act only on strong edge_score sign.", _edge_score_policy),
)


def candidate_manifest() -> List[Dict[str, str]]:
    return [{"name": c.name, "description": c.description} for c in CANDIDATES]


def evaluate_candidate(rows: List[Dict[str, Any]], candidate: Candidate, *, horizon: int = 5) -> Dict[str, Any]:
    """Evaluate one candidate on resolved rows. Pure/testable."""
    ret_key = f"return_{horizon}d_pct"
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    signed_returns: List[float] = []
    decisions: List[Dict[str, Any]] = []
    actionable = 0
    skip_count = 0
    wins = 0
    losses = 0
    gross_win = 0.0
    gross_loss = 0.0

    for row in rows:
        ret = _f(row.get(ret_key))
        if ret is None:
            continue
        decision = str(candidate.fn(row) or "SKIP").upper()
        if decision not in ("UP", "DOWN", "HOLD", "SKIP"):
            decision = "SKIP"
        correct = _direction_correct(decision, ret)
        signed = _signed_return(decision, ret)
        if decision in ("UP", "DOWN"):
            actionable += 1
            if correct:
                wins += 1
            else:
                losses += 1
            if signed is not None:
                signed_returns.append(signed)
                equity += signed
                if signed > 0:
                    gross_win += signed
                elif signed < 0:
                    gross_loss += abs(signed)
                peak = max(peak, equity)
                max_dd = min(max_dd, equity - peak)
        else:
            skip_count += 1
        decisions.append({"id": row.get("id"), "symbol": row.get("symbol"), "decision": decision, "return_pct": ret, "correct": correct})

    n = len([r for r in rows if _f(r.get(ret_key)) is not None])
    win_rate = wins / actionable if actionable else None
    false_positive_rate = losses / actionable if actionable else None
    avg_signed = sum(signed_returns) / len(signed_returns) if signed_returns else None
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else None)
    # Score is conservative expectancy: average signed return penalized for false positives and drawdown.
    score = None
    if avg_signed is not None:
        fpr = false_positive_rate or 0.0
        score = avg_signed - (fpr * 0.25) + (max_dd / 100.0)

    return {
        "candidate": candidate.name,
        "description": candidate.description,
        "rows_evaluated": n,
        "actionable_count": actionable,
        "skip_count": skip_count,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "false_positive_rate": round(false_positive_rate, 4) if false_positive_rate is not None else None,
        "avg_signed_return_pct": round(avg_signed, 3) if avg_signed is not None else None,
        "net_return_pct": round(sum(signed_returns), 3) if signed_returns else None,
        "profit_factor": round(profit_factor, 4) if isinstance(profit_factor, float) and math.isfinite(profit_factor) else profit_factor,
        "max_drawdown_pct": round(abs(max_dd), 3),
        "score": round(score, 4) if score is not None else None,
        "sample_decisions": decisions[-10:],
    }


def benchmark_candidates(rows: List[Dict[str, Any]], *, horizon: int = 5, min_rows: int = MIN_ROWS_FOR_RECOMMENDATION) -> Dict[str, Any]:
    h = horizon if horizon in HORIZONS else 5
    resolved = [r for r in rows if _f(r.get(f"return_{h}d_pct")) is not None]
    results = [evaluate_candidate(resolved, c, horizon=h) for c in CANDIDATES]
    by_name = {r["candidate"]: r for r in results}
    champion = by_name.get("production_champion") or (results[0] if results else {})
    ranked = sorted(
        results,
        key=lambda r: (
            r.get("score") is not None,
            r.get("score") if r.get("score") is not None else -999,
            r.get("actionable_count") or 0,
        ),
        reverse=True,
    )
    best = ranked[0] if ranked else None

    status = "insufficient_rows"
    reason = f"Need at least {min_rows} resolved rows; have {len(resolved)}."
    recommended = None
    if len(resolved) >= min_rows and best:
        if (best.get("actionable_count") or 0) < MIN_ACTIONABLE_FOR_RECOMMENDATION:
            status = "insufficient_actionable"
            reason = f"Best candidate has only {best.get('actionable_count') or 0} actionable calls; need {MIN_ACTIONABLE_FOR_RECOMMENDATION}."
        else:
            champ_score = champion.get("score")
            best_score = best.get("score")
            champ_wr = champion.get("win_rate") or 0.0
            best_wr = best.get("win_rate") or 0.0
            if best["candidate"] != "production_champion" and best_score is not None and champ_score is not None and (best_score - champ_score) >= MIN_SCORE_IMPROVEMENT and (best_wr - champ_wr) >= MIN_WIN_RATE_IMPROVEMENT:
                status = "challenger_candidate"
                recommended = best["candidate"]
                reason = "Challenger beat champion on score and win-rate gates. Keep in shadow until independently confirmed."
            else:
                status = "keep_champion"
                reason = "No challenger cleared improvement gates over the production champion."

    return {
        "ok": True,
        "horizon_days": h,
        "rows_evaluated": len(resolved),
        "min_rows_for_recommendation": min_rows,
        "champion_candidate": "production_champion",
        "recommended_candidate": recommended,
        "recommendation_status": status,
        "recommendation_reason": reason,
        "results": ranked,
        "candidate_manifest": candidate_manifest(),
        "note": "Shadow benchmark only. No auto-promotion, no trading, no financial advice.",
    }


def _resolved_rows(cur, *, symbol: Optional[str], horizon: int, limit: int) -> List[Dict[str, Any]]:
    h = horizon if horizon in HORIZONS else 5
    ret_col = f"return_{h}d_pct"
    correct_col = f"correct_{h}d"
    resolved_col = f"resolved_{h}d_at"
    cols = [
        "id", "symbol", "created_at", "reference_price", "direction", "action", "confidence",
        "conviction_score", "edge_score", "quality_score", "accuracy_grade", "checklist_coverage",
        "regime_label", "regime_risk_state", "stop_loss", "target_price", ret_col, correct_col, resolved_col,
    ]
    where = f"{resolved_col} IS NOT NULL AND {ret_col} IS NOT NULL"
    params: List[Any] = []
    if symbol:
        where += " AND symbol = %s"
        params.append(symbol.upper())
    cur.execute(
        f"SELECT {', '.join(cols)} FROM super_ghost_predictions WHERE {where} ORDER BY created_at ASC LIMIT %s",
        params + [max(1, min(5000, int(limit)))],
    )
    rows = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        d[f"return_{h}d_pct"] = d.pop(ret_col)
        d[f"correct_{h}d"] = d.pop(correct_col)
        d[f"resolved_{h}d_at"] = d.pop(resolved_col)
        rows.append(d)
    return rows


def run_lab(*, symbol: Optional[str] = None, horizon: int = 5, limit: int = 1000, persist: bool = True) -> Dict[str, Any]:
    """Run a champion/challenger benchmark and optionally persist results."""
    h = horizon if horizon in HORIZONS else 5
    try:
        from core.db import db_conn
        from core.super_ghost_ledger import ensure_ledger_table

        with db_conn() as conn:
            cur = conn.cursor()
            ensure_ledger_table(cur)
            ensure_lab_tables(cur)
            rows = _resolved_rows(cur, symbol=symbol, horizon=h, limit=limit)
            out = benchmark_candidates(rows, horizon=h)
            if persist:
                now = _now()
                cur.execute(
                    """
                    INSERT INTO super_ghost_lab_runs (
                        created_at, symbol, horizon_days, min_rows, rows_evaluated,
                        champion_candidate, recommended_candidate, recommendation_status,
                        recommendation_reason, payload_json
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    RETURNING id
                    """,
                    (
                        now, (symbol or "ALL").upper(), h, out["min_rows_for_recommendation"], out["rows_evaluated"],
                        out["champion_candidate"], out.get("recommended_candidate"), out["recommendation_status"],
                        out["recommendation_reason"], _jsonb(out),
                    ),
                )
                run_id = int(cur.fetchone()[0])
                for r in out.get("results", []):
                    cur.execute(
                        """
                        INSERT INTO super_ghost_lab_results (
                            run_id, candidate, rows_evaluated, actionable_count, skip_count,
                            wins, losses, win_rate, false_positive_rate, avg_signed_return_pct,
                            net_return_pct, profit_factor, max_drawdown_pct, score, payload_json
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                        """,
                        (
                            run_id, r["candidate"], r["rows_evaluated"], r["actionable_count"], r["skip_count"],
                            r["wins"], r["losses"], r.get("win_rate"), r.get("false_positive_rate"),
                            r.get("avg_signed_return_pct"), r.get("net_return_pct"), r.get("profit_factor"),
                            r.get("max_drawdown_pct"), r.get("score"), _jsonb(r),
                        ),
                    )
                out["run_id"] = run_id
            return out
    except Exception as exc:
        LOGGER.warning("run_lab: %s", str(exc)[:180])
        return {"ok": False, "error": str(exc)[:180], "results": []}


def latest_lab_summary(*, symbol: Optional[str] = None, horizon: int = 5) -> Dict[str, Any]:
    """Return the latest persisted lab run, or a cold-start response."""
    h = horizon if horizon in HORIZONS else 5
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_lab_tables(cur)
            where = "horizon_days = %s"
            params: List[Any] = [h]
            if symbol:
                where += " AND symbol = %s"
                params.append(symbol.upper())
            cur.execute(
                f"""
                SELECT id, created_at, symbol, horizon_days, rows_evaluated, champion_candidate,
                       recommended_candidate, recommendation_status, recommendation_reason, payload_json
                FROM super_ghost_lab_runs
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT 1
                """,
                params,
            )
            row = cur.fetchone()
            if not row:
                return {"ok": True, "available": False, "symbol": (symbol or "ALL").upper(), "horizon_days": h, "message": "No lab run persisted yet.", "candidate_manifest": candidate_manifest()}
            payload = _coerce_json(row[9]) or {}
            payload.update({
                "ok": True,
                "available": True,
                "run_id": row[0],
                "created_at": row[1],
                "symbol": row[2],
                "horizon_days": row[3],
                "rows_evaluated": row[4],
                "champion_candidate": row[5],
                "recommended_candidate": row[6],
                "recommendation_status": row[7],
                "recommendation_reason": row[8],
            })
            return payload
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160], "available": False}
