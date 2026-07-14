"""core/watcher.py — read-only Ghost babysitter / calibration observer (PR #153).

The Watcher never changes Ghost decisions. It summarizes existing persisted
prediction/shadow evidence so a human can see whether Ghost's stated confidence
is calibrated to reality or just guessing.
"""
from __future__ import annotations

import math
import time
from core.quiet import note_suppressed
from typing import Any, Dict, Iterable, List, Optional, Sequence


def wilson_interval(wins: int, n: int, z: float = 1.96) -> Dict[str, float]:
    if n <= 0:
        return {"p": 0.0, "low": 0.0, "high": 0.0}
    p = wins / n
    denom = 1.0 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return {
        "p": round(p, 4),
        "low": round(max(0.0, (centre - margin) / denom), 4),
        "high": round(min(1.0, (centre + margin) / denom), 4),
    }


def brier_score(rows: Sequence[Dict[str, Any]], *, prob_key: str = "prob", outcome_key: str = "win") -> Optional[float]:
    vals: List[float] = []
    for r in rows:
        p = r.get(prob_key)
        y = r.get(outcome_key)
        if p is None or y is None:
            continue
        try:
            pf = max(0.0, min(1.0, float(p)))
            yf = 1.0 if bool(y) else 0.0
            vals.append((pf - yf) ** 2)
        except Exception:
            continue
    if not vals:
        return None
    return round(sum(vals) / len(vals), 4)


def calibration_bins(
    rows: Sequence[Dict[str, Any]],
    *,
    prob_key: str = "prob",
    outcome_key: str = "win",
    buckets: Sequence[tuple[str, float, float]] | None = None,
) -> List[Dict[str, Any]]:
    """Bucket resolved probabilities vs actual outcomes.

    Pure/read-only. Rows with missing probability/outcome are ignored. Bounds are
    [lo, hi) except the final bucket if hi >= 1.0.
    """
    spec = buckets or (
        ("<50", 0.0, 0.50),
        ("50-55", 0.50, 0.55),
        ("55-60", 0.55, 0.60),
        ("60-70", 0.60, 0.70),
        ("70+", 0.70, 1.01),
    )
    out: List[Dict[str, Any]] = []
    for label, lo, hi in spec:
        picked = []
        for r in rows:
            p = r.get(prob_key)
            y = r.get(outcome_key)
            if p is None or y is None:
                continue
            try:
                pf = float(p)
            except Exception:
                continue
            in_bucket = (pf >= lo and (pf < hi or (hi >= 1.0 and pf <= hi)))
            if in_bucket:
                picked.append((pf, 1 if bool(y) else 0))
        n = len(picked)
        wins = sum(y for _, y in picked)
        ci = wilson_interval(wins, n)
        out.append({
            "label": label,
            "min": lo,
            "max": hi,
            "n": n,
            "wins": wins,
            "win_rate": round(wins / n, 4) if n else None,
            "wilson_low": ci["low"] if n else None,
            "wilson_high": ci["high"] if n else None,
            "mean_prob": round(sum(p for p, _ in picked) / n, 4) if n else None,
            "calibration_gap": round((wins / n) - (sum(p for p, _ in picked) / n), 4) if n else None,
        })
    return out


def watcher_verdict(*, high_win_rate: Optional[float], high_n: int, brier: Optional[float]) -> Dict[str, str]:
    """Plain-English verdict. Honest by construction; no flattery."""
    if high_n < 30:
        return {
            "status": "insufficient_evidence",
            "headline": f"Only {high_n} high-confidence resolved rows; keep watching before judging.",
        }
    if high_win_rate is None:
        return {"status": "insufficient_evidence", "headline": "No resolved high-confidence rows."}
    if high_win_rate >= 0.70:
        return {"status": "proven_high_confidence", "headline": f"High-confidence calls are running {high_win_rate*100:.1f}% — 70%+ observed, verify Wilson floor before promotion."}
    if high_win_rate >= 0.58:
        return {"status": "real_but_not_70", "headline": f"High-confidence calls are running {high_win_rate*100:.1f}% — real signal, not yet 70%."}
    if high_win_rate >= 0.47:
        return {"status": "near_coin_flip", "headline": f"High-confidence calls are running {high_win_rate*100:.1f}% — close to guessing."}
    return {"status": "inverted_or_broken", "headline": f"High-confidence calls are running {high_win_rate*100:.1f}% — worse than chance; do not trust."}


def _additional_wins_needed(wins: int, n: int, target: float, *, wilson: bool) -> Optional[int]:
    """How many consecutive future wins are needed to clear ``target``.

    Pure planning math for the operator: no optimism, no mutation. If ``wilson``
    is true, the Wilson LOWER bound must clear the target; otherwise the raw
    win rate must clear it. Consecutive wins is the best-case path, so any
    future loss increases the true requirement.
    """
    if target <= 0:
        return 0
    if target >= 1:
        return None
    wins_i = int(wins or 0)
    n_i = int(n or 0)
    for extra in range(0, 10000):
        nn = n_i + extra
        ww = wins_i + extra
        if nn <= 0:
            continue
        if wilson:
            if wilson_interval(ww, nn)["low"] >= target:
                return extra
        elif (ww / nn) >= target:
            return extra
    return None


def contract_win_test_status(*, wins: int, n: int, target: float = 0.70) -> Dict[str, Any]:
    """70+ confidence bucket contract status.

    The raw rate answers "is the bucket currently 70%+ observed?" The Wilson
    lower bound answers the stricter question "is 70% statistically proven?"
    Both are shown because the operator asked for 70+; Ghost should never hide
    whether the bar is merely observed or actually proven.
    """
    n_i = int(n or 0)
    w_i = int(wins or 0)
    wr = (w_i / n_i) if n_i else None
    ci = wilson_interval(w_i, n_i) if n_i else {"p": 0.0, "low": 0.0, "high": 0.0}
    target_f = max(0.0, min(1.0, float(target or 0.70)))
    raw_pass = bool(wr is not None and wr >= target_f)
    wilson_pass = bool(n_i > 0 and ci["low"] >= target_f)
    if wilson_pass:
        status = "passed_wilson"
    elif raw_pass:
        status = "raw_70_unproven"
    else:
        status = "not_70"
    return {
        "threshold": 0.70,
        "target_win_rate": round(target_f, 4),
        "n": n_i,
        "wins": w_i,
        "win_rate": round(wr, 4) if wr is not None else None,
        "wilson": ci if n_i else None,
        "raw_pass": raw_pass,
        "wilson_pass": wilson_pass,
        "status": status,
        "additional_consecutive_wins_needed_raw": _additional_wins_needed(w_i, n_i, target_f, wilson=False),
        "additional_consecutive_wins_needed_wilson": _additional_wins_needed(w_i, n_i, target_f, wilson=True),
        "note": ("Consecutive-wins counts are best-case; any future loss raises the requirement. "
                 "Wilson_pass is the statistically-proven 70+ bar."),
    }


def contract_70_symbol_breakdown(rows: Sequence[Dict[str, Any]], *, target: float = 0.70) -> List[Dict[str, Any]]:
    """Per-symbol breakdown for rows in the 70+ confidence bucket.

    Read-only diagnosis for the 70+ mission: which symbols are dragging the
    high-confidence bucket below target, and which are actually carrying it.
    This is not a selector by itself — it is evidence for the next pre-registered
    filter or retrain decision.
    """
    grouped: Dict[str, Dict[str, int]] = {}
    for r in rows:
        try:
            p = float(r.get("prob"))
        except Exception:
            continue
        if p < 0.70:
            continue
        sym = str(r.get("symbol") or "UNKNOWN").upper()
        g = grouped.setdefault(sym, {"n": 0, "wins": 0})
        g["n"] += 1
        if bool(r.get("win")):
            g["wins"] += 1
    out = []
    for sym, g in grouped.items():
        n = int(g["n"])
        wins = int(g["wins"])
        wr = wins / n if n else None
        ci = wilson_interval(wins, n) if n else None
        out.append({
            "symbol": sym,
            "n": n,
            "wins": wins,
            "win_rate": round(wr, 4) if wr is not None else None,
            "wilson_low": (ci or {}).get("low") if ci else None,
            "raw_pass": bool(wr is not None and wr >= target),
            "wilson_pass": bool(ci and ci.get("low", 0) >= target),
        })
    out.sort(key=lambda x: (x["win_rate"] if x["win_rate"] is not None else -1, -x["n"], x["symbol"]))
    return out


def summarize_shadow_outcomes(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarize ghost_shadow_outcomes-like rows for Watcher endpoint/tests."""
    resolved = []
    for r in rows:
        outcome = str(r.get("outcome") or "").upper()
        if outcome not in ("WIN", "LOSS"):
            continue
        resolved.append({"prob": r.get("up_prob"), "win": outcome == "WIN", "symbol": r.get("symbol")})
    bins = calibration_bins(resolved, prob_key="prob", outcome_key="win")
    high = [r for r in resolved if r.get("prob") is not None and float(r.get("prob")) >= 0.55]
    high_wins = sum(1 for r in high if r["win"])
    high_wr = high_wins / len(high) if high else None
    high70 = [r for r in resolved if r.get("prob") is not None and float(r.get("prob")) >= 0.70]
    high70_wins = sum(1 for r in high70 if r["win"])
    try:
        from core.accuracy_contract import active_contract
        target_wr = float(active_contract().target_win_rate)
    except Exception:
        target_wr = 0.70
    brier = brier_score(resolved, prob_key="prob", outcome_key="win")
    contract_70 = contract_win_test_status(wins=high70_wins, n=len(high70), target=target_wr)
    contract_70["symbols"] = contract_70_symbol_breakdown(resolved, target=target_wr)
    return {
        "resolved_n": len(resolved),
        "high_confidence": {
            "threshold": 0.55,
            "n": len(high),
            "wins": high_wins,
            "win_rate": round(high_wr, 4) if high_wr is not None else None,
            **({"wilson": wilson_interval(high_wins, len(high))} if high else {"wilson": None}),
        },
        "brier": brier,
        "bins": bins,
        "contract_70": contract_70,
        "verdict": watcher_verdict(high_win_rate=high_wr, high_n=len(high), brier=brier),
    }


def watcher_summary(*, days: int = 30, limit: int = 5000) -> Dict[str, Any]:
    """Read-only live summary from existing Ghost tables."""
    from core.db import db_conn
    cutoff = int(time.time()) - max(1, min(365, int(days))) * 86400
    with db_conn() as conn:
        cur = conn.cursor()
        rows: List[Dict[str, Any]] = []
        try:
            cur.execute(
                """
                SELECT symbol, eval_ts, up_prob, outcome, pnl_pct
                FROM ghost_shadow_outcomes
                WHERE eval_ts >= %s AND outcome IN ('WIN','LOSS')
                ORDER BY eval_ts DESC
                LIMIT %s
                """,
                (cutoff, max(1, min(20000, int(limit)))),
            )
            rows = [
                {"symbol": r[0], "eval_ts": r[1], "up_prob": r[2], "outcome": r[3], "pnl_pct": r[4]}
                for r in cur.fetchall()
            ]
        except Exception:
            rows = []

        skip_rows: List[Dict[str, Any]] = []
        try:
            cur.execute(
                """
                SELECT skip_code, COUNT(*)
                FROM ghost_perf_symbol_evals
                WHERE eval_ts >= %s AND fired = FALSE
                GROUP BY skip_code
                ORDER BY COUNT(*) DESC
                LIMIT 12
                """,
                (cutoff,),
            )
            skip_rows = [{"skip_code": r[0] or "unknown", "count": int(r[1] or 0)} for r in cur.fetchall()]
        except Exception:
            skip_rows = []

    shadow = summarize_shadow_outcomes(rows)
    brains: List[Dict[str, Any]] = []
    try:
        from core.db import db_conn as _db_conn
        with _db_conn() as conn:
            cur = conn.cursor()
            # Read-only: do not CREATE tables from the summary endpoint. If the
            # shadow profile table is absent, return an empty profile list.
            cur.execute(
                """
                SELECT model_id, model_family, horizon_days, sample_count, actionable_count,
                       wins, losses, win_rate, avg_signed_return_pct, net_return_pct, status, updated_at,
                       calibration_error
                FROM super_ghost_shadow_model_profiles
                ORDER BY horizon_days, win_rate DESC NULLS LAST, sample_count DESC
                LIMIT 200
                """
            )
            brains = [
                {"model_id": r[0], "model_family": r[1], "horizon_days": r[2],
                 "sample_count": r[3], "actionable_count": r[4], "wins": r[5],
                 "losses": r[6], "win_rate": r[7], "avg_signed_return_pct": r[8],
                 "net_return_pct": r[9], "status": r[10], "updated_at": r[11],
                 # PR #163: Brier on the brain's own confidence — makes
                 # confidence quality visible per brain, not just direction.
                 "brier": r[12]}
                for r in cur.fetchall()
            ]
    except Exception:
        note_suppressed()
        brains = []
    try:
        from core.super_ghost_shadow import shadow_manifest
        manifest = shadow_manifest()
    except Exception:
        note_suppressed()
        manifest = []
    return {
        "ok": True,
        "read_only": True,
        "days": max(1, min(365, int(days))),
        "purpose": "Watcher is a notebook: observes confidence calibration, gate blocks, and shadow-brain evidence; never influences Ghost decisions.",
        "shadow_calibration": shadow,
        "blind_spots": {"top_skip_codes": skip_rows},
        "brains": brains,
        "manifest": manifest,
    }


def ensure_watcher_tables(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_watcher_snapshots (
            id SERIAL PRIMARY KEY,
            created_at BIGINT NOT NULL,
            days INT NOT NULL,
            resolved_n INT NOT NULL,
            high_conf_n INT NOT NULL,
            high_conf_win_rate FLOAT,
            brier FLOAT,
            verdict_status TEXT,
            payload_json JSONB NOT NULL
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_watcher_created_at ON ghost_watcher_snapshots (created_at DESC)")


def snapshot_watcher(*, days: int = 30) -> Dict[str, Any]:
    """Append one Watcher observation. Safe: writes only Watcher's notebook table."""
    import json as _json
    from core.db import db_conn
    summary = watcher_summary(days=days)
    now = int(time.time())
    shadow = summary.get("shadow_calibration") or {}
    high = shadow.get("high_confidence") or {}
    verdict = shadow.get("verdict") or {}
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_watcher_tables(cur)
        cur.execute(
            """
            INSERT INTO ghost_watcher_snapshots
                (created_at, days, resolved_n, high_conf_n, high_conf_win_rate, brier, verdict_status, payload_json)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
            """,
            (
                now,
                int(summary.get("days") or days),
                int(shadow.get("resolved_n") or 0),
                int(high.get("n") or 0),
                high.get("win_rate"),
                shadow.get("brier"),
                verdict.get("status"),
                _json.dumps(summary),
            ),
        )
    return {"ok": True, "read_only_decisions": True, "snapshot_at": now, "summary": summary}


def latest_watcher_snapshots(*, limit: int = 20) -> Dict[str, Any]:
    """Read Watcher's own notebook rows. GET path is read-only: no DDL."""
    from core.db import db_conn
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT created_at, days, resolved_n, high_conf_n, high_conf_win_rate, brier, verdict_status
                FROM ghost_watcher_snapshots
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (max(1, min(200, int(limit))),),
            )
            rows = cur.fetchall()
    except Exception:
        note_suppressed()
        rows = []
    return {"ok": True, "read_only": True, "rows": [
        {"created_at": r[0], "days": r[1], "resolved_n": r[2], "high_conf_n": r[3],
         "high_conf_win_rate": r[4], "brier": r[5], "verdict_status": r[6]}
        for r in rows
    ]}
