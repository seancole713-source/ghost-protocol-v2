"""scripts/provable_oppoint_sweep.py — read-only 'can Ghost prove 70%?' harness.

Replicates the real train -> calibrate -> precision-gate path per symbol WITHOUT
persisting any model or touching a Railway variable, at two stop geometries, and
pools every symbol's untouched gate-slice OOS predictions to test the same
global 70% operating point the live engine uses. Answers the mission's headline
question with N and a Wilson lower bound.
"""
from __future__ import annotations
import os, sys
import numpy as np

SYMS = ["WOLF","AI","AMC","ARCT","ARDT","BB","BMBL","LCID","XPO","ITRI","CLNE","PLUG",
        "SNAP","GME","NOK","OPK","YMM","PLTK","CVNA","SABR","BILL","HOOD","ABCL","DUOL"]
MULTS = [0.65, 1.8]
TARGET = 0.70


def run_symbol(sym, cols):
    from core.signal_engine import (backtest_symbol, _walk_forward_scores)
    from core.engine_config import _v3_holdout_slices, _purged_holdout_bounds, _v3_wf_purge
    from core.engine_calibration import _maybe_calibrate, _build_ensemble, _evaluate_calibration_holdout
    from core.precision_gate import select_fire_threshold
    from core.stacking_ensemble import is_stacking_enabled
    from xgboost import XGBClassifier

    up_rows,_ = backtest_symbol(sym,"stock")
    if len(up_rows) < 120:
        return None
    X = np.array([[r["features"].get(c,0.0) for c in cols] for r in up_rows])
    y = np.array([r["label"] for r in up_rows])
    n=len(X)
    train_end, calib_end = _v3_holdout_slices(n)
    tf, cf = _purged_holdout_bounds(n, train_end, calib_end, _v3_wf_purge())
    Xtr,ytr = X[:tf], y[:tf]
    Xca,yca = X[train_end:cf], y[train_end:cf]
    Xg,yg = X[calib_end:], y[calib_end:]
    if len(np.unique(ytr))<2 or len(Xg)<10:
        return None
    pos=int(np.sum(ytr)); neg=len(ytr)-pos
    spw=min(25.0,max(1.0,neg/pos)) if pos>0 else 1.0
    m=XGBClassifier(n_estimators=200,max_depth=4,learning_rate=0.03,subsample=0.8,
        colsample_bytree=0.7,min_child_weight=3,scale_pos_weight=spw,
        eval_metric='logloss',random_state=42)
    m.fit(Xtr,ytr)
    if is_stacking_enabled():
        fm,_ci=_build_ensemble(m,Xtr,ytr,None,Xca,yca)
    else:
        fm,_ci=_maybe_calibrate(m,Xca,yca)
    ho=_evaluate_calibration_holdout(fm,Xg,yg)
    wf=_walk_forward_scores(X,y)
    cap=fm.predict_proba(Xca)[:,1] if len(Xca) else []
    gap=fm.predict_proba(Xg)[:,1] if len(Xg) else []
    pg=select_fire_threshold(cap,yca,gap,yg,TARGET)
    return {"gate_probs":[float(p) for p in gap], "gate_labels":[int(v) for v in yg],
            "wf_edge":wf["edge_mean"], "wf_acc":wf["acc_mean"], "acc":ho["holdout_acc"],
            "edge":ho["edge"], "brier":ho.get("gate_brier"), "pg_ok":bool(pg.get("ok")),
            "pg_thr":pg.get("threshold"), "pg_fail":pg.get("fail_reason")}


def main():
    from core.signal_engine import _active_feature_cols
    from core.precision_gate import select_global_threshold, wilson_lower_bound
    cols=_active_feature_cols()
    for mult in MULTS:
        os.environ["V3_STOP_VOL_MULT"]=str(mult)
        print("\n"+"="*100)
        print(f"STOP_VOL_MULT = {mult}   (target precision {TARGET})")
        print("="*100)
        print("{:<6}{:<8}{:<8}{:<9}{:<8}{:<10}{:<12}".format(
            "sym","acc","edge","wf_edge","brier","pg_ok","pg_thr/fail"))
        pooled_p=[]; pooled_y=[]; per_sym_proven=0; nsym=0
        for sym in SYMS:
            r=run_symbol(sym,cols)
            if r is None:
                print(f"{sym:<6} skip"); continue
            nsym+=1
            pooled_p+=r["gate_probs"]; pooled_y+=r["gate_labels"]
            if r["pg_ok"]: per_sym_proven+=1
            print("{:<6}{:<8}{:<8}{:<9}{:<8}{:<10}{:<12}".format(
                sym, round(r["acc"],3), round(r["edge"],3), round(r["wf_edge"],3),
                str(r["brier"]), "YES" if r["pg_ok"] else "no",
                str(r["pg_thr"]) if r["pg_ok"] else str(r["pg_fail"])[:11]))
        # pooled global operating point (what the live engine falls back to)
        g=select_global_threshold(pooled_p, pooled_y, TARGET)
        print("-"*100)
        print(f"symbols evaluated: {nsym}   per-symbol precision-gate PROVEN: {per_sym_proven}")
        print(f"pooled gate-slice OOS samples: {len(pooled_y)}")
        if g.get("ok"):
            print(f"POOLED GLOBAL 70% OPERATING POINT: PROVEN  thr={g['threshold']} "
                  f"precision={g['precision']} support={g['support']} wilson_low={g['wilson_low']}")
        else:
            print(f"POOLED GLOBAL 70% OPERATING POINT: UNPROVEN ({g.get('fail_reason')})")
            c=g.get("candidate")
            if c: print(f"   best candidate: thr={c['threshold']} precision={c['precision']} "
                        f"support={c['support']} wilson_low={c['wilson_low']}")
        # honest 'what win rate CAN we prove' scan on the pooled OOS set
        import numpy as _np
        p=_np.array(pooled_p); yy=_np.array(pooled_y)
        print("   pooled precision at fixed thresholds (raw, not gate-selected):")
        for thr in (0.55,0.6,0.65,0.7,0.75,0.8):
            mask=p>=thr; sup=int(mask.sum()); wins=int(yy[mask].sum())
            if sup>=20:
                prec=wins/sup; wl=wilson_lower_bound(wins,sup)
                print(f"     thr>={thr}: precision={prec:.3f} support={sup} wilson_low={wl:.3f}")

if __name__=="__main__":
    sys.exit(main())
