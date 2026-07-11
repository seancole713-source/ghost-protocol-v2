"""scripts/geometry_grid_sweep.py — read-only TARGET x STOP geometry grid (PR #163 research).

Mission: find a label geometry (target move %, stop mult, i.e. reward:risk +
base-rate trade-off) where a >=70% OOS precision operating point EXISTS with
POSITIVE EV. The 2026-07-08 audit proved the two endpoints both fail:
  * stop 1.8 (wide): raw win rate inflated, walk-forward edge destroyed,
    "proven" symbols are base-rate riders.
  * stop 0.65 (tight): real edge (+0.22 wf, 12/12 positive) but ~35% base
    rate makes 70% precision unreachable.
This sweeps the middle ground AND scales the target itself (base_vol_pct),
which the audit never varied.

READ-ONLY: in-process env/monkeypatch overrides only. No DB writes, no model
persistence, no Railway variable mutation. Run through railway for data feeds:
  railway run --environment production --service ghost-protocol-v2 \
    sh -c "cd <repo> && PYTHONPATH=<repo> python3.13 scripts/geometry_grid_sweep.py"
"""
from __future__ import annotations

import json
import os
import sys
import time

import numpy as np

_DEFAULT_SYMS = ("WOLF,AI,AMC,ARCT,ARDT,BB,BMBL,LCID,XPO,ITRI,CLNE,PLUG,"
                 "SNAP,GME,NOK,OPK,YMM,PLTK,CVNA,SABR,BILL,HOOD,ABCL,DUOL")
SYMS = [s.strip().upper() for s in
        os.environ.get("GEOM_SYMS", _DEFAULT_SYMS).split(",") if s.strip()]
TARGET = float(os.environ.get("GEOM_TARGET_PRECISION", "0.70"))
# target_scale multiplies base_vol_pct (1.0 -> +2% stock target).
TARGET_SCALES = [float(x) for x in
                 os.environ.get("GEOM_TARGET_SCALES", "1.0,1.25,1.5").split(",")]
# stop = scaled_vol * mult. BE win rate = mult/(1+mult) — the honest knob.
STOP_MULTS = [float(x) for x in
              os.environ.get("GEOM_STOP_MULTS", "0.65,0.85,1.0,1.2").split(",")]
OUT_PATH = os.environ.get("GEOM_SWEEP_OUT", "/tmp/geometry_grid_sweep.json")

_ohlcv_cache: dict = {}


def _patch_fetch_cache():
    """Fetch each symbol's bars ONCE across the whole grid (rate-limit safety)."""
    import core.signal_engine as se
    orig = se._fetch_ohlcv

    def cached(symbol, asset_type, period=None, interval=None, **kw):
        # period=None passes through so V3_OHLCV_PERIOD (e.g. 5y sweeps)
        # keeps working — a hardcoded default here would silently pin 2y.
        key = (symbol, asset_type, period, interval)
        if key not in _ohlcv_cache:
            if interval is None:
                _ohlcv_cache[key] = orig(symbol, asset_type, period=period, **kw)
            else:
                _ohlcv_cache[key] = orig(symbol, asset_type, period=period,
                                         interval=interval, **kw)
        return _ohlcv_cache[key]

    se._fetch_ohlcv = cached


def _patch_target_scale(scale: float):
    """Scale base_vol_pct everywhere it was imported (labels + live mirror)."""
    import core.vol_targets as vt
    if not hasattr(vt, "_orig_base_vol_pct"):
        vt._orig_base_vol_pct = vt.base_vol_pct
    orig = vt._orig_base_vol_pct

    def scaled(symbol, asset_type, _s=scale):
        return orig(symbol, asset_type) * _s

    for mod in list(sys.modules.values()):
        f = getattr(mod, "base_vol_pct", None)
        if f is not None and getattr(f, "__module__", "") in ("core.vol_targets",
                                                              __name__):
            try:
                mod.base_vol_pct = scaled
            except Exception:
                pass
    vt.base_vol_pct = scaled


def run_symbol(sym, cols):
    """Train -> calibrate -> gate exactly like production, minus persistence.
    (Walk-forward skipped for speed — the pooled OOS gate slice is the
    pass/fail evidence for this sweep.)"""
    from xgboost import XGBClassifier

    from core.engine_calibration import (_build_ensemble,
                                         _evaluate_calibration_holdout,
                                         _maybe_calibrate)
    from core.engine_config import (_purged_holdout_bounds, _v3_holdout_slices,
                                    _v3_wf_purge)
    from core.precision_gate import select_fire_threshold
    from core.signal_engine import backtest_symbol
    from core.stacking_ensemble import is_stacking_enabled

    up_rows, _ = backtest_symbol(sym, "stock")
    if len(up_rows) < 120:
        return None
    X = np.array([[r["features"].get(c, 0.0) for c in cols] for r in up_rows])
    y = np.array([r["label"] for r in up_rows])
    n = len(X)
    train_end, calib_end = _v3_holdout_slices(n)
    tf, cf = _purged_holdout_bounds(n, train_end, calib_end, _v3_wf_purge())
    Xtr, ytr = X[:tf], y[:tf]
    Xca, yca = X[train_end:cf], y[train_end:cf]
    Xg, yg = X[calib_end:], y[calib_end:]
    if len(np.unique(ytr)) < 2 or len(Xg) < 10:
        return None
    pos = int(np.sum(ytr))
    neg = len(ytr) - pos
    spw = min(25.0, max(1.0, neg / pos)) if pos > 0 else 1.0
    m = XGBClassifier(n_estimators=200, max_depth=4, learning_rate=0.03,
                      subsample=0.8, colsample_bytree=0.7, min_child_weight=3,
                      scale_pos_weight=spw, eval_metric="logloss",
                      random_state=42)
    m.fit(Xtr, ytr)
    if is_stacking_enabled():
        fm, _ci = _build_ensemble(m, Xtr, ytr, None, Xca, yca)
    else:
        fm, _ci = _maybe_calibrate(m, Xca, yca)
    ho = _evaluate_calibration_holdout(fm, Xg, yg)
    cap = fm.predict_proba(Xca)[:, 1] if len(Xca) else []
    gap = fm.predict_proba(Xg)[:, 1] if len(Xg) else []
    pg = select_fire_threshold(cap, yca, gap, yg, TARGET)
    acc = float(ho["holdout_acc"] or 0)
    edge = float(ho["edge"] or 0)
    return {"gate_probs": [float(p) for p in gap],
            "gate_labels": [int(v) for v in yg],
            "acc": acc, "edge": edge,
            "natural_rate": float(np.mean(y)) if n else None,
            "pg_ok": bool(pg.get("ok")), "pg_thr": pg.get("threshold"),
            # Contract-70 SERVE floors (min_holdout_acc 0.60, min_edge 0.05):
            # a model that fails these is never STORED in prod, so no evals,
            # no shadow evidence — the 2026-07-10 mult-1.2 live retrain failed
            # 0/33 exactly here. Any future config must clear these offline.
            "serve_pass": bool(acc >= 0.60 and edge >= 0.05)}


def ev_stats(precision, target_pct, stop_pct):
    """EV per trade (%) at realized precision under this geometry."""
    ev = precision * target_pct * 100.0 - (1.0 - precision) * stop_pct * 100.0
    be = stop_pct / (target_pct + stop_pct)
    return round(ev, 4), round(be, 4)


def main():
    t0 = time.time()
    _patch_fetch_cache()
    from core.precision_gate import select_global_threshold, wilson_lower_bound
    from core.signal_engine import _active_feature_cols
    cols = _active_feature_cols()
    results = []
    combos = [(ts, sm) for ts in TARGET_SCALES for sm in STOP_MULTS]
    print(f"grid: {len(combos)} geometries x {len(SYMS)} symbols "
          f"(features={len(cols)})", flush=True)
    for ts, sm in combos:
        os.environ["V3_STOP_VOL_MULT"] = str(sm)
        _patch_target_scale(ts)
        target_pct = 0.02 * ts          # generic stock; WOLF slightly wider
        stop_pct = target_pct * sm
        pooled_p, pooled_y = [], []
        per_sym_ok = 0
        serve_pass_ct = 0
        nsym = 0
        for sym in SYMS:
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
            if r["pg_ok"]:
                per_sym_ok += 1
            if r.get("serve_pass"):
                serve_pass_ct += 1
        g = select_global_threshold(pooled_p, pooled_y, TARGET)
        row = {"target_scale": ts, "stop_mult": sm,
               "target_pct": round(target_pct, 4), "stop_pct": round(stop_pct, 4),
               "symbols": nsym, "per_symbol_proven": per_sym_ok,
               "serve_pass": serve_pass_ct,
               "pooled_n": len(pooled_y),
               "pooled_ok": bool(g.get("ok")),
               "pooled_thr": g.get("threshold"),
               "pooled_precision": g.get("precision"),
               "pooled_support": g.get("support"),
               "pooled_wilson_low": g.get("wilson_low"),
               "fail_reason": g.get("fail_reason"),
               "candidate": g.get("candidate")}
        # EV at the proven (or best-candidate) operating point.
        op = g if g.get("ok") else (g.get("candidate") or {})
        prec = op.get("precision")
        if prec:
            ev, be = ev_stats(float(prec), target_pct, stop_pct)
            row["ev_pct_per_trade"] = ev
            row["break_even_wr"] = be
        # Honest fixed-threshold scan for context.
        p = np.array(pooled_p)
        yy = np.array(pooled_y)
        scan = {}
        for thr in (0.55, 0.6, 0.65, 0.7, 0.75, 0.8):
            mask = p >= thr
            sup = int(mask.sum())
            if sup >= 20:
                wins = int(yy[mask].sum())
                scan[str(thr)] = {"precision": round(wins / sup, 4),
                                  "support": sup,
                                  "wilson_low": round(
                                      wilson_lower_bound(wins, sup), 4)}
        row["fixed_thr_scan"] = scan
        results.append(row)
        print(f"[{time.time()-t0:7.0f}s] tgt x{ts} ({target_pct*100:.1f}%) "
              f"stop x{sm} ({stop_pct*100:.2f}%): pooled_n={row['pooled_n']} "
              f"ok={row['pooled_ok']} prec={row['pooled_precision']} "
              f"wl={row['pooled_wilson_low']} ev={row.get('ev_pct_per_trade')} "
              f"per_sym={per_sym_ok}/{nsym}", flush=True)
        with open(OUT_PATH, "w") as f:
            json.dump({"target_precision": TARGET, "symbols": SYMS,
                       "results": results, "elapsed_s": int(time.time() - t0)},
                      f, indent=1)
    print(f"DONE in {time.time()-t0:.0f}s -> {OUT_PATH}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
