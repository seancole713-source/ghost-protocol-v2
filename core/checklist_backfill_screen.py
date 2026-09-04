"""Retrospective screen: does the checklist score separate winners from losers?

The prospective answer is weeks away — checklist snapshots only became
recordable on 2026-09-04 (PR #180 fixed a hold-bars contract mismatch that had
silently rejected every write), so the live calibration cohorts start empty and
fill at the shadow-resolution rate.

This gets a first answer today instead, by replaying the checklist against
shadow outcomes that have ALREADY resolved. That is legitimate here only
because collect_evidence is genuinely point-in-time: it drops any record whose
source_ts or observation_ts postdates asof_ts, and every underlying source
enforces its own cutoff independently (get_earnings_surprise refuses reports
filed later, get_fundamentals drops later EPS/revenue, fetch_recent_8k drops
later filings, recent_events_for_symbol is already bounded).

WHAT THIS IS NOT. It is a SCREEN, not proof:

  * A replay can only be as point-in-time as its sources. The gates above are
    real and layered, but a backfill is where lookahead hides, so a positive
    result here must still be confirmed prospectively before anything is built
    on it.
  * Evidence stores hold what was ingested. Sparse historical news or filings
    make boxes read "unknown", which is also true prospectively, so the bias is
    at least consistent — but it is a bias.

Its job is to kill a dead idea cheaply. A flat spread here means the checklist
carries no signal and weeks of building should not happen. A strong spread is a
lead worth confirming, nothing more.

Writes NOTHING to the checklist ledger. Pure computation over resolved rows,
kept entirely out of the live calibration cohorts, so it cannot contaminate the
prospective record it exists to pre-empt.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("ghost.checklist_screen")

SCREEN_VERSION = "retrospective_replay_v1"
_STATE_KEY = "checklist_backfill_screen_v1"

# Bounded so one pass cannot run away. The heavy sources (EDGAR, earnings) are
# cached PER SYMBOL, and the watchlist is 107 symbols, so cost is ~one fetch per
# symbol per source regardless of row count — the rows themselves are cheap.
DEFAULT_LIMIT = 600


def _resolved_rows(limit: int) -> List[Dict[str, Any]]:
    """Shadow outcomes with a known win/loss and a usable issue timestamp."""
    from core.db import db_conn

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT symbol, direction, eval_ts, outcome "
            "FROM ghost_shadow_outcomes "
            "WHERE outcome IN ('WIN','LOSS') "
            "AND direction IN ('UP','DOWN') "
            "AND eval_ts IS NOT NULL AND eval_ts > 0 "
            "ORDER BY eval_ts DESC LIMIT %s",
            (int(limit),),
        )
        return [
            {"symbol": r[0], "direction": r[1], "eval_ts": int(r[2]), "outcome": r[3]}
            for r in cur.fetchall()
        ]


def run_screen(limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
    """Replay the checklist over resolved shadow rows and calibrate the result."""
    from core.catalyst_checklist import CHECKLIST_VERSION, evaluate_checklist
    from core.checklist_calibration import build_calibration
    from core.checklist_evidence import collect_evidence

    started = time.time()
    rows = _resolved_rows(limit)

    samples: Dict[str, List[Dict[str, Any]]] = {"UP": [], "DOWN": []}
    errors = 0
    for row in rows:
        try:
            evidence = collect_evidence(row["symbol"], asof_ts=row["eval_ts"])
            report = evaluate_checklist(row["symbol"], row["direction"], evidence)
            score = report.get("score_pct")
            if score is None:
                continue
            samples[row["direction"]].append({
                "score_pct": float(score),
                "won": row["outcome"] == "WIN",
            })
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the screen
            errors += 1
            if errors <= 3:
                LOGGER.warning(
                    "checklist screen row failed (%s): %s",
                    row.get("symbol"), str(exc)[:120],
                )

    out: Dict[str, Any] = {
        "screen_version": SCREEN_VERSION,
        "checklist_version": CHECKLIST_VERSION,
        "computed_at": int(started),
        "duration_s": round(time.time() - started, 1),
        "rows_considered": len(rows),
        "row_errors": errors,
        "evidence": "retrospective_replay",
        "caveat": (
            "Screening only. A replay is as point-in-time as its sources; a "
            "positive result must be confirmed prospectively before anything "
            "is built on it."
        ),
        "directions": {},
    }
    for direction in ("UP", "DOWN"):
        calib = build_calibration(samples[direction])
        populated = [
            {
                "band": b["band"], "n": b["n"], "wins": b["wins"],
                "raw_rate_pct": b["raw_rate_pct"],
                "proven_rate_pct": b["proven_rate_pct"],
                "proven": b["proven"],
            }
            for b in (calib.get("bands") or []) if b.get("n")
        ]
        spread = None
        if len(populated) >= 2:
            spread = round(
                populated[-1]["raw_rate_pct"] - populated[0]["raw_rate_pct"], 2
            )
        overall = None
        if samples[direction]:
            wins = sum(1 for s in samples[direction] if s["won"])
            overall = round(100.0 * wins / len(samples[direction]), 2)

        out["directions"][direction] = {
            "total_samples": calib.get("total_samples", 0),
            "overall_win_rate_pct": overall,
            "populated_bands": len(populated),
            "proven_bands": sum(1 for b in populated if b["proven"]),
            "spread_pp": spread,
            "bands": populated,
        }
    return out


def store_screen(result: Dict[str, Any]) -> None:
    import json

    from core.db import db_conn, ensure_ghost_state

    with db_conn() as conn:
        cur = conn.cursor()
        ensure_ghost_state(cur)
        cur.execute(
            "INSERT INTO ghost_state(key,val) VALUES(%s,%s) "
            "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
            (_STATE_KEY, json.dumps(result)[:200000]),
        )


def cached_screen() -> Optional[Dict[str, Any]]:
    """Last computed screen, or None. Never recomputes — callers are read paths."""
    import json

    from core.db import db_conn

    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT val FROM ghost_state WHERE key=%s", (_STATE_KEY,))
            row = cur.fetchone()
            if not row or not row[0]:
                return None
            return json.loads(row[0])
    except Exception:
        return None


def refresh_screen(limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
    """Compute and persist. Intended for a scheduled job, never a request path:
    the first pass warms one EDGAR/earnings fetch per symbol."""
    result = run_screen(limit=limit)
    store_screen(result)
    up = result["directions"]["UP"]
    down = result["directions"]["DOWN"]
    LOGGER.warning(
        "CHECKLIST SCREEN: UP n=%s spread=%spp | DOWN n=%s spread=%spp "
        "(rows=%s errors=%s %ss)",
        up["total_samples"], up["spread_pp"],
        down["total_samples"], down["spread_pp"],
        result["rows_considered"], result["row_errors"], result["duration_s"],
    )
    return result
