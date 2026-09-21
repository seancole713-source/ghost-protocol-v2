"""scripts/feature_group_sweep.py — read-only sweep of the DARK feature groups.

Ghost trains on 31 price-derived columns. Thirty-one more are built and
switched off, every one of them defaulting to "off" in core/engine_config.py:

    V3_NEWS_FEATURES              3 cols   news_sentiment / bullish / bearish
    V3_MACRO_FEATURES             8 cols   VIX, yield spread, fed rate, DXY, SPY
    V3_CROSS_SECTIONAL_FEATURES  11 cols   rsi/volume/momentum/short-float RANK
                                           of this symbol against every other
    V3_INTRADAY_FEATURES          6 cols   VWAP deviation, gap fill, hourly mom
    V3_OPTIONS_FEATURES           3 cols   put/call ratio, skew

2026-09-09 made the cost concrete. ROIV ran +18.8% on a mosliciguat Phase 2b
readout; AMGN -10% and NVS -14% on a Novartis trial failure read across the
Lp(a) class; the tape fell 628 points on oil near $99 and Fed repricing. Every
one of those is news, sector or macro. A price-only model watched all three
happen as a wiggle.

WHY A SWEEP AND NOT A SWITCH. On 2026-09-06 this project spent a cycle acting
on scripts/geometry_edge_sweep.py's docstring, which asserted a July result
that no longer reproduced -- 0.65 came back the WORST stop multiplier, not the
best. Enabling a feature group also changes feature_schema, invalidating all
257 stored models and forcing a full retrain. Measure first.

THE COVERAGE COLUMN IS THE POINT. A group can leave walk-forward edge
unchanged for two completely different reasons: the signal genuinely does not
help, or the columns are all zero because the history was never available to
compute them (news retention, options history, intraday bars). Those look
identical in an edge number and are opposite problems. cov% reports the
fraction of training rows where at least one column in the group is non-zero:

    cov ~0    the feature is not being COMPUTED -- fix the data path, the
              edge number means nothing
    cov high  the feature is real and measured -- an unchanged edge is a
              genuine null result and the group should stay off

READ-ONLY: sets V3_*_FEATURES in this process only, reads OHLCV, runs the same
purged walk-forward validator the gates use. No DB writes, no model
persistence, no Railway variable mutation.
"""
from __future__ import annotations

import os
import sys

import numpy as np

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

SYMS = ["WOLF", "AI", "AMC", "ARCT", "ARDT", "BB", "BMBL", "LCID", "XPO",
        "ITRI", "CLNE", "PLUG"]

# Each group alone against the baseline, then everything at once. One variable
# at a time is the only way to attribute a change to a group.
CONFIGS = [
    ("baseline", {}),
    ("+news", {"V3_NEWS_FEATURES": "1"}),
    ("+macro", {"V3_MACRO_FEATURES": "1"}),
    ("+cross_sec", {"V3_CROSS_SECTIONAL_FEATURES": "1"}),
    ("+intraday", {"V3_INTRADAY_FEATURES": "1"}),
    ("+options", {"V3_OPTIONS_FEATURES": "1"}),
    ("+sector", {"V3_SECTOR_FEATURE": "1"}),
    ("ALL", {"V3_NEWS_FEATURES": "1", "V3_MACRO_FEATURES": "1",
             "V3_CROSS_SECTIONAL_FEATURES": "1", "V3_INTRADAY_FEATURES": "1",
             "V3_OPTIONS_FEATURES": "1", "V3_SECTOR_FEATURE": "1"}),
]

_TOGGLES = ["V3_NEWS_FEATURES", "V3_MACRO_FEATURES", "V3_CROSS_SECTIONAL_FEATURES",
            "V3_INTRADAY_FEATURES", "V3_OPTIONS_FEATURES", "V3_SECTOR_FEATURE"]


def _apply(env: dict) -> None:
    """Set exactly this config, clearing every other toggle first."""
    for name in _TOGGLES:
        os.environ.pop(name, None)
    for name, val in env.items():
        os.environ[name] = val


def _group_cols(baseline_cols, cols):
    """Columns this config added on top of the baseline."""
    return [c for c in cols if c not in set(baseline_cols)]


def _coverage(rows, added_cols) -> float:
    """Fraction of training rows where at least one ADDED column is non-zero.

    Near zero means the columns exist in the vector and carry nothing -- the
    model is being handed a block of constants and the edge number below is
    measuring noise, not the feature.
    """
    if not added_cols or not rows:
        return 0.0
    live = 0
    for r in rows:
        feats = r.get("features") or {}
        if any(abs(float(feats.get(c, 0.0) or 0.0)) > 1e-12 for c in added_cols):
            live += 1
    return live / len(rows)


def main() -> int:
    from core.signal_engine import (
        _active_feature_cols, _fetch_ohlcv, _walk_forward_scores,
        backtest_symbol,
    )

    _apply({})
    baseline_cols = list(_active_feature_cols())
    print(f"baseline n_cols={len(baseline_cols)} symbols={len(SYMS)} "
          f"configs={len(CONFIGS)}")
    print("=" * 104)
    print("{:<12}{:<8}{:<8}{:<10}{:<11}{:<11}{:<10}{:<8}".format(
        "config", "n_cols", "added", "cov%", "wf_edge", "wf_acc", "holdout", "syms"))
    print("-" * 104)

    # Bars are config-independent; fetch once so eight configs do not re-pull
    # the same two years of history eight times.
    bars = {}
    for sym in SYMS:
        rows = _fetch_ohlcv(sym, "stock", period="2y") or []
        if len(rows) >= 150:
            bars[sym] = rows

    results = []
    for label, env in CONFIGS:
        _apply(env)
        cols = list(_active_feature_cols())
        added = _group_cols(baseline_cols, cols)
        edges, accs, holds, covs = [], [], [], []
        for sym in bars:
            try:
                up_rows, _ = backtest_symbol(sym, "stock")
            except Exception as exc:  # noqa: BLE001 - one symbol must not end the sweep
                print(f"  {label} {sym}: backtest failed ({type(exc).__name__})")
                continue
            if not up_rows:
                continue
            X = np.array([[r["features"].get(c, 0.0) for c in cols] for r in up_rows])
            y = np.array([r["label"] for r in up_rows])
            if len(set(y.tolist())) < 2:
                continue
            wf = _walk_forward_scores(X, y)
            edges.append(wf["edge_mean"])
            accs.append(wf["acc_mean"])
            holds.append(float(np.mean(y)))
            covs.append(_coverage(up_rows, added))
        if not edges:
            print(f"{label:<12}no usable symbols")
            continue
        row = {
            "config": label,
            "n_cols": len(cols),
            "added": len(added),
            "cov": float(np.mean(covs)) if covs else 0.0,
            "wf_edge": float(np.mean(edges)),
            "wf_acc": float(np.mean(accs)),
            "holdout": float(np.mean(holds)),
            "syms": len(edges),
        }
        results.append(row)
        print("{:<12}{:<8}{:<8}{:<10}{:<11}{:<11}{:<10}{:<8}".format(
            row["config"], row["n_cols"], row["added"],
            round(row["cov"] * 100, 1), round(row["wf_edge"], 4),
            round(row["wf_acc"], 4), round(row["holdout"], 3), row["syms"]))

    print("-" * 104)
    base = next((r for r in results if r["config"] == "baseline"), None)
    if base:
        print("\n=== DELTA vs BASELINE (wf_edge; positive = the group helps) ===")
        for r in results:
            if r["config"] == "baseline":
                continue
            delta = r["wf_edge"] - base["wf_edge"]
            if r["added"] == 0:
                verdict = "NO COLUMNS ADDED — toggle had no effect"
            elif r["cov"] < 0.01:
                verdict = "NOT COMPUTED — columns all zero, edge is meaningless"
            elif delta > 0.01:
                verdict = "HELPS"
            elif delta < -0.01:
                verdict = "HURTS"
            else:
                verdict = "flat — genuine null, leave it off"
            print(f"  {r['config']:<12} {delta:+.4f}   cov={r['cov']*100:5.1f}%   {verdict}")
    _apply({})
    return 0


if __name__ == "__main__":
    sys.exit(main())
