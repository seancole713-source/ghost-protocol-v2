"""scripts/geometry_edge_sweep.py — read-only stop-geometry edge sweep.

Produced the Fugu 2026-07-08 finding that V3_STOP_VOL_MULT=1.8 collapses
out-of-time model edge (mean wf_edge -0.06, negative for 8/12 symbols) while
0.65 restores it (mean wf_edge +0.22, positive for all 12). Wide stops inflate
the raw win rate but destroy the edge the precision gate needs, which is why
every fleet retrain at 1.8 yields 0 serveable/fireable models.

Usage (data feeds only exist on the box, so run through Railway):
  railway run --environment production --service ghost-protocol-v2 \
    sh -c "cd $PWD && PYTHONPATH=$PWD python3.13 scripts/geometry_edge_sweep.py"

Tests lever #1: does V3_STOP_VOL_MULT=1.8 collapse out-of-time edge vs 0.65?
READ-ONLY: fetches OHLCV via the 5-tier chain, regenerates TP/SL labels at each
candidate stop multiplier IN-PROCESS (os.environ override only, never a Railway
variable), and runs the SAME purged walk-forward validator the gates use.
No DB writes, no model persistence, no env-var mutation.
"""
from __future__ import annotations
import os, sys, time
import numpy as np

SYMS = ["WOLF","AI","AMC","ARCT","ARDT","BB","BMBL","LCID","XPO","ITRI","CLNE","PLUG"]
MULTS = [0.65, 0.9, 1.2, 1.5, 1.8]

def label_counts(rows, hold_bars, vol_pct, mult):
    from core.tp_sl_resolve import simulate_tp_sl_label
    os.environ["V3_STOP_VOL_MULT"] = str(mult)
    w=l=e=0
    margin = hold_bars + 1
    for i in range(len(rows) - margin):
        out = simulate_tp_sl_label(rows, i, hold_bars, vol_pct, "UP")
        if out == "WIN": w+=1
        elif out == "LOSS": l+=1
        else: e+=1
    return w,l,e

def main():
    from core.signal_engine import (backtest_symbol, _walk_forward_scores,
        _active_feature_cols, V3_LABEL_HOLD_BARS, _fetch_ohlcv)
    from core.vol_targets import base_vol_pct
    cols = _active_feature_cols(); hold = V3_LABEL_HOLD_BARS
    print(f"hold_bars={hold} n_feature_cols={len(cols)} symbols={len(SYMS)} mults={MULTS}")
    print("="*120)
    print("{:<6}{:<6}{:<6}{:<7}{:<7}{:<10}{:<8}{:<8}{:<9}{:<10}{:<7}".format(
        "sym","mult","N","winRt","resRt","EVvolU","wf_fld","wf_acc","wf_edge","wf_edgMn","natRt"))
    print("-"*120)
    agg = {m:{"ev":[],"wf_edge":[],"wf_acc":[],"winrt":[],"resrt":[]} for m in MULTS}
    for sym in SYMS:
        rows = _fetch_ohlcv(sym,"stock",period="2y") or []
        if len(rows) < 150:
            print(f"{sym:<6} SKIP only {len(rows)} bars"); continue
        vol = base_vol_pct(sym,"stock")
        for m in MULTS:
            os.environ["V3_STOP_VOL_MULT"] = str(m)
            up_rows,_ = backtest_symbol(sym,"stock")
            if not up_rows:
                print(f"{sym:<6}{m:<6} no rows"); continue
            X = np.array([[r["features"].get(c,0.0) for c in cols] for r in up_rows])
            y = np.array([r["label"] for r in up_rows])
            wf = _walk_forward_scores(X,y)
            w,l,e = label_counts(rows,hold,vol,m)
            resolved=w+l; tot=w+l+e
            winrt=(w/resolved) if resolved else 0.0
            resrt=(resolved/tot) if tot else 0.0
            pw=w/tot if tot else 0.0; pl=l/tot if tot else 0.0
            ev=pw*1.0 - pl*m
            natrt=float(np.mean(y)) if len(y) else 0.0
            print("{:<6}{:<6}{:<6}{:<7}{:<7}{:<10}{:<8}{:<8}{:<9}{:<10}{:<7}".format(
                sym,m,len(y),round(winrt,3),round(resrt,3),round(ev,4),
                wf["fold_count"],round(wf["acc_mean"],3),round(wf["edge_mean"],4),
                round(wf["edge_min"],4),round(natrt,3)))
            agg[m]["ev"].append(ev); agg[m]["wf_edge"].append(wf["edge_mean"])
            agg[m]["wf_acc"].append(wf["acc_mean"]); agg[m]["winrt"].append(winrt)
            agg[m]["resrt"].append(resrt)
        print("-"*120)
    print("\n=== AGGREGATE ACROSS SYMBOLS (mean) ===")
    print("{:<6}{:<12}{:<12}{:<12}{:<12}{:<12}".format(
        "mult","meanWinRt","meanResRt","meanWfEdge","meanWfAcc","meanEV"))
    for m in MULTS:
        a=agg[m]
        if not a["ev"]: continue
        print("{:<6}{:<12}{:<12}{:<12}{:<12}{:<12}".format(
            m, round(float(np.mean(a["winrt"])),3), round(float(np.mean(a["resrt"])),3),
            round(float(np.mean(a["wf_edge"])),4), round(float(np.mean(a["wf_acc"])),3),
            round(float(np.mean(a["ev"])),4)))

if __name__ == "__main__":
    sys.exit(main())
