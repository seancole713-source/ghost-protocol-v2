"""Performance attribution (roadmap #4b).

A) feature_attribution — works on the already-journaled model feature vector
   (scores.features) + regime: average signal values for WIN vs LOSS picks and
   win-rate by regime label. Computable over all history right now.

B) ghost_components — a per-pick snapshot of the 5 ghost-score components from
   the inputs available at fire time, journaled onto each pick so true
   component attribution accrues going forward. `sector` needs endpoint-only
   data so it is omitted (None). Weights mirror
   api.wolf_endpoints._GHOST_WEIGHTS (a drift-guard test asserts they match).
"""
import statistics
from typing import Any, Dict, List, Optional, Sequence

_ATTR_FEATURES = ["rsi", "macd_hist", "pct_b", "volume_ratio", "mom_4h", "atr_pct", "adx"]


def _avg(rows, getter):
    vals = [v for v in (getter(r) for r in rows) if v is not None]
    return round(statistics.mean(vals), 4) if vals else None


def feature_attribution(trades: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """A. trades: dicts with outcome, features(dict), regime_label(str|None)."""
    wins = [t for t in trades if t.get("outcome") == "WIN"]
    losses = [t for t in trades if t.get("outcome") == "LOSS"]

    def feat(rows, key):
        return _avg(rows, lambda r: (float(r["features"][key])
                    if isinstance(r.get("features"), dict) and r["features"].get(key) is not None
                    else None))

    feats = []
    for k in _ATTR_FEATURES:
        wv, lv = feat(wins, k), feat(losses, k)
        feats.append({"feature": k, "win_avg": wv, "loss_avg": lv,
                      "delta": round(wv - lv, 4) if (wv is not None and lv is not None) else None})

    regimes: Dict[str, Dict[str, int]] = {}
    for t in trades:
        rl = t.get("regime_label") or "Unknown"
        d = regimes.setdefault(rl, {"wins": 0, "losses": 0})
        if t.get("outcome") == "WIN":
            d["wins"] += 1
        elif t.get("outcome") == "LOSS":
            d["losses"] += 1
    by_regime = []
    for rl, d in regimes.items():
        dec = d["wins"] + d["losses"]
        by_regime.append({"regime": rl, "wins": d["wins"], "losses": d["losses"],
                          "win_rate_pct": round(d["wins"] / dec * 100, 1) if dec else None})
    by_regime.sort(key=lambda r: -(r["wins"] + r["losses"]))

    return {"wins": len(wins), "losses": len(losses), "features": feats, "by_regime": by_regime}


def component_attribution(trades: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """B. WIN-vs-LOSS averages of journaled ghost_components. Populates as new
    picks (post-deploy) accrue the snapshot."""
    comps = ["model", "volume", "sector", "momentum", "freshness"]
    wins = [t for t in trades if t.get("outcome") == "WIN" and isinstance(t.get("components"), dict)]
    losses = [t for t in trades if t.get("outcome") == "LOSS" and isinstance(t.get("components"), dict)]

    def comp(rows, key):
        return _avg(rows, lambda r: (float(r["components"][key]) if r["components"].get(key) is not None else None))

    rows = []
    for k in comps:
        wv, lv = comp(wins, k), comp(losses, k)
        rows.append({"component": k, "win_avg": wv, "loss_avg": lv,
                     "delta": round(wv - lv, 3) if (wv is not None and lv is not None) else None})
    return {"wins": len(wins), "losses": len(losses), "components": rows,
            "available": (len(wins) + len(losses)) > 0}


def ghost_components(confidence, direction, features, predicted_at, now_ts) -> Dict[str, Any]:
    """B snapshot. Weights/bands mirror api.wolf_endpoints scorers."""
    f = features or {}
    conf = float(confidence or 0)
    d = (direction or "").upper()
    if d in ("UP", "BUY"):
        model = round(conf * 40, 2)
    elif d in ("DOWN", "SELL"):
        model = round((1 - conf) * 40, 2)
    else:
        model = 20.0
    vr = f.get("volume_ratio")
    volume = round(min(20.0, max(0.0, float(vr) * 10)), 2) if vr is not None else 10.0
    mom = f.get("mom_4h")
    momentum = 7.5
    if mom is not None:
        m = float(mom)
        momentum = 15.0 if m >= 0.03 else 12.0 if m >= 0.01 else 7.5 if m >= -0.01 else 3.0 if m >= -0.03 else 0.0
    age_h = max(0.0, (now_ts - (predicted_at or now_ts)) / 3600.0)
    freshness = (10.0 if age_h <= 2 else 8.0 if age_h <= 6 else 6.0 if age_h <= 12
                 else 4.0 if age_h <= 24 else 2.0 if age_h <= 48 else 0.0)
    return {"model": model, "volume": volume, "sector": None, "momentum": momentum, "freshness": freshness}
