"""Contract-70 verdict — pre-registered honesty layer (registered 2026-07-16).

Mirrors the 80%-claim falsification precedent (core/prediction.py
FALSIFICATION_THRESHOLD → ABANDON_80_CLAIM): the falsification AND revival
criteria are written down before the outcome is known, so neither can be
back-fit to the data that decides them.

Read-only: reads the same shadow-outcome evidence the watcher reads and the
same forward registry contract-70 uses. Never modifies gates, thresholds, or
engine behavior — this layer changes what Ghost CLAIMS, never what it FIRES.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("ghost.contract70.verdict")

VERDICT_VERSION = "1.0"
PREREGISTERED_AT = "2026-07-16"

# Pre-registered decision rules. Changing these after evidence accrues is a
# goalpost move — any edit must be ledgered with its own justification.
FALSIFIED_MIN_N = 100          # enough resolved 70+ rows that the CI is tight
FALSIFIED_MAX_WIN_RATE = 0.65  # observed rate still 5+ points under target
FALSIFIED_WILSON_HIGH_BELOW = 0.70  # 95% CI excludes the 70% target entirely
INSUFFICIENT_N_BELOW = 20
REVIVAL_FORWARD_MIN_N = 25     # forward-only rows on a pre-registered slice
REVIVAL_WILSON_LOW = 0.70      # Wilson-proven, not raw-observed


def preregistration() -> Dict[str, Any]:
    """Static, DB-free registration record — safe for the contract endpoint."""
    return {
        "verdict_version": VERDICT_VERSION,
        "preregistered_at": PREREGISTERED_AT,
        "claim_under_test": (
            "The high-confidence bucket (up_prob >= 0.70) wins at a 70%+ rate, "
            "proven by Wilson 95% lower bound — not merely observed."
        ),
        "status_at_registration": "UNPROVEN_AT_CURRENT_DATA",
        "evidence_at_registration": [
            "Offline geometry sweep (12 configs x 24 symbols): no pooled >=70% "
            "precision operating point; serve floors and precision never coexist.",
            "Offline fundamentals sweep (V3_FUNDAMENTAL_FEATURES=1, inputs "
            "validated): serve_pass unchanged, precision unchanged-to-worse.",
            "Offline momentum sweep (6 point-in-time trend features the model "
            "never contained): serve_pass unchanged, precision mostly worse.",
            "Live watcher: win rate flat ~0.55 across all confidence buckets — "
            "the probability ranking does not discriminate above 0.55.",
            "Slice search: 0 of 280 family-corrected (Sidak) hypotheses qualify "
            "at Wilson-low >= 0.70.",
            "Retrain pipeline: ~50 runs over 7 days with ~7 total serve-floor "
            "passes; fail mix is edge==0.0 base-rate riders + sub-60% holdouts.",
        ],
        "falsified_if": {
            "min_n": FALSIFIED_MIN_N,
            "win_rate_below": FALSIFIED_MAX_WIN_RATE,
            "wilson_high_below": FALSIFIED_WILSON_HIGH_BELOW,
            "meaning": (
                "With >=100 resolved 70+ rows, observed rate under 65%, and the "
                "95% CI excluding 70%, the claim is FALSIFIED at current data — "
                "same shape as the pre-registered 80%-claim gate."
            ),
        },
        "revived_if": {
            "forward_min_n": REVIVAL_FORWARD_MIN_N,
            "wilson_low_at_least": REVIVAL_WILSON_LOW,
            "meaning": (
                "A pre-registered forward slice/universe (contract_70_registry) "
                "reaches Wilson-low >= 0.70 on forward-only outcomes; OR a new "
                "data-source candidate first clears the offline harness "
                "(serve floors + pooled 70% operating point on "
                "scripts/geometry_grid_sweep.py) and then forward-proves."
            ),
        },
        "non_negotiable": (
            "Gates stay strict regardless of verdict. This layer changes the "
            "claim, never the firing behavior."
        ),
    }


def _load_resolved_rows(days: int, limit: int) -> Optional[List[Dict[str, Any]]]:
    """Same evidence pull as watcher_summary; None means 'could not read'."""
    try:
        from core.db import db_conn
        cutoff = int(time.time()) - max(1, min(365, int(days))) * 86400
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT symbol, eval_ts, up_prob, outcome
                FROM ghost_shadow_outcomes
                WHERE eval_ts >= %s AND outcome IN ('WIN','LOSS','EXPIRED')
                ORDER BY eval_ts DESC
                LIMIT %s
                """,
                (cutoff, max(1, min(20000, int(limit)))),
            )
            return [
                {"symbol": r[0], "eval_ts": r[1], "up_prob": r[2], "outcome": r[3]}
                for r in cur.fetchall()
            ]
    except Exception as e:
        LOGGER.warning("contract-70 verdict evidence read failed: %s", str(e)[:80])
        return None


def _forward_proof_status(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Forward-registry revival check (same flow as watcher_summary);
    never invents a proof."""
    try:
        from core.contract_70_registry import (evaluate_forward,
                                               evaluate_forward_slices,
                                               load_registry)
        reg = load_registry()
        if not reg:
            return {"registered": False, "revival_met": False,
                    "note": "no forward registration exists yet"}
        if reg.get("mode") == "slices" and reg.get("slices"):
            try:
                from core.contract_70_slices import load_resolved_contract_rows_since
                fwd_rows = load_resolved_contract_rows_since(
                    since_ts=int(reg.get("registered_at_ts") or 0), limit=50000)
            except Exception:
                fwd_rows = rows
            fwd = evaluate_forward_slices(
                fwd_rows,
                registered_slices=reg.get("slices") or [],
                registered_at_ts=int(reg.get("registered_at_ts") or 0),
                target=float(reg.get("target") or 0.70))
        else:
            fwd = evaluate_forward(
                rows,
                registered_symbols=reg.get("symbols") or [],
                registered_at_ts=int(reg.get("registered_at_ts") or 0),
                prob_floor=float(reg.get("prob_floor") or 0.70),
                target=float(reg.get("target") or 0.70))
        # Both evaluators return the contract_win_test_status shape:
        # wilson_pass already encodes Wilson-low >= 0.70.
        met = bool(fwd.get("wilson_pass")
                   and int(fwd.get("n") or 0) >= REVIVAL_FORWARD_MIN_N)
        return {"registered": True, "revival_met": met, "forward": fwd}
    except Exception as e:
        return {"registered": None, "revival_met": False,
                "note": "registry read failed: " + str(e)[:80]}


def contract_70_verdict(days: int = 90, limit: int = 20000) -> Dict[str, Any]:
    """Live verdict under the pre-registered rules. Read-only."""
    from core.watcher import contract_win_test_status

    out: Dict[str, Any] = {
        "ok": True,
        "read_only": True,
        "preregistration": preregistration(),
        "days": int(days),
        "ts": int(time.time()),
    }
    rows = _load_resolved_rows(days, limit)
    if rows is None:
        out["status"] = "INSUFFICIENT_EVIDENCE"
        out["reason"] = "shadow outcome evidence unreadable this cycle"
        return out
    # EXPIRED counts in the denominator as a non-win (2026-07-14 correction).
    bucket = [r for r in rows if r.get("up_prob") is not None
              and float(r["up_prob"]) >= 0.70]
    wins = sum(1 for r in bucket if r.get("outcome") == "WIN")
    test = contract_win_test_status(wins=wins, n=len(bucket))
    out["live"] = test
    forward = _forward_proof_status(rows)
    out["forward_proof"] = forward

    n = test["n"]
    wr = test["win_rate"] or 0.0
    wilson = test.get("wilson") or {}
    if test.get("wilson_pass") or forward.get("revival_met"):
        status = "PROVEN"
    elif (n >= FALSIFIED_MIN_N and wr < FALSIFIED_MAX_WIN_RATE
          and float(wilson.get("high") or 1.0) < FALSIFIED_WILSON_HIGH_BELOW):
        status = "FALSIFIED_AT_CURRENT_DATA"
    elif n < INSUFFICIENT_N_BELOW:
        status = "INSUFFICIENT_N"
    else:
        status = "UNPROVEN_AT_CURRENT_DATA"
    out["status"] = status
    out["disclaimer"] = (
        "Verdict changes the claim only; gates and firing behavior are "
        "unaffected. Criteria are pre-registered — see preregistration."
    )
    return out
