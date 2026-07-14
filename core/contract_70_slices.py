"""core/contract_70_slices.py - honest search for a real 70+ win-test slice.

The live calibration proves the single model probability (``up_prob``) is
NON-discriminative above ~0.55: realized win rate is roughly flat (~0.56) across
the 55-60, 60-70, and 70+ buckets while the claimed probability climbs to 0.78.
That means NO threshold on ``up_prob`` alone can isolate a genuine 70% pocket -
the 70+ bucket already IS the top bucket and it only runs ~57%.

If a truthful 70+ result exists at all, it lives in a CONDITIONAL slice of the
resolved outcomes (e.g. a particular symbol, market regime, or probability band,
or a combination). This module searches those slices on ALREADY-RESOLVED
contract outcomes and reports which - if any - clear a Wilson-PROVEN 0.70 bar.

Design guarantees (so this can never manufacture a fake 70+):

* It is read-only. It never fires a trade, never loosens a gate, never mutates a
  model, and never writes prediction/broker/wallet state.
* It scores the SAME TP/SL win test the contract already uses - it only groups
  the existing WIN/LOSS rows by dimensions; it does not re-label outcomes.
* Qualification uses the Wilson LOWER bound, not the raw rate, so a lucky small
  sample cannot pass.
* Finding a qualified slice here is NOT a 70+ claim. It is a CANDIDATE to be
  pre-registered for a forward-only proof (core.contract_70_registry); the 70+
  is proven only when future, out-of-sample outcomes clear the bar.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.watcher import wilson_interval

# Same bin edges the Watcher reports so a probability-band slice lines up exactly
# with the calibration table the operator already sees.
_PROB_BUCKETS: Tuple[Tuple[str, float, float], ...] = (
    ("<50", 0.0, 0.50),
    ("50-55", 0.50, 0.55),
    ("55-60", 0.55, 0.60),
    ("60-70", 0.60, 0.70),
    ("70+", 0.70, 1.01),
)


def up_prob_bucket(p: Any) -> Optional[str]:
    """Label a probability with the Watcher's bin edges. None if not numeric."""
    try:
        pf = float(p)
    except Exception:
        return None
    for label, lo, hi in _PROB_BUCKETS:
        if pf >= lo and (pf < hi or (hi >= 1.0 and pf <= hi)):
            return label
    return None


def _dim_value(row: Dict[str, Any], dim: str) -> Any:
    if dim == "up_prob_bucket":
        return up_prob_bucket(row.get("up_prob"))
    v = row.get(dim)
    if v is None:
        return None
    if dim == "symbol":
        return str(v).upper()
    if dim == "regime_label":
        return str(v)
    return v


def summarize_slices(
    rows: Sequence[Dict[str, Any]],
    *,
    dims: Sequence[str],
    target: float = 0.70,
) -> List[Dict[str, Any]]:
    """Group resolved WIN/LOSS rows by ``dims`` and score each slice.

    Each input row needs an ``outcome`` of ``WIN``/``LOSS`` plus whatever
    dimension fields ``dims`` references (``symbol``, ``regime_label``, or the
    synthetic ``up_prob_bucket``). Rows missing any requested dimension value are
    skipped for that grouping so a slice can never be diluted by ``None`` keys.
    Pure/read-only. Sorted strongest-first by Wilson lower bound.
    """
    target_f = max(0.0, min(1.0, float(target or 0.70)))
    grouped: Dict[Tuple[Any, ...], Dict[str, int]] = {}
    for r in rows:
        outcome = str(r.get("outcome") or "").upper()
        if outcome not in ("WIN", "LOSS"):
            continue
        key_parts: List[Any] = []
        ok = True
        for d in dims:
            val = _dim_value(r, d)
            if val is None or val == "":
                ok = False
                break
            key_parts.append(val)
        if not ok:
            continue
        g = grouped.setdefault(tuple(key_parts), {"n": 0, "wins": 0})
        g["n"] += 1
        if outcome == "WIN":
            g["wins"] += 1

    out: List[Dict[str, Any]] = []
    for key, g in grouped.items():
        n = int(g["n"])
        wins = int(g["wins"])
        ci = wilson_interval(wins, n)
        wr = wins / n if n else None
        out.append({
            "dims": list(dims),
            "key": {d: k for d, k in zip(dims, key)},
            "n": n,
            "wins": wins,
            "win_rate": round(wr, 4) if wr is not None else None,
            "wilson_low": ci["low"],
            "wilson_high": ci["high"],
            "raw_pass": bool(wr is not None and wr >= target_f),
            "wilson_pass": bool(n > 0 and ci["low"] >= target_f),
        })
    out.sort(key=lambda s: (s["wilson_low"], s["n"], s["win_rate"] or 0.0), reverse=True)
    return out


# Dimension sets searched by default. Ordered simplest-first; a single-dimension
# proven slice is preferable to a narrow multi-dimension one (less overfit risk).
DEFAULT_DIMENSION_SETS: Tuple[Tuple[str, ...], ...] = (
    ("symbol",),
    ("regime_label",),
    ("up_prob_bucket",),
    ("symbol", "regime_label"),
    ("regime_label", "up_prob_bucket"),
    ("symbol", "up_prob_bucket"),
)


def find_qualified_slices(
    rows: Sequence[Dict[str, Any]],
    *,
    dimension_sets: Sequence[Sequence[str]] = DEFAULT_DIMENSION_SETS,
    target: float = 0.70,
    min_n: int = 8,
    min_wilson_low: float = 0.70,
) -> Dict[str, Any]:
    """Search every dimension set; return slices whose Wilson low clears the bar.

    Returns both the qualified slices (candidates to pre-register for a forward
    proof) and, for transparency, the single strongest slice per dimension set
    even when nothing qualifies - so the readout honestly shows how far the best
    conditional pocket is from a proven 70+.
    """
    target_f = max(0.0, min(1.0, float(target or 0.70)))
    min_n_i = max(1, int(min_n))
    min_wl = max(0.0, min(1.0, float(min_wilson_low)))

    qualified: List[Dict[str, Any]] = []
    best_per_dim: List[Dict[str, Any]] = []
    for dims in dimension_sets:
        slices = summarize_slices(rows, dims=dims, target=target_f)
        eligible = [s for s in slices if s["n"] >= min_n_i]
        for s in eligible:
            if s["wilson_low"] >= min_wl:
                qualified.append(s)
        if eligible:
            best = eligible[0]
        elif slices:
            best = dict(slices[0])
            best["under_min_sample"] = True
        else:
            best = None
        if best is not None:
            best_per_dim.append(best)

    qualified.sort(key=lambda s: (s["wilson_low"], s["n"]), reverse=True)
    return {
        "target": target_f,
        "min_n": min_n_i,
        "min_wilson_low": min_wl,
        "resolved_n": sum(1 for r in rows if str(r.get("outcome") or "").upper() in ("WIN", "LOSS")),
        "qualified_count": len(qualified),
        "qualified": qualified,
        "best_per_dimension": best_per_dim,
        "status": "qualified_slice_found" if qualified else "no_qualified_slice",
        "note": (
            "A qualified slice is a CANDIDATE to pre-register for a forward-only "
            "proof; it is not a 70+ claim. 70+ is proven only when future "
            "out-of-sample outcomes for the frozen slice clear the Wilson bar."
        ),
    }


def load_resolved_contract_rows(*, days: int = 120, limit: int = 20000) -> List[Dict[str, Any]]:
    """Read resolved contract outcomes with their conditioning signal attached.

    Joins ``ghost_shadow_outcomes`` (the table the 70+ contract scores) back to
    the eval that produced it so each outcome carries its market ``regime_label``
    and probability band. Prefers the self-describing ``regime_label`` column on
    the outcome row (durable) and falls back to the eval table for older rows.
    Read-only; returns [] on any error so callers degrade gracefully.
    """
    import time as _time
    from core.db import db_conn

    cutoff = int(_time.time()) - max(1, min(365, int(days))) * 86400
    lim = max(1, min(50000, int(limit)))
    rows: List[Dict[str, Any]] = []
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT so.symbol, so.eval_ts, so.up_prob, so.outcome,
                       COALESCE(
                         so.regime_label,
                         (SELECT pe.regime_label FROM ghost_perf_symbol_evals pe
                          WHERE pe.symbol = so.symbol AND pe.eval_ts = so.eval_ts
                            AND pe.regime_label IS NOT NULL
                          LIMIT 1)
                       ) AS regime_label
                FROM ghost_shadow_outcomes so
                WHERE so.eval_ts >= %s AND so.outcome IN ('WIN','LOSS')
                ORDER BY so.eval_ts DESC
                LIMIT %s
                """,
                (cutoff, lim),
            )
            for r in cur.fetchall():
                rows.append({
                    "symbol": r[0],
                    "eval_ts": r[1],
                    "up_prob": r[2],
                    "outcome": r[3],
                    "regime_label": r[4],
                })
    except Exception:
        return []
    return rows


def contract_70_slice_search(
    *,
    days: int = 120,
    target: float = 0.70,
    min_n: int = 8,
    min_wilson_low: float = 0.70,
    limit: int = 20000,
) -> Dict[str, Any]:
    """Live, read-only 70+ slice search over resolved contract outcomes."""
    try:
        from core.accuracy_contract import active_contract
        target = float(active_contract().target_win_rate)
    except Exception:
        pass
    rows = load_resolved_contract_rows(days=days, limit=limit)
    out = find_qualified_slices(
        rows,
        target=target,
        min_n=min_n,
        min_wilson_low=min_wilson_low,
    )
    out["days"] = max(1, min(365, int(days)))
    out["read_only"] = True
    return out
