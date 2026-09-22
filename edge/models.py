"""The model layer: learn which gappers follow through -- and prove it or stay silent.

Trained on the backtest dataset (every priced, gap-qualified, liquid candidate
with point-in-time features from edge/features.py and its market outcome under
the frozen levels). Three rules decide whether it is ever used:

  1. WALK-FORWARD BY DATE. Train on earlier sessions, test on later ones, never
     the reverse, never shuffled.
  2. CALIBRATE ON TRAIN ONLY. Isotonic calibration is fit on the last 25% of
     each fold's TRAINING days; the test days never touch a parameter.
  3. BEAT TWO BARS OUT OF SAMPLE. Better Brier score than the base-rate
     forecast, AND its daily top-2 pick wins more often than the dollar-volume
     top-2 the frozen rule uses, on the same untouched test days, with at least
     40 test trades. Otherwise it is recorded as NOT QUALIFIED and never
     produces a forecast.

A qualified model is frozen: coefficients, scaler, calibration steps and the
training window are hashed into its own experiment spec
(gap_and_go_model@v<YYYYMMDD>), with a preregistered abstention threshold.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple

from edge import features as FX, stats
from edge.contracts import GAP_AND_GO_V1

MIN_PROB = 0.40          # preregistered: just above the 37.5% break-even
MIN_TEST_TRADES = 40


def _triggered(rows: List[dict]) -> List[dict]:
    return [r for r in rows if r["market"] in ("WIN", "LOSS", "TIME_EXIT")]


def _xy(rows: List[dict]) -> Tuple[List[List[float]], List[int]]:
    return [FX.vector(r["features"]) for r in rows], [1 if r["market"] == "WIN" else 0 for r in rows]


def fit(rows: List[dict]) -> Optional[Dict[str, Any]]:
    """Fit on TRAIN rows: logistic on the first 75% of days, isotonic on the last 25%."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    rows = _triggered(rows)
    days = sorted({r["day"] for r in rows})
    if len(days) < 8 or len(rows) < 40:
        return None
    cut = days[int(len(days) * 0.75)]
    core, calib = [r for r in rows if r["day"] < cut], [r for r in rows if r["day"] >= cut]
    x, y = _xy(core)
    if len(set(y)) < 2 or not calib:
        return None
    scaler = StandardScaler().fit(x)
    clf = LogisticRegression(C=0.5, max_iter=500).fit(scaler.transform(x), y)
    cx, cy = _xy(calib)
    raw = clf.predict_proba(scaler.transform(cx))[:, 1]
    steps = stats.isotonic_fit(list(map(float, raw)), cy)
    return {"features": list(FX.FEATURES), "mean": list(map(float, scaler.mean_)),
            "scale": list(map(float, scaler.scale_)), "coef": list(map(float, clf.coef_[0])),
            "intercept": float(clf.intercept_[0]), "iso": [[float(a), float(b)] for a, b in steps],
            "train_days": [days[0], days[-1]], "train_rows": len(rows)}


def predict(art: Dict[str, Any], feats: Dict[str, Any]) -> Optional[float]:
    if not FX.complete(feats):
        return None
    import math
    z = art["intercept"] + sum(c * ((v - m) / s if s else 0.0)
                               for c, v, m, s in zip(art["coef"], FX.vector(feats), art["mean"], art["scale"]))
    raw = 1 / (1 + math.exp(-z))
    return stats.isotonic_apply([tuple(x) for x in art["iso"]], raw)


def evaluate(dataset: List[dict], *, min_train_days: int = 20, test_days: int = 5) -> Dict[str, Any]:
    days = sorted({r["day"] for r in dataset})
    brier_m = brier_b = 0.0
    n_scored = 0
    model_hits: List[bool] = []
    base_hits: List[bool] = []
    by_day_model: Dict[str, List[bool]] = {}
    for start in range(min_train_days, len(days), test_days):
        train_days, test = set(days[:start]), days[start:start + test_days]
        train = [r for r in dataset if r["day"] in train_days]
        art = fit(train)
        if art is None:
            continue
        base_rate = sum(1 for r in _triggered(train) if r["market"] == "WIN") / max(1, len(_triggered(train)))
        for d in test:
            cands = [r for r in dataset if r["day"] == d]
            scored = [(predict(art, r["features"]), r) for r in cands]
            for p, r in scored:
                if p is None or r["market"] not in ("WIN", "LOSS", "TIME_EXIT"):
                    continue
                y = 1.0 if r["market"] == "WIN" else 0.0
                brier_m += (p - y) ** 2
                brier_b += (base_rate - y) ** 2
                n_scored += 1
            picks = sorted([(p, r) for p, r in scored if p is not None and p >= MIN_PROB],
                           key=lambda t: -t[0])[:2]
            for _, r in picks:
                if r["market"] in ("WIN", "LOSS", "TIME_EXIT"):
                    model_hits.append(r["market"] == "WIN")
                    by_day_model.setdefault(d, []).append(r["market"] == "WIN")
            for r in sorted(cands, key=lambda r: -(r.get("avg_dollars") or 0))[:2]:
                if r["market"] in ("WIN", "LOSS", "TIME_EXIT"):
                    base_hits.append(r["market"] == "WIN")
    def rate(h):
        return sum(h) / len(h) if h else None
    bss = (1 - brier_m / brier_b) if brier_b > 0 else None
    mr, br = rate(model_hits), rate(base_hits)
    lo, hi = stats.wilson(sum(model_hits), len(model_hits)) if model_hits else (None, None)
    unmet = []
    if n_scored == 0:
        unmet.append("no out-of-sample predictions")
    if bss is None or bss <= 0:
        unmet.append("Brier score not better than the base rate")
    if len(model_hits) < MIN_TEST_TRADES:
        unmet.append(f"only {len(model_hits)} out-of-sample model trades (< {MIN_TEST_TRADES})")
    if mr is None or br is None or mr <= br:
        unmet.append("model's daily top-2 does not beat the dollar-volume top-2")
    return {"scored": n_scored, "brier_skill": bss, "model_trades": len(model_hits), "model_win_rate": mr,
            "model_win_rate_ci": [lo, hi] if model_hits else None, "baseline_trades": len(base_hits),
            "baseline_win_rate": br, "qualified": not unmet, "unmet": unmet,
            "method": "walk-forward by date; isotonic fit on the last 25% of TRAIN days only"}


def spec_for(art: Dict[str, Any]):
    sha = hashlib.sha256(json.dumps(art, sort_keys=True).encode()).hexdigest()
    version = int(art["train_days"][1].replace("-", ""))
    return sha, replace(
        GAP_AND_GO_V1, name="gap_and_go_model", version=version,
        description="Gap-and-Go v1 levels on gap-qualified liquid names, top 2 by a walk-forward-validated, "
                    "calibrated model probability; abstain below min_prob.",
        min_prob=MIN_PROB,
        eligibility={**GAP_AND_GO_V1.eligibility, "catalyst": "model feature, not a gate",
                     "model_sha": sha, "features": art["features"], "trained_on": art["train_days"],
                     "feature_cutoff": "SIP premarket bars closed by 08:55 ET"},
    )


def train_and_register(store, dataset: List[dict]) -> Dict[str, Any]:
    ev = evaluate(dataset)
    art = fit(dataset) if ev["qualified"] else None
    rec = {"evaluation": ev, "qualified": bool(art), "rows": len(dataset)}
    if art:
        sha, spec = spec_for(art)
        rec.update({"model_sha": sha, "experiment_id": spec.experiment_id})
        store.put("edge_models", sha, {"artifact": art, "evaluation": ev, "experiment_id": spec.experiment_id})
        store.put("edge_models", "current", {"model_sha": sha, "experiment_id": spec.experiment_id})
    else:
        store.put("edge_models", "last_attempt", rec)
    return {"status": "qualified" if art else "not_qualified", **{k: rec[k] for k in rec if k != "evaluation"},
            "unmet": ev["unmet"], "brier_skill": ev["brier_skill"],
            "model_win_rate": ev["model_win_rate"], "baseline_win_rate": ev["baseline_win_rate"]}


def current(store) -> Optional[Tuple[Dict[str, Any], Any]]:
    ptr = store.get("edge_models", "current")
    if not ptr:
        return None
    rec = store.get("edge_models", ptr["model_sha"])
    if not rec:
        return None
    sha, spec = spec_for(rec["artifact"])
    if sha != ptr["model_sha"]:
        return None                      # a stored artifact that no longer hashes to its id is not used
    return rec["artifact"], spec
