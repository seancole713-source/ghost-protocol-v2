"""scripts/momentum_feature_sweep.py — read-only momentum-features sweep.

Third lever of the 2026-07-16 edge hunt. Levers one (label geometry) and two
(PR #165 SEC fundamentals) were both measured null on the same harness:
no config cleared the contract-70 serve floors at scale and no pooled >=70%
precision operating point exists (see /tmp/geometry_grid_sweep*.json runs).

This lever injects the momentum/trend signals that core/momentum.py computes
for the momentum shadow brains — built precisely because the base engine is
mean-reversion-blind — into the model feature vector, which has NEVER
contained them. Features are computed per training bar from the SAME hist
window backtest_symbol already passes to _calculate_features (the slice ends
at bar i, so they are point-in-time by construction — no look-ahead):

  mom_breakout_20d    close >= 0.99 * 20d high
  mom_uptrend_struct  sma20 > sma50
  mom_above_sma20     close > sma20
  mom_ret20_pct       20-day return %
  mom_vol_ratio       5d/20d volume ratio
  mom_score           0-6 signal count (incl. adx>=20 trending)

READ-ONLY: in-process monkeypatch only (core.signal_engine._calculate_features
+ _active_feature_cols), then delegates to scripts/geometry_grid_sweep.main().
No DB writes, no model persistence, no Railway variable mutation. Run through
railway for data feeds, same as the other sweeps:
  railway run -s ghost-protocol-v2 -e production -- sh -c \
    "PYTHONPATH=<repo> <repo>/.venv/bin/python3 scripts/momentum_feature_sweep.py"
"""
from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

MOM_COLS = ["mom_breakout_20d", "mom_uptrend_struct", "mom_above_sma20",
            "mom_ret20_pct", "mom_vol_ratio", "mom_score"]


def _momentum_features(hist) -> dict:
    """Same signal math as core/momentum.py::_compute, but over the
    point-in-time hist window instead of the latest bar."""
    closes = [r["close"] for r in hist]
    if len(closes) < 60 or closes[-1] <= 0:
        return {k: 0.0 for k in MOM_COLS}
    from core.engine_indicators import _adx
    highs = [r["high"] for r in hist]
    lows = [r["low"] for r in hist]
    vols = [r.get("volume") or 0 for r in hist]
    c = closes[-1]
    sma20 = sum(closes[-20:]) / 20
    sma50 = sum(closes[-50:]) / 50
    hi20 = max(highs[-20:])
    ret20 = (c / closes[-21] - 1) * 100 if len(closes) >= 21 else 0.0
    adx = _adx(highs, lows, closes)
    vol_recent = sum(vols[-5:]) / 5 if any(vols[-5:]) else 0
    vol_base = sum(vols[-20:]) / 20 if any(vols[-20:]) else 0
    vol_ratio = (vol_recent / vol_base) if vol_base else 0.0
    breakout = 1.0 if c >= hi20 * 0.99 else 0.0
    uptrend = 1.0 if sma20 > sma50 else 0.0
    above20 = 1.0 if c > sma20 else 0.0
    trending = 1.0 if adx >= 20.0 else 0.0
    strong = 1.0 if ret20 >= 8.0 else 0.0
    vol_conf = 1.0 if vol_ratio >= 1.2 else 0.0
    return {
        "mom_breakout_20d": breakout,
        "mom_uptrend_struct": uptrend,
        "mom_above_sma20": above20,
        "mom_ret20_pct": ret20,
        "mom_vol_ratio": vol_ratio,
        "mom_score": breakout + uptrend + above20 + trending + strong + vol_conf,
    }


def patch_momentum_features() -> None:
    import core.signal_engine as se
    orig_calc = se._calculate_features

    def calc_with_momentum(hist):
        f = orig_calc(hist)
        try:
            f.update(_momentum_features(hist))
        except Exception:
            for k in MOM_COLS:
                f.setdefault(k, 0.0)
        return f

    se._calculate_features = calc_with_momentum
    orig_cols = se._active_feature_cols
    se._active_feature_cols = lambda: list(orig_cols()) + MOM_COLS


if __name__ == "__main__":
    patch_momentum_features()
    os.environ.setdefault("GEOM_SWEEP_OUT", "/tmp/geometry_grid_sweep_momentum.json")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import geometry_grid_sweep
    sys.exit(geometry_grid_sweep.main())
