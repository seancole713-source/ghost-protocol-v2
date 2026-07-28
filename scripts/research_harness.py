"""scripts/research_harness.py — Pre-registered experiment runner for Ghost Protocol research program.

Each experiment is pre-registered before running, outputs a standardized JSON
result, and appends to the research log. No cherry-picking possible — every
run is recorded, pass or fail.

Usage:
  python scripts/research_harness.py --config geometry_sweep_v1
  python scripts/research_harness.py --config ensemble_test_v1
  python scripts/research_harness.py --list  # show registered experiments
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ── Pre-registration ──────────────────────────────────────────────────

REGISTRY_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "research_registry.json")
LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "research_log.md")


def load_registry() -> Dict[str, Any]:
    if os.path.exists(REGISTRY_PATH):
        with open(REGISTRY_PATH) as f:
            return json.load(f)
    return {"experiments": {}, "created_at": datetime.now(timezone.utc).isoformat()}


def save_registry(reg: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(REGISTRY_PATH), exist_ok=True)
    with open(REGISTRY_PATH, "w") as f:
        json.dump(reg, f, indent=2)


def preregister(name: str, description: str, hypothesis: str,
                config: Dict[str, Any], metrics: List[str]) -> Dict[str, Any]:
    """Pre-register an experiment before running it. Returns the registration."""
    reg = load_registry()
    if name in reg["experiments"]:
        existing = reg["experiments"][name]
        if existing.get("status") == "completed":
            print(f"Experiment '{name}' already completed. Use --force to re-run.")
            return existing
    entry = {
        "name": name,
        "description": description,
        "hypothesis": hypothesis,
        "config": config,
        "target_metrics": metrics,
        "status": "registered",
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "started_at": None,
        "completed_at": None,
        "result": None,
    }
    reg["experiments"][name] = entry
    save_registry(reg)
    return entry


def mark_started(name: str) -> None:
    reg = load_registry()
    if name in reg["experiments"]:
        reg["experiments"][name]["status"] = "running"
        reg["experiments"][name]["started_at"] = datetime.now(timezone.utc).isoformat()
        save_registry(reg)


def mark_completed(name: str, result: Dict[str, Any]) -> None:
    reg = load_registry()
    if name in reg["experiments"]:
        reg["experiments"][name]["status"] = "completed"
        reg["experiments"][name]["completed_at"] = datetime.now(timezone.utc).isoformat()
        reg["experiments"][name]["result"] = result
        save_registry(reg)
    _append_log(name, result)


def _append_log(name: str, result: Dict[str, Any]) -> None:
    """Append a human-readable entry to the research log."""
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    reg = load_registry()
    exp = reg["experiments"].get(name, {})
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"\n## {name} — {ts}\n",
        f"**Status**: {result.get('status', 'unknown')}\n",
        f"**Hypothesis**: {exp.get('hypothesis', '?')}\n",
        f"**Config**: {json.dumps(exp.get('config', {}))}\n",
        f"**Duration**: {result.get('duration_s', '?')}s\n",
    ]
    if result.get("best"):
        b = result["best"]
        lines.append(f"**Best geometry**: target_scale={b.get('target_scale')}, "
                     f"stop_mult={b.get('stop_mult')}, "
                     f"wilson_low={b.get('pooled_wilson_low')}, "
                     f"serve_pass={b.get('serve_pass')}\n")
    if result.get("summary"):
        lines.append(f"**Summary**: {result['summary']}\n")
    lines.append("\n---\n")
    with open(LOG_PATH, "a") as f:
        f.writelines(lines)


# ── Experiment runners ────────────────────────────────────────────────

def run_geometry_sweep(config: Dict[str, Any]) -> Dict[str, Any]:
    """Run an expanded geometry grid sweep.

    Config keys:
      - symbols: list of symbols (default: 24-symbol set)
      - target_scales: list of target vol multipliers
      - stop_mults: list of stop vol multipliers
      - hold_bars: list of hold bar counts (default: [3])
      - target_precision: float (default: 0.70)
      - ensemble: bool (default: False)
    """
    t0 = time.time()
    symbols = config.get("symbols", ["WOLF"])
    target_scales = config.get("target_scales", [1.0])
    stop_mults = config.get("stop_mults", [0.65, 0.85, 1.0, 1.2])
    hold_bars_list = config.get("hold_bars", [3])
    target_precision = config.get("target_precision", 0.70)
    use_ensemble = config.get("ensemble", False)

    # Set ensemble mode
    if use_ensemble:
        os.environ["V3_ENSEMBLE"] = "stacking"
    else:
        os.environ["V3_ENSEMBLE"] = "off"

    # Import after env is set
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "geometry_grid_sweep",
        os.path.join(os.path.dirname(__file__), "geometry_grid_sweep.py"),
    )
    _gmod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_gmod)
    _patch_fetch_cache = _gmod._patch_fetch_cache
    _patch_target_scale = _gmod._patch_target_scale
    run_symbol = _gmod.run_symbol
    ev_stats = _gmod.ev_stats
    from core.precision_gate import select_global_threshold, wilson_lower_bound
    from core.signal_engine import _active_feature_cols

    _patch_fetch_cache()
    cols = _active_feature_cols()

    results = []
    combos = []
    for hb in hold_bars_list:
        os.environ["V3_LABEL_HOLD_BARS"] = str(hb)
        for ts in target_scales:
            for sm in stop_mults:
                combos.append((ts, sm, hb))

    print(f"Geometry sweep: {len(combos)} configs × {len(symbols)} symbols "
          f"(features={len(cols)}, ensemble={use_ensemble})", flush=True)

    for ts, sm, hb in combos:
        os.environ["V3_STOP_VOL_MULT"] = str(sm)
        os.environ["V3_LABEL_HOLD_BARS"] = str(hb)
        _patch_target_scale(ts)
        target_pct = 0.02 * ts
        stop_pct = target_pct * sm
        pooled_p, pooled_y, pooled_ts = [], [], []
        serve_pass_ct = 0
        nsym = 0
        for sym in symbols:
            try:
                r = run_symbol(sym, cols)
            except Exception as exc:
                print(f"  {sym} ERROR {str(exc)[:80]}", flush=True)
                continue
            if r is None:
                continue
            nsym += 1
            pooled_p += r["gate_probs"]
            pooled_y += r["gate_labels"]
            pooled_ts += r["gate_timestamps"]
            if r.get("serve_pass"):
                serve_pass_ct += 1

        if not pooled_y:
            results.append({
                "target_scale": ts, "stop_mult": sm, "hold_bars": hb,
                "target_pct": round(target_pct, 4),
                "stop_pct": round(stop_pct, 4),
                "symbols": nsym, "serve_pass": serve_pass_ct,
                "pooled_n": 0, "pooled_ok": False,
                "pooled_wilson_low": None, "error": "no_gate_data",
            })
            continue

        g = select_global_threshold(pooled_p, pooled_y, target_precision,
                                    timestamps=pooled_ts)
        ev, be = ev_stats(
            g.get("precision") or 0, target_pct, stop_pct,
        )
        row = {
            "target_scale": ts, "stop_mult": sm, "hold_bars": hb,
            "target_pct": round(target_pct, 4),
            "stop_pct": round(stop_pct, 4),
            "symbols": nsym, "serve_pass": serve_pass_ct,
            "pooled_n": len(pooled_y),
            "pooled_ok": bool(g.get("ok")),
            "pooled_thr": g.get("threshold"),
            "pooled_precision": g.get("precision"),
            "pooled_support": g.get("support"),
            "pooled_wilson_low": g.get("wilson_low"),
            "pooled_wilson_high": g.get("wilson_high"),
            "ev_per_trade_pct": ev,
            "break_even_win_rate": be,
        }
        results.append(row)
        status = "PASS" if row["pooled_ok"] else "FAIL"
        print(f"  ts={ts} sm={sm} hb={hb} {status} "
              f"n={row['pooled_n']} wl={row['pooled_wilson_low']} "
              f"serve={serve_pass_ct}/{nsym}", flush=True)

    # Find best by Wilson low
    valid = [r for r in results if r["pooled_wilson_low"] is not None]
    best = max(valid, key=lambda r: r["pooled_wilson_low"]) if valid else None
    passing = [r for r in results if r["pooled_ok"]]

    duration = round(time.time() - t0, 1)
    return {
        "status": "completed",
        "duration_s": duration,
        "total_configs": len(results),
        "passing_configs": len(passing),
        "best": best,
        "passing": passing[:10],
        "all_results": results,
        "summary": (
            f"{len(passing)}/{len(results)} configs pass Wilson-low ≥ "
            f"{target_precision}. Best: ts={best['target_scale']}, "
            f"sm={best['stop_mult']}, hb={best['hold_bars']}, "
            f"wl={best['pooled_wilson_low']}, "
            f"serve={best['serve_pass']}/{best['symbols']}"
            if best else "No valid results"
        ),
    }


def run_ensemble_test(config: Dict[str, Any]) -> Dict[str, Any]:
    """Compare XGBoost-only vs stacking ensemble on identical data."""
    t0 = time.time()
    symbols = config.get("symbols", ["WOLF"])
    target_scales = config.get("target_scales", [1.0])
    stop_mults = config.get("stop_mults", [0.65, 0.85, 1.0, 1.2])
    hold_bars = config.get("hold_bars", [3])
    target_precision = config.get("target_precision", 0.70)

    # Run with ensemble OFF
    os.environ["V3_ENSEMBLE"] = "off"
    baseline = run_geometry_sweep({
        "symbols": symbols, "target_scales": target_scales,
        "stop_mults": stop_mults, "hold_bars": hold_bars,
        "target_precision": target_precision, "ensemble": False,
    })

    # Run with ensemble ON
    os.environ["V3_ENSEMBLE"] = "stacking"
    ensemble = run_geometry_sweep({
        "symbols": symbols, "target_scales": target_scales,
        "stop_mults": stop_mults, "hold_bars": hold_bars,
        "target_precision": target_precision, "ensemble": True,
    })

    duration = round(time.time() - t0, 1)
    b_best = baseline.get("best", {})
    e_best = ensemble.get("best", {})

    return {
        "status": "completed",
        "duration_s": duration,
        "baseline": {
            "best_wilson_low": b_best.get("pooled_wilson_low"),
            "passing_configs": baseline.get("passing_configs", 0),
            "total_configs": baseline.get("total_configs", 0),
        },
        "ensemble": {
            "best_wilson_low": e_best.get("pooled_wilson_low"),
            "passing_configs": ensemble.get("passing_configs", 0),
            "total_configs": ensemble.get("total_configs", 0),
        },
        "ensemble_improvement": (
            round((e_best.get("pooled_wilson_low") or 0) -
                  (b_best.get("pooled_wilson_low") or 0), 4)
        ),
        "summary": (
            f"Baseline best WL={b_best.get('pooled_wilson_low')}, "
            f"Ensemble best WL={e_best.get('pooled_wilson_low')}, "
            f"Δ={round((e_best.get('pooled_wilson_low') or 0) - (b_best.get('pooled_wilson_low') or 0), 4)}"
        ),
    }


# ── CLI ───────────────────────────────────────────────────────────────

EXPERIMENTS = {
    "geometry_sweep_v1": {
        "description": "Expanded geometry sweep: 4 target scales × 5 stop mults × 3 hold bars × 24 symbols",
        "hypothesis": "A broader sweep of target/stop/hold-bar combinations will find at least one config with Wilson-low ≥ 0.70 and serve_pass > 0.",
        "config": {
            "symbols": ["WOLF", "AI", "AMC", "ARCT", "ARDT", "BB", "BMBL", "LCID",
                        "XPO", "ITRI", "CLNE", "PLUG", "SNAP", "GME", "NOK", "OPK",
                        "YMM", "PLTK", "CVNA", "SABR", "BILL", "HOOD", "ABCL", "DUOL"],
            "target_scales": [0.75, 1.0, 1.25, 1.5],
            "stop_mults": [0.4, 0.5, 0.65, 0.85, 1.0],
            "hold_bars": [3, 5, 7],
            "target_precision": 0.70,
            "ensemble": False,
        },
        "metrics": ["pooled_wilson_low", "serve_pass", "pooled_ok", "ev_per_trade_pct"],
        "runner": run_geometry_sweep,
    },
    "ensemble_test_v1": {
        "description": "XGBoost vs stacking ensemble comparison on geometry sweep",
        "hypothesis": "Stacking ensemble (XGBoost + RandomForest → LogisticRegression) will improve Wilson-low by ≥ 0.02 over single XGBoost.",
        "config": {
            "symbols": ["WOLF", "AI", "AMC", "ARCT", "ARDT", "BB", "BMBL", "LCID",
                        "XPO", "ITRI", "CLNE", "PLUG", "SNAP", "GME", "NOK", "OPK",
                        "YMM", "PLTK", "CVNA", "SABR", "BILL", "HOOD", "ABCL", "DUOL"],
            "target_scales": [0.75, 1.0, 1.25, 1.5],
            "stop_mults": [0.4, 0.5, 0.65, 0.85, 1.0],
            "hold_bars": [3, 5, 7],
            "target_precision": 0.70,
        },
        "metrics": ["ensemble_improvement", "baseline_best_wilson_low", "ensemble_best_wilson_low"],
        "runner": run_ensemble_test,
    },
    "universe_focus_v1": {
        "description": "Per-symbol backtest ranking to identify predictable subset",
        "hypothesis": "A focused universe of top-10 symbols by per-symbol Wilson-low will outperform the full 24-symbol set.",
        "config": {
            "symbols": ["WOLF", "AI", "AMC", "ARCT", "ARDT", "BB", "BMBL", "LCID",
                        "XPO", "ITRI", "CLNE", "PLUG", "SNAP", "GME", "NOK", "OPK",
                        "YMM", "PLTK", "CVNA", "SABR", "BILL", "HOOD", "ABCL", "DUOL"],
            "target_scales": [1.0],
            "stop_mults": [0.65],
            "hold_bars": [3],
            "target_precision": 0.70,
        },
        "metrics": ["per_symbol_wilson_low", "top10_pooled_wilson_low", "full_pooled_wilson_low"],
        "runner": None,  # custom runner
    },
}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Ghost Protocol Research Harness")
    parser.add_argument("--config", type=str, help="Experiment name to run")
    parser.add_argument("--list", action="store_true", help="List registered experiments")
    parser.add_argument("--force", action="store_true", help="Re-run completed experiments")
    parser.add_argument("--status", action="store_true", help="Show experiment status")
    args = parser.parse_args()

    if args.list:
        print("Registered experiments:")
        for name, exp in EXPERIMENTS.items():
            reg = load_registry()
            status = reg["experiments"].get(name, {}).get("status", "not_registered")
            print(f"  {name}: {status} — {exp['description'][:80]}")
        return

    if args.status:
        reg = load_registry()
        print(f"Research registry: {len(reg['experiments'])} experiments")
        for name, entry in reg["experiments"].items():
            print(f"  {name}: {entry.get('status')} "
                  f"(started={entry.get('started_at', '?')[:19]}, "
                  f"completed={entry.get('completed_at', '?')[:19]})")
        return

    if not args.config:
        print("Usage: python scripts/research_harness.py --config <name>")
        print("Available: " + ", ".join(EXPERIMENTS.keys()))
        return

    if args.config not in EXPERIMENTS:
        print(f"Unknown experiment: {args.config}")
        print("Available: " + ", ".join(EXPERIMENTS.keys()))
        return

    exp = EXPERIMENTS[args.config]
    reg = load_registry()
    existing = reg["experiments"].get(args.config, {})
    if existing.get("status") == "completed" and not args.force:
        print(f"Experiment '{args.config}' already completed.")
        print(f"Result: {json.dumps(existing.get('result', {}).get('summary', '?'), indent=2)}")
        print("Use --force to re-run.")
        return

    # Pre-register
    preregister(
        args.config, exp["description"], exp["hypothesis"],
        exp["config"], exp["metrics"],
    )
    mark_started(args.config)

    print(f"\n{'='*60}")
    print(f"Running: {args.config}")
    print(f"Hypothesis: {exp['hypothesis']}")
    print(f"Config: {json.dumps(exp['config'], indent=2)}")
    print(f"{'='*60}\n")

    runner = exp["runner"]
    if runner is None:
        print(f"No runner defined for {args.config}. Implement custom runner.")
        return

    result = runner(exp["config"])
    mark_completed(args.config, result)

    print(f"\n{'='*60}")
    print(f"Result: {result.get('summary', 'No summary')}")
    print(f"Duration: {result.get('duration_s', '?')}s")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
