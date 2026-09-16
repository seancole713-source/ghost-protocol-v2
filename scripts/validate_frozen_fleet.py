"""Evaluate a frozen fleet snapshot without production or network writes.

Run in a fresh process with a declared manifest and a hash-bound bars file.
Uses the real candidate trainer, including peer pools and purged validation.
This is exploratory qualification, never an accuracy or activation endpoint.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
from statistics import NormalDist


def family_lower_bound(wins: int, n: int, family_size: int, alpha: float = 0.05) -> float:
    from core.binomial_stats import wilson_lower_bound

    if any(type(value) is not int for value in (wins, n, family_size)):
        raise ValueError("Evidence counts must be integers")
    if not 0 <= wins <= n or n <= 0 or family_size <= 0 or not 0 < alpha < 1:
        raise ValueError("Invalid family evidence")
    z = NormalDist().inv_cdf(1 - alpha / (2 * family_size))
    return float(wilson_lower_bound(wins, n, z))


def load_snapshot(manifest_path: Path, bars_path: Path):
    manifest = json.loads(manifest_path.read_text())
    raw = bars_path.read_bytes()
    if manifest.get("snapshot_status") != "COMPLETE":
        raise ValueError("Snapshot is not complete")
    if hashlib.sha256(raw).hexdigest() != manifest.get("snapshot_sha256"):
        raise ValueError("Snapshot hash mismatch")
    universe, directions = manifest["universe"], manifest["directions"]
    if len(set(universe)) != len(universe) or directions != ["UP", "DOWN"]:
        raise ValueError("Invalid declared family")
    if not universe or manifest["family_size"] != len(universe) * len(directions):
        raise ValueError("Family size mismatch")
    return manifest, json.loads(raw)


def run(manifest_path: Path, bars_path: Path, output: Path) -> dict:
    import sys
    from functools import lru_cache
    from unittest.mock import patch

    if "core.engine_config" in sys.modules:
        raise RuntimeError("Run the audit in a fresh interpreter")
    manifest, source = load_snapshot(manifest_path, bars_path)
    if output.exists():
        raise FileExistsError("Never overwrite an existing experiment")
    output.mkdir(parents=True)
    (output / "manifest.json").write_bytes(manifest_path.read_bytes())
    for key in list(os.environ):
        if key.startswith("V3_") or key in (
            "GHOST_ACCURACY_CONTRACT", "PEER_SYMBOLS", "SECTOR_PROXY",
            "MIN_TRAIN_ROWS", "MIN_BACKTEST_BARS", "STOCK_SYMBOLS",
        ):
            os.environ.pop(key, None)
    os.environ.update(manifest["config"])
    os.environ.update(OMP_NUM_THREADS="2", OPENBLAS_NUM_THREADS="2", MKL_NUM_THREADS="2")
    from core import db, signal_engine as engine
    from core.precision_gate import precision_target, validate_fire_proof
    from core.research_training import train_research_candidate
    from threadpoolctl import threadpool_limits
    import requests
    import psycopg2

    if precision_target() < 0.70 or manifest["evaluation_target"] != 0.70:
        raise ValueError("This declared audit requires the 70% target")
    expected = manifest["latest_required_session"]
    snapshot, quality = {}, {}
    for symbol, rows in source.items():
        normalized = engine._normalize_daily_ohlcv(rows) if rows else None
        warnings = []
        if not normalized:
            warnings.append("missing_or_invalid_bars")
        elif normalized[-1]["ts"][:10] != expected:
            warnings.append("latest_session_missing")
        else:
            snapshot[symbol] = normalized
            if any(abs(b["close"] / a["close"] - 1) > 0.40
                   for a, b in zip(normalized, normalized[1:])):
                warnings.append("large_daily_move_requires_corporate_action_audit")
        quality[symbol] = warnings
    if manifest["proxy"] not in snapshot:
        raise ValueError("Required sector context unavailable")
    feature_order = engine._active_feature_cols()
    calculable = set(engine._calculate_features(snapshot[manifest["proxy"]][-121:]))
    if set(feature_order) - calculable - {"sector_rel_strength"}:
        raise ValueError("Snapshot cannot supply every enabled feature")
    import importlib.metadata
    (output / "runtime.json").write_text(json.dumps({
        "feature_schema": engine._v3_feature_schema(),
        "label_schema": engine._v3_label_schema(),
        "validation_schema": engine._v3_validation_schema(),
        "feature_order": feature_order,
        "versions": {name: importlib.metadata.version(name)
                     for name in ("numpy", "scikit-learn", "xgboost")},
    }, indent=2))
    (output / "data_quality.json").write_text(json.dumps(quality, indent=2, sort_keys=True))
    original_backtest = engine.backtest_symbol

    @lru_cache(maxsize=None)
    def backtest(symbol, asset_type):
        return original_backtest(symbol, asset_type)

    def fetch(symbol, asset_type, period=None, interval="1d"):
        if interval != "1d":
            raise ValueError("Only the declared daily snapshot is available")
        return snapshot.get(symbol)

    def forbidden(*args, **kwargs):
        raise RuntimeError("Offline validation attempted network/database access")

    results = []
    with patch.object(engine, "_fetch_ohlcv", side_effect=fetch), \
            patch.object(engine, "backtest_symbol", side_effect=backtest), \
            patch.object(db, "db_conn", side_effect=forbidden), \
            patch.object(psycopg2, "connect", side_effect=forbidden), \
            patch.object(requests.sessions.Session, "request", side_effect=forbidden), \
            threadpool_limits(limits=2):
        for symbol in manifest["universe"]:
            for direction in manifest["directions"]:
                row = {"symbol": symbol, "direction": direction,
                       "data_warnings": quality.get(symbol, ["missing_bars"]),
                       "candidate_qualified": False}
                try:
                    candidate = train_research_candidate(symbol, direction, include_failed=True)
                    if candidate is None:
                        labeled = backtest(symbol, "stock")[0 if direction == "UP" else 1]
                        row["n_samples"] = len(labeled)
                        row["status"] = "TRAINING_INCOMPLETE" if len(labeled) >= 50 else "NO_CANDIDATE"
                    else:
                        detail = candidate.get("detail") or {}
                        proof = candidate.get("precision_gate") or {}
                        gate = proof.get("gate") or {}
                        support, wins = gate.get("effective_support", 0), gate.get("effective_wins", 0)
                        lower = family_lower_bound(wins, support, manifest["family_size"],
                                                   manifest["family_alpha"]) if support else 0.0
                        row.update(
                            status="EVALUATED", training_passed=candidate.get("training_passed") is True,
                            n_samples=detail.get("n_samples"),
                            holdout={key: detail.get(key) for key in (
                                "holdout_acc", "edge", "wf_acc_mean", "wf_edge_mean",
                                "wf_fold_count", "fail_reason")},
                            gate_brier=(detail.get("calibration") or {}).get("gate_brier"),
                            precision_gate=proof, family_wilson_low=lower,
                        )
                        row["candidate_qualified"] = bool(
                            row["training_passed"] and validate_fire_proof(proof)
                            and candidate.get("contract_compatible") is True
                            and math.isfinite(lower) and lower >= 0.70
                            and not row["data_warnings"]
                        )
                        if row["candidate_qualified"]:
                            (output / f"candidate_{symbol}_{direction}.json").write_text(
                                json.dumps(candidate, default=str))
                except Exception as exc:
                    row.update(status="ERROR", error=f"{type(exc).__name__}: {exc}")
                results.append(row)
                with (output / "results.jsonl").open("a") as stream:
                    stream.write(json.dumps(row, default=str) + "\n")
                print(json.dumps({key: row.get(key) for key in (
                    "symbol", "direction", "status", "training_passed", "candidate_qualified")}), flush=True)

    report = {"family_size": manifest["family_size"], "results_count": len(results),
              "evaluated": sum(row["status"] == "EVALUATED" for row in results),
              "training_passed": sum(row.get("training_passed", False) for row in results),
              "qualified": sum(row["candidate_qualified"] for row in results),
              "errors": sum(row["status"] in {"ERROR", "TRAINING_INCOMPLETE"} for row in results),
              "accuracy_proven": False, "production_models_written": False}
    (output / "summary.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--bars", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    run(args.manifest, args.bars, args.output)
