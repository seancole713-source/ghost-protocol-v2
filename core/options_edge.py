"""Options-PCR edge test — the harness that answers, once enough forward data
has accrued, whether the put/call ratio actually discriminates winners.

Read-only. Joins the point-in-time options snapshots (ghost_options_snapshots)
to the shadow-outcome ledger (ghost_shadow_outcomes) on (symbol, date) — the
PCR that was live the day the virtual pick was made — then buckets by PCR and
Wilson-tests each bucket, family-corrected with the same Sidak machinery the
contract-70 slice search uses. EXPIRED counts as a non-win (2026-07-14 rule).

This is built NOW, before the data is sufficient, so that the moment ~2 weeks
of daily snapshots have paired with resolved outcomes it runs unchanged and
gives an honest verdict: PCR discriminates (spread + a Wilson-proven bucket),
or PCR is flat (dead, like up_prob) — no fabrication either way.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

LOGGER = logging.getLogger("ghost.options_edge")

# PCR = put/call VOLUME ratio. Low = call-heavy (bullish flow); high = put-heavy.
PCR_BUCKETS: Tuple[Tuple[str, float, float], ...] = (
    ("<0.5", 0.0, 0.5),
    ("0.5-0.7", 0.5, 0.7),
    ("0.7-1.0", 0.7, 1.0),
    ("1.0-1.5", 1.0, 1.5),
    (">=1.5", 1.5, float("inf")),
)

# Enough paired evidence to trust a bucket Wilson bound at all.
READY_MIN_PAIRED = 200
READY_MIN_DAYS = 8
TARGET = 0.70


def _bucket_for(pcr: float) -> Optional[str]:
    for label, lo, hi in PCR_BUCKETS:
        if lo <= pcr < hi:
            return label
    return None


def load_paired_rows(days: int = 60, limit: int = 50000) -> Optional[List[Dict[str, Any]]]:
    """Resolved shadow outcomes joined to the point-in-time PCR for that
    (symbol, trade_date). None means the read failed; [] means no pairs yet."""
    try:
        from core.db import db_conn
        cutoff = int(time.time()) - max(1, min(365, int(days))) * 86400
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT s.symbol, s.trade_date, s.outcome, s.up_prob, o.pcr_volume
                FROM ghost_shadow_outcomes s
                JOIN ghost_options_snapshots o
                  ON o.symbol = s.symbol AND o.snap_date = s.trade_date
                WHERE s.eval_ts >= %s
                  AND s.outcome IN ('WIN','LOSS','EXPIRED')
                  AND o.pcr_volume IS NOT NULL
                ORDER BY s.eval_ts DESC
                LIMIT %s
                """,
                (cutoff, max(1, min(200000, int(limit)))),
            )
            return [
                {"symbol": r[0], "trade_date": r[1], "outcome": r[2],
                 "up_prob": r[3], "pcr": float(r[4])}
                for r in cur.fetchall()
            ]
    except Exception as e:
        LOGGER.warning("options-edge join failed: %s", str(e)[:100])
        return None


def summarize_pcr_edge(rows: Sequence[Dict[str, Any]], *, target: float = TARGET) -> Dict[str, Any]:
    """Pure bucket analysis — testable without a DB. WIN vs (LOSS|EXPIRED)."""
    from core.watcher import wilson_interval
    from core.contract_70_slices import _sidak_family_z

    buckets: Dict[str, Dict[str, int]] = {lbl: {"n": 0, "wins": 0} for lbl, _, _ in PCR_BUCKETS}
    for r in rows:
        b = _bucket_for(float(r.get("pcr") or -1))
        if b is None:
            continue
        buckets[b]["n"] += 1
        if str(r.get("outcome")) == "WIN":
            buckets[b]["wins"] += 1

    non_empty = [(lbl, d) for lbl, d in buckets.items() if d["n"] > 0]
    family_z = _sidak_family_z(max(1, len(non_empty)))
    out_buckets = []
    wrs = []
    proven = []
    for lbl, d in buckets.items():
        n, w = d["n"], d["wins"]
        wr = (w / n) if n else None
        ci = wilson_interval(w, n) if n else None
        ci_fam = wilson_interval(w, n, z=family_z) if n else None
        wilson_pass = bool(n and ci_fam and ci_fam["low"] >= target)
        if wr is not None:
            wrs.append(wr)
        if wilson_pass:
            proven.append(lbl)
        out_buckets.append({
            "pcr_bucket": lbl, "n": n, "wins": w,
            "win_rate": round(wr, 4) if wr is not None else None,
            "wilson_low": round(ci["low"], 4) if ci else None,
            "family_wilson_low": round(ci_fam["low"], 4) if ci_fam else None,
            "wilson_pass_70": wilson_pass,
        })

    spread = (max(wrs) - min(wrs)) if len(wrs) >= 2 else None
    total_n = sum(d["n"] for _, d in buckets.items())
    return {
        "buckets": out_buckets,
        "family_size": len(non_empty),
        "family_z": round(family_z, 4),
        "total_paired": total_n,
        "win_rate_spread": round(spread, 4) if spread is not None else None,
        "proven_70_buckets": proven,
        "discriminates": bool(spread is not None and spread >= 0.10),
        "verdict": (
            "PROVEN_70" if proven else
            "DISCRIMINATES_UNPROVEN" if (spread is not None and spread >= 0.10) else
            "FLAT_NO_EDGE" if total_n else "NO_DATA"
        ),
    }


def options_pcr_edge(days: int = 60) -> Dict[str, Any]:
    """Live PCR-edge verdict. Read-only; honest 'insufficient' until data accrues."""
    rows = load_paired_rows(days)
    if rows is None:
        return {"ok": True, "status": "READ_FAILED", "read_only": True}
    ready = options_pcr_readiness(days)
    res = summarize_pcr_edge(rows)
    sufficient = (res["total_paired"] >= READY_MIN_PAIRED
                  and ready.get("distinct_days", 0) >= READY_MIN_DAYS)
    return {
        "ok": True,
        "read_only": True,
        "days": int(days),
        "ts": int(time.time()),
        "sufficient_data": sufficient,
        "readiness": ready,
        "result": res,
        "note": (
            "Verdict is provisional until sufficient_data=true "
            f"(>= {READY_MIN_PAIRED} paired obs across >= {READY_MIN_DAYS} days). "
            "EXPIRED counts as a non-win. This tests whether PCR separates "
            "winners; it does not loosen any gate."
        ),
    }


def options_pcr_readiness(days: int = 60) -> Dict[str, Any]:
    """Accrual health: is the forward-clock actually building a testable set?
    Watches for the silent-failure mode that killed the v1 collector."""
    try:
        from core.db import db_conn
        cutoff = int(time.time()) - max(1, min(365, int(days))) * 86400
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*), COUNT(DISTINCT snap_date), COUNT(DISTINCT symbol) "
                "FROM ghost_options_snapshots WHERE ts >= %s AND available = TRUE",
                (cutoff,),
            )
            total, days_ct, syms = cur.fetchone()
            cur.execute(
                "SELECT snap_date, COUNT(*) FROM ghost_options_snapshots "
                "WHERE ts >= %s AND available = TRUE GROUP BY snap_date "
                "ORDER BY snap_date DESC LIMIT 5",
                (cutoff,),
            )
            recent = [{"date": r[0], "rows": int(r[1])} for r in cur.fetchall()]
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    paired = load_paired_rows(days) or []
    return {
        "ok": True,
        "total_snapshot_rows": int(total or 0),
        "distinct_days": int(days_ct or 0),
        "distinct_symbols": int(syms or 0),
        "recent_days": recent,
        "paired_with_outcomes": len(paired),
        "ready_to_test": bool(len(paired) >= READY_MIN_PAIRED and (days_ct or 0) >= READY_MIN_DAYS),
        "need": {
            "min_paired": READY_MIN_PAIRED, "min_days": READY_MIN_DAYS,
            "paired_so_far": len(paired), "days_so_far": int(days_ct or 0),
        },
    }
