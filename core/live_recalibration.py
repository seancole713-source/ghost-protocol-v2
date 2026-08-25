"""core/live_recalibration.py — per-bin live probability recalibration (PR #162).

Ghost's probability calibration is fit once at TRAIN time (Platt/sigmoid +
conformal on a holdout slice). Live outcomes were only ever OBSERVED (the
Watcher's calibration bins) or used to BLOCK (PR #156 overconfidence gate) —
never fed back into the probability itself. Result: the 70+ bucket ran ~79%
predicted / ~48% realized and the system's only response was refusal, which
also froze out any genuinely well-calibrated high-probability pick.

This layer closes the loop. It reads resolved live shadow outcomes
(ghost_shadow_outcomes), bins them on the SAME edges the Watcher reports
(<50, 50-55, 55-60, 60-70, 70+), and shrinks the live probability toward its
bin's realized win rate, weighted by evidence (pseudo-count smoothing):

    adj = (wins + k * p_raw) / (n + k)        k = prior strength

With no evidence (n=0) adj == p_raw (pure prior). As resolved samples grow,
the realized rate dominates. A well-calibrated bin barely moves; an inverted
bin gets pulled down hard — the model finally hears its own scoreboard,
per-bin, instead of an all-or-nothing global block.

Scope rules (mirrors the PR #155/#156 gates):
  * LIVE firing only. Research/shadow probes keep RAW model probabilities —
    the evidence stream this layer feeds on must stay unadjusted, or the
    loop would eat its own output.
  * UP lane only. ghost_shadow_outcomes stores up_prob against the long
    geometry; applying it to the DOWN lane would mix populations.
  * Fail-safe: never raises; on any DB/parse error returns the raw prob
    with applied=False. The PR #156 block remains as the backstop.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

# Same edges as core.watcher.calibration_bins' default spec — keep in sync.
BIN_EDGES: Tuple[Tuple[float, float], ...] = (
    (0.0, 0.5),
    (0.5, 0.55),
    (0.55, 0.6),
    (0.6, 0.7),
    (0.7, 1.01),
)


def enabled() -> bool:
    return (os.getenv("V3_LIVE_RECALIBRATION", "on") or "on").strip().lower() not in (
        "0", "off", "false", "no",
    )


def prior_strength() -> float:
    """Pseudo-count weight of the model's own probability. Default 25:
    a bin needs ~25 resolved live outcomes before the scoreboard outvotes
    the model. Env: V3_LIVE_RECAL_PRIOR_STRENGTH."""
    try:
        return max(1.0, float(os.getenv("V3_LIVE_RECAL_PRIOR_STRENGTH", "25")))
    except Exception:
        return 25.0


def min_bin_samples() -> int:
    """Below this many resolved outcomes in the bin, pass the raw prob
    through untouched (too little evidence to move anything).
    Env: V3_LIVE_RECAL_MIN_BIN_N."""
    try:
        return max(0, int(os.getenv("V3_LIVE_RECAL_MIN_BIN_N", "5")))
    except Exception:
        return 5


def bin_for(prob: float) -> Tuple[float, float]:
    """The (lo, hi) Watcher bin containing this probability."""
    p = max(0.0, min(1.0, float(prob or 0.0)))
    for lo, hi in BIN_EDGES:
        if lo <= p < hi:
            return lo, hi
    return BIN_EDGES[-1]


def recalibrate(prob: float, samples: int, wins: int,
                k: Optional[float] = None) -> Dict[str, Any]:
    """Pure per-bin shrink. Returns raw + adjusted prob with full working.

    adj = (wins + k*p) / (n + k). Monotone in p, bounded in (0, 1), equals p
    at n=0, converges to the realized bin win rate as n → ∞.
    """
    p = max(0.0, min(1.0, float(prob or 0.0)))
    n = max(0, int(samples or 0))
    w = max(0, min(n, int(wins or 0)))
    kk = float(k) if k is not None else prior_strength()
    lo, hi = bin_for(p)
    out: Dict[str, Any] = {
        "applied": False,
        "prob_raw": round(p, 4),
        "prob_adjusted": round(p, 4),
        "bin": [lo, hi],
        "bin_n": n,
        "bin_wins": w,
        "bin_win_rate": round(w / n, 4) if n else None,
        "prior_strength": kk,
        "min_bin_n": min_bin_samples(),
    }
    if n < min_bin_samples():
        out["note"] = "insufficient_bin_evidence"
        return out
    adj = (w + kk * p) / (n + kk)
    # Brake-only contract: live evidence may reduce an overconfident model but
    # may never manufacture a higher probability than the train-calibrated
    # input. Positive shifts remain observable as a no-op.
    adj = min(p, adj)
    out["applied"] = True
    out["prob_adjusted"] = round(max(0.0, min(1.0, adj)), 4)
    out["shift"] = round(out["prob_adjusted"] - out["prob_raw"], 4)
    return out


def _exact_identity(
    *, direction: str, model_sha256: str, feature_schema: str,
    label_schema: str, validation_schema: str, hold_bars: int,
) -> Optional[Tuple[str, str, str, str, str, int]]:
    lane = str(direction or "").upper()
    if lane != "UP":
        return None
    if isinstance(hold_bars, bool):
        return None
    try:
        hold_numeric = float(hold_bars)
    except (TypeError, ValueError, OverflowError):
        return None
    if not hold_numeric.is_integer() or hold_numeric < 1:
        return None
    strings = (model_sha256, feature_schema, label_schema, validation_schema)
    if not all(isinstance(value, str) and value for value in strings):
        return None
    return (lane, *strings, int(hold_numeric))


def live_bin_stats(
    lo: float, hi: float, *, direction: str, model_sha256: str,
    feature_schema: str, label_schema: str, validation_schema: str,
    hold_bars: int,
) -> Tuple[int, int]:
    """Resolved exact-generation (samples, wins) inside one model-prob bin."""
    identity = _exact_identity(
        direction=direction, model_sha256=model_sha256,
        feature_schema=feature_schema, label_schema=label_schema,
        validation_schema=validation_schema, hold_bars=hold_bars,
    )
    if identity is None:
        raise ValueError("recalibration_identity_missing")
    from core.db import db_conn
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*) AS samples,
                   SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) AS wins
            FROM ghost_shadow_outcomes
            WHERE outcome IN ('WIN','LOSS','EXPIRED')
              AND model_prob >= %s AND model_prob < %s
              AND direction=%s AND model_sha256=%s AND feature_schema=%s
              AND label_schema=%s AND validation_schema=%s AND hold_bars=%s
            """,
            (lo, hi, *identity),
        )
        row = cur.fetchone()
    return int((row and row[0]) or 0), int((row and row[1]) or 0)


def live_recalibrated_prob(
    prob: float, *, direction: str, model_sha256: str,
    feature_schema: str, label_schema: str, validation_schema: str,
    hold_bars: int,
) -> Dict[str, Any]:
    """Exact-generation bin lookup + shrink. Never raises."""
    p = float(prob or 0.0)
    if not enabled():
        return {"applied": False, "prob_raw": round(p, 4),
                "prob_adjusted": round(p, 4), "disabled": True}
    if str(direction or "").upper() != "UP":
        return {"applied": False, "prob_raw": round(p, 4),
                "prob_adjusted": round(p, 4), "note": "down_lane_unsupported"}
    identity = _exact_identity(
        direction=direction, model_sha256=model_sha256,
        feature_schema=feature_schema, label_schema=label_schema,
        validation_schema=validation_schema, hold_bars=hold_bars,
    )
    if identity is None:
        return {"applied": False, "prob_raw": round(p, 4),
                "prob_adjusted": round(p, 4), "note": "identity_missing"}
    lo, hi = bin_for(p)
    try:
        n, w = live_bin_stats(
            lo, hi, direction=direction, model_sha256=model_sha256,
            feature_schema=feature_schema, label_schema=label_schema,
            validation_schema=validation_schema, hold_bars=hold_bars,
        )
    except Exception as exc:
        return {"applied": False, "prob_raw": round(p, 4),
                "prob_adjusted": round(p, 4),
                "note": "stats_unavailable", "error": str(exc)[:120]}
    return recalibrate(p, n, w)
