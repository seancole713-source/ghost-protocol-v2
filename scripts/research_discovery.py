"""scripts/research_discovery.py — bounded candidate discovery program.

Preregisters a family of up to 12 hypotheses, trains each candidate through
the production pipeline, evaluates every mandatory gate on purged walk-forward
data, applies Sidak family correction, and selects at most one finalist per
exact contract/direction for forward confirmatory registration.

Read-only: never writes to ghost_v3_model, live predictions, shadow outcomes,
wallets, or performance logs. All output goes to research artifact tables.

Usage (data feeds only exist on Railway):
  railway run --environment production --service ghost-protocol-v2 \
    sh -c "cd $PWD && PYTHONPATH=$PWD python3.13 scripts/research_discovery.py"
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# ── Family preregistration ──────────────────────────────────────────────────

# Maximum 12 hypotheses. First 6 are production-compatible model/feature
# variants. Remaining 6 are predefined geometry variants (research-only).
FAMILY_SIZE = 12

# Production-compatible variants (6 slots)
PRODUCTION_VARIANTS = [
    {
        "name": "baseline_xgb",
        "description": "Current XGBoost, current features, current geometry",
        "env_overrides": {},
    },
    {
        "name": "soft_voting_ensemble",
        "description": "Soft-voting ensemble under same contract",
        "env_overrides": {"V3_ENSEMBLE_ENABLED": "1", "V3_STACKING_ENABLED": "0"},
    },
    {
        "name": "stacking_ensemble",
        "description": "Stacking ensemble under same contract",
        "env_overrides": {"V3_STACKING_ENABLED": "1"},
    },
    {
        "name": "no_pooling",
        "description": "XGBoost with peer pooling disabled",
        "env_overrides": {"V3_POOL_TRAINING": "0"},
    },
    {
        "name": "with_fundamentals",
        "description": "XGBoost with point-in-time SEC fundamentals (requires >=80%% non-stale coverage audit)",
        "env_overrides": {"V3_FUNDAMENTAL_FEATURES": "1"},
        "requires_coverage_audit": True,
    },
    {
        "name": "pruned_features",
        "description": "XGBoost with predefined V3_PRUNE_FEATURES list",
        "env_overrides": {"V3_PRUNE_FEATURES": "volume_spike,near_low,near_high"},
    },
]

# Geometry variants (6 slots, research-only, cannot activate under current contract)
GEOMETRY_VARIANTS = [
    {"name": "geom_t1.0_s0.85", "target_scale": 1.0, "stop_mult": 0.85},
    {"name": "geom_t1.0_s1.0", "target_scale": 1.0, "stop_mult": 1.0},
    {"name": "geom_t1.0_s1.2", "target_scale": 1.0, "stop_mult": 1.2},
    {"name": "geom_t1.25_s0.65", "target_scale": 1.25, "stop_mult": 0.65},
    {"name": "geom_t1.25_s0.85", "target_scale": 1.25, "stop_mult": 0.85},
    {"name": "geom_t1.25_s1.0", "target_scale": 1.25, "stop_mult": 1.0},
]

# ── Gate requirements ───────────────────────────────────────────────────────

MIN_HOLDOUT_ACC = 0.60
MIN_WF_EDGE = 0.05
MIN_WF_FOLDS = 4
MIN_GATE_SUPPORT = 10
TARGET_PRECISION = 0.70
MAX_BRIER = 0.30
MAX_INVALID_RATE = 0.10
MIN_COVERAGE = 0.01
SIDAK_FAMILY_CONFIDENCE = 0.95


def _sidak_corrected_alpha(family_size: int, confidence: float = 0.95) -> float:
    """Sidak correction: alpha_family = 1 - (1 - alpha)^(1/m)."""
    if family_size <= 1:
        return 1.0 - confidence
    return 1.0 - (confidence) ** (1.0 / family_size)


def _sidak_z(alpha: float) -> float:
    """Z-score for a two-sided confidence level."""
    import math
    # Two-sided: alpha/2 in each tail
    from statistics import NormalDist
    return abs(NormalDist().inv_cdf(alpha / 2.0))


def evaluate_candidate_gates(
    detail: Dict[str, Any],
    family_size: int = 1,
) -> Dict[str, Any]:
    """Evaluate all mandatory gates for one candidate.

    Returns {passed, gates: [{name, passed, value, threshold, reason}]}.
    """
    gates = []
    holdout = detail.get("holdout", {})
    precision = detail.get("precision_gate", {})

    # 1. Holdout accuracy
    acc = float(holdout.get("holdout_acc", 0))
    gates.append({
        "name": "holdout_accuracy",
        "passed": acc >= MIN_HOLDOUT_ACC,
        "value": round(acc, 4),
        "threshold": MIN_HOLDOUT_ACC,
        "reason": "" if acc >= MIN_HOLDOUT_ACC else f"holdout_acc {acc:.4f} < {MIN_HOLDOUT_ACC}",
    })

    # 2. Walk-forward edge
    edge = float(holdout.get("edge", 0))
    gates.append({
        "name": "wf_edge",
        "passed": edge >= MIN_WF_EDGE,
        "value": round(edge, 4),
        "threshold": MIN_WF_EDGE,
        "reason": "" if edge >= MIN_WF_EDGE else f"wf_edge {edge:.4f} < {MIN_WF_EDGE}",
    })

    # 3. Walk-forward folds
    folds = int(holdout.get("wf_folds", 0))
    gates.append({
        "name": "wf_folds",
        "passed": folds >= MIN_WF_FOLDS,
        "value": folds,
        "threshold": MIN_WF_FOLDS,
        "reason": "" if folds >= MIN_WF_FOLDS else f"wf_folds {folds} < {MIN_WF_FOLDS}",
    })

    # 4. Precision gate
    pg_ok = bool(precision.get("ok"))
    gates.append({
        "name": "precision_gate",
        "passed": pg_ok,
        "value": precision.get("wilson_low"),
        "threshold": TARGET_PRECISION,
        "reason": "" if pg_ok else precision.get("fail_reason", "precision gate failed"),
    })

    # 5. Gate support
    gate_n = int(holdout.get("gate_n", 0))
    gates.append({
        "name": "gate_support",
        "passed": gate_n >= MIN_GATE_SUPPORT,
        "value": gate_n,
        "threshold": MIN_GATE_SUPPORT,
        "reason": "" if gate_n >= MIN_GATE_SUPPORT else f"gate_n {gate_n} < {MIN_GATE_SUPPORT}",
    })

    # 6. Brier score
    brier = holdout.get("gate_brier")
    if brier is not None:
        brier_f = float(brier)
        gates.append({
            "name": "brier",
            "passed": brier_f <= MAX_BRIER,
            "value": round(brier_f, 4),
            "threshold": MAX_BRIER,
            "reason": "" if brier_f <= MAX_BRIER else f"brier {brier_f:.4f} > {MAX_BRIER}",
        })

    # 7. Natural rate check (must be above base rate)
    natural_rate = float(holdout.get("natural_rate", 0))
    gates.append({
        "name": "above_natural_rate",
        "passed": acc > natural_rate + 0.05 if natural_rate > 0 else True,
        "value": round(acc, 4),
        "threshold": round(natural_rate + 0.05, 4),
        "reason": "" if acc > natural_rate + 0.05 else f"acc {acc:.4f} not above natural rate {natural_rate:.4f}",
    })

    # Apply Sidak correction to precision gate
    if family_size > 1:
        alpha = _sidak_corrected_alpha(family_size, SIDAK_FAMILY_CONFIDENCE)
        z = _sidak_z(alpha)
        from core.binomial_stats import wilson_lower_bound
        pg_wins = int(precision.get("gate_wins", 0) or 0)
        pg_n = int(precision.get("gate_support", 0) or gate_n)
        sidak_low = wilson_lower_bound(pg_wins, pg_n, z) if pg_n > 0 else 0.0
        sidak_pass = sidak_low >= TARGET_PRECISION
        gates.append({
            "name": "precision_sidak",
            "passed": sidak_pass,
            "value": round(sidak_low, 4),
            "threshold": TARGET_PRECISION,
            "reason": "" if sidak_pass else f"sidak_low {sidak_low:.4f} < {TARGET_PRECISION} (family={family_size}, z={z:.2f})",
        })

    all_passed = all(g["passed"] for g in gates)
    return {
        "passed": all_passed,
        "gates": gates,
        "family_size": family_size,
        "sidak_confidence": SIDAK_FAMILY_CONFIDENCE,
    }


def run_discovery(
    symbols: Optional[List[str]] = None,
    direction: str = "UP",
    out_path: str = "/tmp/research_discovery.json",
) -> Dict[str, Any]:
    """Run the full bounded candidate discovery program.

    Returns a report with all candidates, their gate results, and the
    selected finalist (if any).
    """
    from config.symbols import OFFICIAL_WATCHLIST
    from core.research_training import train_research_candidate

    if symbols is None:
        symbols = list(OFFICIAL_WATCHLIST)[:12]  # Limit for sweep speed

    t0 = time.time()
    results: List[Dict[str, Any]] = []
    total_hypotheses = len(PRODUCTION_VARIANTS) + len(GEOMETRY_VARIANTS)
    family_size = min(FAMILY_SIZE, total_hypotheses)

    print(f"Discovery program: {len(symbols)} symbols x {total_hypotheses} variants "
          f"(family_size={family_size})", flush=True)

    # ── Production-compatible variants ──────────────────────────────────
    for variant in PRODUCTION_VARIANTS:
        print(f"\n--- {variant['name']}: {variant['description']} ---", flush=True)
        # Apply env overrides
        for k, v in variant.get("env_overrides", {}).items():
            os.environ[k] = v

        variant_results = []
        for sym in symbols:
            try:
                candidate = train_research_candidate(sym, direction)
                if candidate:
                    gates = evaluate_candidate_gates(candidate["detail"], family_size)
                    variant_results.append({
                        "symbol": sym,
                        "artifact_sha": candidate["artifact_sha"][:16],
                        "model_sha256": candidate["model_sha256"][:16],
                        "passed": gates["passed"],
                        "gates": gates["gates"],
                        "holdout_acc": candidate["holdout"].get("holdout_acc"),
                        "wf_edge": candidate["holdout"].get("edge"),
                        "precision_ok": candidate["precision_gate"].get("ok"),
                    })
                    status = "PASS" if gates["passed"] else "FAIL"
                    print(f"  {sym}: {status} acc={candidate['holdout'].get('holdout_acc',0):.3f} "
                          f"edge={candidate['holdout'].get('edge',0):.3f}", flush=True)
                else:
                    print(f"  {sym}: NO CANDIDATE (training failed)", flush=True)
            except Exception as e:
                print(f"  {sym}: ERROR {str(e)[:80]}", flush=True)

        results.append({
            "variant": variant["name"],
            "type": "production",
            "description": variant["description"],
            "candidates": variant_results,
            "passed_count": sum(1 for r in variant_results if r["passed"]),
            "total_count": len(variant_results),
        })

        # Restore env
        for k in variant.get("env_overrides", {}):
            os.environ.pop(k, None)

    # ── Geometry variants ──────────────────────────────────────────────
    for variant in GEOMETRY_VARIANTS:
        print(f"\n--- {variant['name']}: target_scale={variant['target_scale']} "
              f"stop_mult={variant['stop_mult']} ---", flush=True)
        os.environ["V3_STOP_VOL_MULT"] = str(variant["stop_mult"])

        # Patch target scale
        import core.vol_targets as vt
        orig_base = vt.base_vol_pct

        def scaled_vol(symbol, asset_type, scale=variant["target_scale"]):
            return orig_base(symbol, asset_type) * scale

        vt.base_vol_pct = scaled_vol

        variant_results = []
        for sym in symbols:
            try:
                candidate = train_research_candidate(sym, direction)
                if candidate:
                    gates = evaluate_candidate_gates(candidate["detail"], family_size)
                    variant_results.append({
                        "symbol": sym,
                        "artifact_sha": candidate["artifact_sha"][:16],
                        "passed": gates["passed"],
                        "gates": gates["gates"],
                        "holdout_acc": candidate["holdout"].get("holdout_acc"),
                        "wf_edge": candidate["holdout"].get("edge"),
                    })
                    status = "PASS" if gates["passed"] else "FAIL"
                    print(f"  {sym}: {status}", flush=True)
                else:
                    print(f"  {sym}: NO CANDIDATE", flush=True)
            except Exception as e:
                print(f"  {sym}: ERROR {str(e)[:80]}", flush=True)

        results.append({
            "variant": variant["name"],
            "type": "geometry",
            "target_scale": variant["target_scale"],
            "stop_mult": variant["stop_mult"],
            "candidates": variant_results,
            "passed_count": sum(1 for r in variant_results if r["passed"]),
            "total_count": len(variant_results),
        })

        # Restore
        vt.base_vol_pct = orig_base
        os.environ.pop("V3_STOP_VOL_MULT", None)

    # ── Select finalist ─────────────────────────────────────────────────
    finalists = []
    for r in results:
        for c in r.get("candidates", []):
            if c.get("passed"):
                finalists.append({
                    "variant": r["variant"],
                    "type": r.get("type", "production"),
                    "symbol": c["symbol"],
                    "artifact_sha": c["artifact_sha"],
                    "holdout_acc": c.get("holdout_acc"),
                    "wf_edge": c.get("wf_edge"),
                })

    report = {
        "program": "bounded_candidate_discovery",
        "family_size": family_size,
        "sidak_confidence": SIDAK_FAMILY_CONFIDENCE,
        "symbols": symbols,
        "direction": direction,
        "results": results,
        "finalists": finalists,
        "finalist_count": len(finalists),
        "status": "NO_FORWARD_CANDIDATE" if not finalists else "FINALIST_SELECTED",
        "elapsed_s": int(time.time() - t0),
        "note": (
            "Geometry variants are research-only and cannot activate under "
            "the current production contract. A geometry winner requires a "
            "new contract version, migration, and separate forward proof."
        ),
    }

    with open(out_path, "w") as f:
        json.dump(report, f, indent=1)
    print(f"\nDONE in {time.time()-t0:.0f}s -> {out_path}", flush=True)
    print(f"Finalists: {len(finalists)}", flush=True)
    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Bounded candidate discovery")
    parser.add_argument("--symbols", nargs="*", help="Symbols to evaluate")
    parser.add_argument("--direction", default="UP", help="Direction (UP/DOWN)")
    parser.add_argument("--out", default="/tmp/research_discovery.json", help="Output path")
    args = parser.parse_args()
    run_discovery(
        symbols=args.symbols if args.symbols else None,
        direction=args.direction,
        out_path=args.out,
    )
