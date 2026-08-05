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
PCR_DIRECTIONS: Tuple[str, ...] = ("UP", "DOWN")

# Enough paired evidence to trust a bucket Wilson bound at all.
READY_MIN_PAIRED = 200
READY_MIN_DAYS = 8
TARGET = 0.70
MAX_SNAPSHOT_AGE_S = 4 * 86400
DIRECTIONAL_FAMILY_SIZE = len(PCR_BUCKETS) * len(PCR_DIRECTIONS)


def _bucket_for(pcr: float) -> Optional[str]:
    for label, lo, hi in PCR_BUCKETS:
        if lo <= pcr < hi:
            return label
    return None


def load_paired_rows(days: int = 60, limit: int = 50000) -> Optional[List[Dict[str, Any]]]:
    """Resolved outcomes joined to the latest PCR available at evaluation.

    None means the read failed; [] means no pairs yet. The age bound admits a
    prior trading session across a weekend without treating stale flow as live.
    """
    try:
        from core.db import db_conn
        cutoff = int(time.time()) - max(1, min(365, int(days))) * 86400
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                                SELECT s.symbol, s.trade_date, s.outcome, s.up_prob, o.pcr_volume,
                                             s.eval_ts, o.snap_date, o.ts, s.direction, s.model_prob,
                                             s.model_sha256, s.feature_schema, s.label_schema,
                                             s.validation_schema, s.hold_bars
                FROM ghost_shadow_outcomes s
                                JOIN LATERAL (
                                        SELECT snap_date, ts, pcr_volume
                                        FROM ghost_options_snapshots
                                        WHERE symbol = s.symbol
                                            AND available = TRUE
                                            AND pcr_volume IS NOT NULL
                                            AND ts <= s.eval_ts
                                            AND ts >= s.eval_ts - %s
                                        ORDER BY ts DESC
                                        LIMIT 1
                                ) o ON TRUE
                WHERE s.eval_ts >= %s
                  AND s.outcome IN ('WIN','LOSS','EXPIRED')
                                    AND s.direction IN ('UP','DOWN')
                                    AND s.model_sha256 IS NOT NULL
                                    AND s.feature_schema IS NOT NULL
                                    AND s.label_schema IS NOT NULL
                                    AND s.validation_schema IS NOT NULL
                                    AND s.hold_bars IS NOT NULL
                                    AND EXTRACT(ISODOW FROM s.trade_date::date) BETWEEN 1 AND 5
                ORDER BY s.eval_ts DESC
                LIMIT %s
                """,
                                (MAX_SNAPSHOT_AGE_S, cutoff, max(1, min(200000, int(limit)))),
            )
            return [
                {"symbol": r[0], "trade_date": r[1], "outcome": r[2],
                                 "up_prob": r[3], "pcr": float(r[4]), "eval_ts": int(r[5]),
                                 "snapshot_date": r[6], "snapshot_ts": int(r[7]),
                                 "direction": r[8], "model_prob": r[9], "model_sha256": r[10],
                                 "feature_schema": r[11], "label_schema": r[12],
                                 "validation_schema": r[13], "hold_bars": r[14]}
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


def summarize_directional_pcr_edge(
    rows: Sequence[Dict[str, Any]], *, target: float = TARGET,
) -> Dict[str, Any]:
    """Score the frozen 2-direction x 5-PCR-bucket discovery family."""
    from core.contract_70_slices import _sidak_family_z
    from core.watcher import wilson_interval

    cells: Dict[Tuple[str, str], Dict[str, Any]] = {
        (direction, label): {
            "n": 0, "wins": 0, "dates": {}, "symbols": set(), "identities": set(),
        }
        for direction in PCR_DIRECTIONS
        for label, _, _ in PCR_BUCKETS
    }
    skipped = 0
    for row in rows:
        direction = str(row.get("direction") or "").upper()
        bucket = _bucket_for(float(row.get("pcr") or -1))
        if direction not in PCR_DIRECTIONS or bucket is None:
            skipped += 1
            continue
        cell = cells[(direction, bucket)]
        cell["n"] += 1
        if str(row.get("outcome") or "").upper() == "WIN":
            cell["wins"] += 1
        date_key = str(row.get("trade_date") or "")
        cell["dates"][date_key] = cell["dates"].get(date_key, 0) + 1
        cell["symbols"].add(str(row.get("symbol") or "").upper())
        identity = (
            str(row.get("model_sha256") or ""),
            str(row.get("feature_schema") or ""),
            str(row.get("label_schema") or ""),
            str(row.get("validation_schema") or ""),
            row.get("hold_bars"),
        )
        if all(value not in (None, "") for value in identity):
            cell["identities"].add(identity)

    family_z = _sidak_family_z(DIRECTIONAL_FAMILY_SIZE)
    out_cells = []
    qualified = []
    for direction in PCR_DIRECTIONS:
        for label, _, _ in PCR_BUCKETS:
            cell = cells[(direction, label)]
            n = int(cell["n"])
            wins = int(cell["wins"])
            win_rate = wins / n if n else None
            standard = wilson_interval(wins, n) if n else None
            corrected = wilson_interval(wins, n, z=family_z) if n else None
            family_low = corrected["low"] if corrected else None
            identity_count = len(cell["identities"])
            identity_homogeneous = identity_count == 1
            qualifies = bool(
                family_low is not None
                and family_low >= target
                and identity_homogeneous
            )
            payload = {
                "direction": direction,
                "pcr_bucket": label,
                "n": n,
                "wins": wins,
                "win_rate": round(win_rate, 4) if win_rate is not None else None,
                "wilson_low": round(standard["low"], 4) if standard else None,
                "family_wilson_low": round(family_low, 4) if family_low is not None else None,
                "qualified_discovery_candidate": qualifies,
                "distinct_dates": len(cell["dates"]),
                "max_date_concentration": (
                    round(max(cell["dates"].values()) / n, 4) if n else None
                ),
                "distinct_symbols": len(cell["symbols"] - {""}),
                "distinct_model_generations": identity_count,
                "identity_homogeneous": identity_homogeneous,
            }
            out_cells.append(payload)
            if qualifies:
                qualified.append({"direction": direction, "pcr_bucket": label})

    total_paired = sum(int(cell["n"]) for cell in cells.values())
    return {
        "cells": out_cells,
        "family_size": DIRECTIONAL_FAMILY_SIZE,
        "family_z": round(family_z, 4),
        "multiple_comparisons_correction": "sidak",
        "total_paired": total_paired,
        "skipped_missing_direction_or_pcr": skipped,
        "qualified_discovery_cells": qualified,
        "status": (
            "QUALIFIED_DISCOVERY_CANDIDATE" if qualified else
            "NO_QUALIFIED_DIRECTIONAL_CELL" if total_paired else "NO_DATA"
        ),
        "note": (
            "A qualified cell must contain one exact model generation and is "
            "eligible only for a new exact-artifact forward registration. It is "
            "not a 70% proof or an activation decision."
        ),
    }


def options_pcr_edge(days: int = 60) -> Dict[str, Any]:
    """Live PCR-edge verdict. Read-only; honest 'insufficient' until data accrues."""
    rows = load_paired_rows(days)
    if rows is None:
        return {"ok": True, "status": "READ_FAILED", "read_only": True}
    ready = options_pcr_readiness(days)
    res = summarize_pcr_edge(rows)
    directional = summarize_directional_pcr_edge(rows)
    sufficient = (directional["total_paired"] >= READY_MIN_PAIRED
                  and ready.get("paired_distinct_days", 0) >= READY_MIN_DAYS)
    return {
        "ok": True,
        "read_only": True,
        "days": int(days),
        "ts": int(time.time()),
        "sufficient_data": sufficient,
        "status": (
            directional["status"] if sufficient else "INSUFFICIENT_DATA"
        ),
        "readiness": ready,
        "result": res,
        "directional_result": directional,
        "note": (
            "Verdict is provisional until sufficient_data=true "
            f"(>= {READY_MIN_PAIRED} paired obs across >= {READY_MIN_DAYS} days). "
            "EXPIRED counts as a non-win. Direction x PCR is a frozen 10-cell "
            "Sidak-corrected discovery family; any candidate still requires a "
            "new exact-artifact fixed-50 forward proof. No gate is loosened."
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
            cur.execute(
                """
                SELECT COUNT(*), COUNT(DISTINCT trade_date)
                FROM ghost_shadow_outcomes
                WHERE eval_ts >= %s
                  AND outcome IN ('WIN','LOSS','EXPIRED')
                  AND direction IN ('UP','DOWN')
                  AND model_sha256 IS NOT NULL
                  AND feature_schema IS NOT NULL
                  AND label_schema IS NOT NULL
                  AND validation_schema IS NOT NULL
                  AND hold_bars IS NOT NULL
                  AND EXTRACT(ISODOW FROM trade_date::date) BETWEEN 1 AND 5
                """,
                (cutoff,),
            )
            eligible_outcomes, eligible_days = cur.fetchone()
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    paired = load_paired_rows(days) or []
    paired_days = len({str(row.get("trade_date")) for row in paired})
    return {
        "ok": True,
        "total_snapshot_rows": int(total or 0),
        "distinct_days": int(days_ct or 0),
        "paired_distinct_days": paired_days,
        "distinct_symbols": int(syms or 0),
        "recent_days": recent,
        "paired_with_outcomes": len(paired),
        "eligible_outcomes": int(eligible_outcomes or 0),
        "eligible_distinct_days": int(eligible_days or 0),
        "pairing_coverage": (
            round(len(paired) / int(eligible_outcomes), 4)
            if eligible_outcomes else None
        ),
        "ready_to_test": bool(
            len(paired) >= READY_MIN_PAIRED and paired_days >= READY_MIN_DAYS
        ),
        "need": {
            "min_paired": READY_MIN_PAIRED, "min_days": READY_MIN_DAYS,
            "paired_so_far": len(paired), "days_so_far": paired_days,
        },
    }
