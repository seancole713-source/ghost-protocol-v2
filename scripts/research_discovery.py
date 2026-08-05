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
import time
from typing import Any, Dict, List, Optional

# ── Family preregistration ──────────────────────────────────────────────────

# Maximum 12 hypotheses. First 6 are production-compatible model/feature
# variants. Remaining 6 are predefined geometry variants (research-only).
FAMILY_SIZE = 12

# Production-compatible variants (6 slots)
PRODUCTION_VARIANTS: List[Dict[str, Any]] = [
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
GEOMETRY_VARIANTS: List[Dict[str, Any]] = [
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


def _apply_env(overrides: Dict[str, str]) -> Dict[str, Optional[str]]:
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    return previous


def _restore_env(previous: Dict[str, Optional[str]]) -> None:
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _sidak_corrected_alpha(family_size: int, confidence: float = 0.95) -> float:
    """Sidak correction: alpha_family = 1 - (1 - alpha)^(1/m)."""
    if family_size <= 1:
        return 1.0 - confidence
    return 1.0 - (confidence) ** (1.0 / family_size)


def _sidak_z(alpha: float) -> float:
    """Z-score for a two-sided confidence level."""
    # Two-sided: alpha/2 in each tail
    from statistics import NormalDist
    return abs(NormalDist().inv_cdf(alpha / 2.0))


def evaluate_candidate_gates(
    candidate: Dict[str, Any],
    family_size: int = 1,
) -> Dict[str, Any]:
    """Evaluate all mandatory gates for one candidate.

    Returns {passed, gates: [{name, passed, value, threshold, reason}]}.
    """
    gates = []
    holdout = candidate.get("holdout", {})
    precision = candidate.get("precision_gate", {})
    precision_gate = precision.get("gate", {}) if isinstance(precision, dict) else {}

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
    edge = float(holdout.get("wf_edge_mean", 0))
    gates.append({
        "name": "wf_edge",
        "passed": edge >= MIN_WF_EDGE,
        "value": round(edge, 4),
        "threshold": MIN_WF_EDGE,
        "reason": "" if edge >= MIN_WF_EDGE else f"wf_edge {edge:.4f} < {MIN_WF_EDGE}",
    })

    # 3. Walk-forward folds
    folds = int(holdout.get("wf_fold_count", 0))
    gates.append({
        "name": "wf_folds",
        "passed": folds >= MIN_WF_FOLDS,
        "value": folds,
        "threshold": MIN_WF_FOLDS,
        "reason": "" if folds >= MIN_WF_FOLDS else f"wf_folds {folds} < {MIN_WF_FOLDS}",
    })

    # 4. Precision gate
    from core.precision_gate import validate_fire_proof
    pg_ok = validate_fire_proof(precision)
    gates.append({
        "name": "precision_gate",
        "passed": pg_ok,
        "value": precision_gate.get("wilson_low"),
        "threshold": TARGET_PRECISION,
        "reason": "" if pg_ok else precision.get("fail_reason", "precision gate failed"),
    })

    # 5. Gate support
    gate_n = int(precision_gate.get("support", 0) or 0)
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
        pg_wins = int(precision_gate.get("wins", 0) or 0)
        pg_n = int(precision_gate.get("support", 0) or gate_n)
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


def _gate_value(candidate: Dict[str, Any], gate_name: str) -> float:
    for gate in candidate.get("gates", []):
        if gate.get("name") == gate_name:
            try:
                return float(gate.get("value") or 0.0)
            except (TypeError, ValueError, OverflowError):
                return 0.0
    return 0.0


def select_forward_finalists(
    passing_candidates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Select at most one live-contract finalist by a frozen ranking."""
    eligible = [
        candidate for candidate in passing_candidates
        if candidate.get("type") == "production"
    ]
    return sorted(
        eligible,
        key=lambda candidate: (
            -_gate_value(candidate, "precision_sidak"),
            -_gate_value(candidate, "gate_support"),
            -float(candidate.get("wf_edge") or 0.0),
            -float(candidate.get("holdout_acc") or 0.0),
            str(candidate.get("variant") or ""),
            str(candidate.get("artifact_sha") or ""),
        ),
    )[:1]


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
        symbols = ["WOLF" if "WOLF" in OFFICIAL_WATCHLIST else OFFICIAL_WATCHLIST[0]]
    symbols = sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
    if len(symbols) != 1:
        raise ValueError(
            "bounded discovery preregisters exactly one symbol across 12 hypotheses"
        )
    direction = str(direction or "").strip().upper()
    if direction not in ("UP", "DOWN"):
        raise ValueError("direction must be UP or DOWN")

    t0 = time.time()
    results: List[Dict[str, Any]] = []
    total_hypotheses = len(PRODUCTION_VARIANTS) + len(GEOMETRY_VARIANTS)
    if FAMILY_SIZE != 12 or total_hypotheses != FAMILY_SIZE:
        raise RuntimeError(
            "bounded discovery requires exactly 12 preregistered hypotheses"
        )
    family_size = FAMILY_SIZE

    print(f"Discovery program: {len(symbols)} symbol x {total_hypotheses} variants "
          f"(family_size={family_size})", flush=True)

    # ── Production-compatible variants ──────────────────────────────────
    for variant in PRODUCTION_VARIANTS:
        print(f"\n--- {variant['name']}: {variant['description']} ---", flush=True)
        previous_env = _apply_env(variant.get("env_overrides", {}))

        variant_results: List[Dict[str, Any]] = []
        for sym in symbols:
            try:
                candidate = train_research_candidate(sym, direction)
                if candidate:
                    gates = evaluate_candidate_gates(candidate, family_size)
                    variant_results.append({
                        "symbol": sym,
                        "artifact_sha": candidate["artifact_sha"],
                        "model_sha256": candidate["model_sha256"],
                        "passed": gates["passed"],
                        "gates": gates["gates"],
                        "holdout_acc": candidate["holdout"].get("holdout_acc"),
                        "wf_edge": candidate["holdout"].get("wf_edge_mean"),
                        "precision_ok": candidate["precision_gate"].get("ok"),
                        "_candidate_bundle": candidate,  # full bundle for persistence
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

        _restore_env(previous_env)

    # ── Geometry variants ──────────────────────────────────────────────
    for variant in GEOMETRY_VARIANTS:
        print(f"\n--- {variant['name']}: target_scale={variant['target_scale']} "
              f"stop_mult={variant['stop_mult']} ---", flush=True)
        previous_env = _apply_env({
            "V3_STOP_VOL_MULT": str(variant["stop_mult"]),
        })

        # Patch target scale
        import core.vol_targets as vt
        import core.signal_engine as se
        orig_base = vt.base_vol_pct
        orig_engine_base = se.base_vol_pct

        target_scale = float(variant["target_scale"])

        def scaled_vol(symbol, asset_type, scale: float = target_scale):
            return orig_base(symbol, asset_type) * scale

        vt.base_vol_pct = scaled_vol
        se.base_vol_pct = scaled_vol

        variant_results = []
        for sym in symbols:
            try:
                candidate = train_research_candidate(sym, direction)
                if candidate:
                    gates = evaluate_candidate_gates(candidate, family_size)
                    variant_results.append({
                        "symbol": sym,
                        "artifact_sha": candidate["artifact_sha"],
                        "model_sha256": candidate["model_sha256"],
                        "passed": gates["passed"],
                        "gates": gates["gates"],
                        "holdout_acc": candidate["holdout"].get("holdout_acc"),
                        "wf_edge": candidate["holdout"].get("wf_edge_mean"),
                        "_candidate_bundle": candidate,
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
        se.base_vol_pct = orig_engine_base
        _restore_env(previous_env)

    # ── Select finalist ─────────────────────────────────────────────────
    passing_candidates: List[Dict[str, Any]] = []
    for r in results:
        for c in r.get("candidates", []):
            if c.get("passed"):
                passing_candidates.append({
                    "variant": r["variant"],
                    "type": r.get("type", "production"),
                    "symbol": c["symbol"],
                    "artifact_sha": c["artifact_sha"],
                    "model_sha256": c.get("model_sha256", ""),
                    "holdout_acc": c.get("holdout_acc"),
                    "wf_edge": c.get("wf_edge"),
                    "gates": c.get("gates", []),
                    "_candidate_bundle": c.get("_candidate_bundle"),
                })

    finalists = select_forward_finalists(passing_candidates)

    # ── Persist finalist ───────────────────────────────────────────────
    persisted = 0
    persistence_errors: List[Dict[str, str]] = []
    if finalists:
        from core.research_artifacts import ArtifactMeta, register_artifact
        from core.research_forward import register_forward_experiment
        from core.research_contracts import get_contract
        contract = get_contract("tp_sl_swing", "v1")
        contract_id = contract.contract_id() if contract else "tp_sl_swing/v1"
        for f in finalists:
            try:
                # Use the stored candidate bundle from the sweep — do NOT retrain
                candidate = f.get("_candidate_bundle")
                if not candidate:
                    persistence_errors.append({
                        "symbol": str(f.get("symbol") or ""),
                        "error": "selected_candidate_bundle_missing",
                    })
                    continue
                # Verify the artifact SHA matches what was selected
                if candidate["artifact_sha"] != f["artifact_sha"]:
                    error = (
                        "selected_candidate_sha_mismatch:"
                        f"stored={candidate['artifact_sha'][:16]},"
                        f"selected={f['artifact_sha'][:16]}"
                    )
                    persistence_errors.append({
                        "symbol": str(f.get("symbol") or ""),
                        "error": error,
                    })
                    print(f"  Persist error for {f['symbol']}: {error}", flush=True)
                    continue
                # Register artifact
                meta = ArtifactMeta(
                    artifact_sha=candidate["artifact_sha"],
                    contract_id=contract_id,
                    policy_lineage_id=f"{f['symbol']}/{direction}",
                    policy_lineage_version=1,
                    symbol_scope=(f["symbol"],),
                    output_domain=(str(candidate.get("direction") or direction).upper(),),
                    feature_schema=candidate["feature_schema"],
                    evidence_schema=candidate["label_schema"],
                    validation_schema=candidate["validation_schema"],
                    horizon_bars=candidate["hold_bars"],
                    training_manifest_sha="",
                    calibration_proof=candidate["calibration_proof"],
                    gate_proof=candidate["holdout"] or {},
                    feature_order=candidate["feature_order"],
                    trained_at=candidate["trained_at"],
                )
                register_artifact(meta, payload_bytes=candidate["model_bytes"])
                registration_id = register_forward_experiment(
                    contract_id=contract_id,
                    artifact_sha=candidate["artifact_sha"],
                    direction=direction,
                    threshold=candidate["precision_gate"]["threshold"],
                    symbol_universe=[f["symbol"]],
                    family_size=family_size,
                    family_correction="sidak",
                    selection_evidence={
                        "program": "bounded_candidate_discovery/v2",
                        "hypothesis_count": total_hypotheses,
                        "symbol": f["symbol"],
                        "direction": direction,
                        "selected_variant": f["variant"],
                        "ranking": [
                            "precision_sidak_desc",
                            "gate_support_desc",
                            "wf_edge_desc",
                            "holdout_acc_desc",
                            "variant_asc",
                            "artifact_sha_asc",
                        ],
                        "precision_sidak": _gate_value(f, "precision_sidak"),
                        "gate_support": int(_gate_value(f, "gate_support")),
                    },
                    round_trip_slippage_bps=10.0,
                    round_trip_commission_bps=0.0,
                )
                if registration_id:
                    persisted += 1
                else:
                    persistence_errors.append({
                        "symbol": str(f.get("symbol") or ""),
                        "error": "forward_registration_returned_no_id",
                    })
            except Exception as e:
                error = str(e)[:160]
                persistence_errors.append({
                    "symbol": str(f.get("symbol") or ""),
                    "error": error,
                })
                print(f"  Persist error for {f['symbol']}: {error}", flush=True)

    if persistence_errors:
        discovery_status = "PERSISTENCE_FAILED"
    elif persisted == 0:
        discovery_status = "NO_FORWARD_CANDIDATE"
    else:
        discovery_status = "FINALIST_SELECTED"

    report = {
        "program": "bounded_candidate_discovery",
        "family_size": family_size,
        "sidak_confidence": SIDAK_FAMILY_CONFIDENCE,
        "symbols": symbols,
        "direction": direction,
        "results": results,
        "passing_candidates": passing_candidates,
        "passing_candidate_count": len(passing_candidates),
        "finalists": finalists,
        "finalist_count": len(finalists),
        "persisted": persisted,
        "persistence_errors": persistence_errors,
        "status": discovery_status,
        "elapsed_s": int(time.time() - t0),
        "note": (
            "Geometry variants are research-only and cannot activate under "
            "the current production contract. A geometry winner requires a "
            "new contract version, migration, and separate forward proof."
        ),
    }

    with open(out_path, "w") as output_file:
        json.dump(report, output_file, indent=1)
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
    report = run_discovery(
        symbols=args.symbols if args.symbols else None,
        direction=args.direction,
        out_path=args.out,
    )
    if report["status"] == "PERSISTENCE_FAILED":
        raise SystemExit(1)
