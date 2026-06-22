#!/usr/bin/env python3
"""
scripts/calibrate_confidence_slope.py — Empirical confidence slope calibration.

P1-1 (audit): Replaces the heuristic ×4.0 confidence multiplier with an
empirically derived slope from resolved v3.2 picks.

Usage:
  python3 scripts/calibrate_confidence_slope.py [--apply]

  --dry-run (default): Print the recommended CONFIDENCE_SLOPE value.
  --apply: Write the calibrated value to ghost_state so the engine picks it up
           on the next cycle without a redeploy.

Method:
  1. Collect all (up_prob, outcome) pairs from resolved v3.2 picks
  2. Bin by up_prob decile
  3. Compute realized win rate per bin
  4. Fit linear regression: realized_wr = a + b × up_prob
  5. The slope b is the empirical CONFIDENCE_SLOPE
  6. Also compute the intercept for the confidence formula:
     conf = clamp(accuracy + (up_prob - min_p) × slope, floor, ceiling)
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List, Tuple

import numpy as np


def _db_conn():
    """Lazy DB connection for standalone script use."""
    import psycopg2
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)
    return psycopg2.connect(dsn)


def collect_resolved_picks() -> List[Tuple[float, int]]:
    """Return list of (up_prob, is_win) for resolved v3.2 picks."""
    conn = _db_conn()
    cur = conn.cursor()
    # Get picks with scores JSONB containing up_prob
    cur.execute("""
        SELECT scores, outcome
        FROM predictions
        WHERE id >= 223438
          AND outcome IN ('WIN', 'LOSS')
          AND scores IS NOT NULL
        ORDER BY predicted_at ASC NULLS LAST, id ASC
    """)
    rows = cur.fetchall()
    conn.close()

    pairs = []
    for scores_raw, outcome in rows:
        sc = scores_raw
        if isinstance(sc, str):
            try:
                sc = json.loads(sc)
            except Exception:
                continue
        if not isinstance(sc, dict):
            continue
        up_prob = sc.get("up_prob")
        if up_prob is None:
            continue
        is_win = 1 if outcome == "WIN" else 0
        pairs.append((float(up_prob), is_win))

    return pairs


def calibrate(pairs: List[Tuple[float, int]]) -> Dict[str, Any]:
    """Fit linear regression: realized_wr = a + b × up_prob."""
    if len(pairs) < 20:
        return {
            "ok": False,
            "error": f"Need ≥20 resolved picks with up_prob; have {len(pairs)}",
            "samples": len(pairs),
        }

    up_probs = np.array([p[0] for p in pairs])
    outcomes = np.array([p[1] for p in pairs])

    # Decile binning for visualization
    bins = [0.0, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 1.0]
    bin_stats = []
    for i in range(len(bins) - 1):
        lo, hi = bins[i], bins[i + 1]
        mask = (up_probs >= lo) & (up_probs < hi)
        n = int(mask.sum())
        if n == 0:
            continue
        wr = float(outcomes[mask].mean())
        bin_stats.append({
            "bin": f"{lo:.2f}-{hi:.2f}",
            "samples": n,
            "win_rate": round(wr, 4),
            "mean_up_prob": round(float(up_probs[mask].mean()), 4),
        })

    # Linear regression: realized_wr = intercept + slope × up_prob
    X = np.vstack([np.ones(len(up_probs)), up_probs]).T
    coeffs, residuals, rank, singular = np.linalg.lstsq(X, outcomes, rcond=None)
    intercept = float(coeffs[0])
    slope = float(coeffs[1])

    # R²
    predicted = intercept + slope * up_probs
    ss_res = float(np.sum((outcomes - predicted) ** 2))
    ss_tot = float(np.sum((outcomes - outcomes.mean()) ** 2))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    # Current heuristic: slope = 4.0
    # Recommended: use the empirical slope, clamped to [1.0, 8.0] for safety
    recommended = max(1.0, min(8.0, round(slope, 2)))

    return {
        "ok": True,
        "samples": len(pairs),
        "wins": int(outcomes.sum()),
        "losses": len(pairs) - int(outcomes.sum()),
        "natural_wr": round(float(outcomes.mean()), 4),
        "intercept": round(intercept, 4),
        "slope_raw": round(slope, 4),
        "slope_recommended": recommended,
        "r_squared": round(r_squared, 4),
        "current_heuristic": 4.0,
        "improvement_note": (
            f"Empirical slope {recommended} vs heuristic 4.0. "
            f"R²={r_squared:.3f}. "
            + ("GOOD fit — replace heuristic." if r_squared > 0.3 else "WEAK fit — keep heuristic for now.")
        ),
        "decile_bins": bin_stats,
    }


def apply_slope(slope: float) -> Dict[str, Any]:
    """Write the calibrated slope to ghost_state for runtime pickup."""
    conn = _db_conn()
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
    cur.execute(
        "INSERT INTO ghost_state(key,val) VALUES('confidence_slope_calibrated',%s) "
        "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
        (str(slope),),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "applied": slope, "note": "Set CONFIDENCE_SLOPE env var or restart to pick up"}


def main():
    apply_flag = "--apply" in sys.argv

    print("=" * 60)
    print("Ghost Protocol — Confidence Slope Calibration")
    print("=" * 60)
    print()

    pairs = collect_resolved_picks()
    print(f"Collected {len(pairs)} resolved picks with up_prob scores")
    if not pairs:
        print("No data — need resolved v3.2 picks with scores JSONB.")
        return

    result = calibrate(pairs)
    if not result.get("ok"):
        print(f"ERROR: {result.get('error')}")
        return

    print(f"Natural win rate: {result['natural_wr']:.1%}")
    print(f"Wins: {result['wins']}  Losses: {result['losses']}")
    print()
    print("Decile bins:")
    for b in result["decile_bins"]:
        bar = "█" * int(b["win_rate"] * 20)
        print(f"  up_prob {b['bin']:>8}: {b['win_rate']:.1%} WR ({b['samples']:>3} samples) {bar}")
    print()
    print(f"Linear fit: realized_wr = {result['intercept']:.4f} + {result['slope_raw']:.4f} × up_prob")
    print(f"R² = {result['r_squared']:.4f}")
    print(f"Current heuristic slope: {result['current_heuristic']}")
    print(f"Recommended slope:       {result['slope_recommended']}")
    print()
    print(result["improvement_note"])

    if apply_flag:
        applied = apply_slope(result["slope_recommended"])
        print()
        print(f"✅ Applied slope {applied['applied']} to ghost_state.confidence_slope_calibrated")
        print("   Set CONFIDENCE_SLOPE env var or restart to pick up.")
    else:
        print()
        print("Run with --apply to write the calibrated slope to ghost_state.")


if __name__ == "__main__":
    main()
