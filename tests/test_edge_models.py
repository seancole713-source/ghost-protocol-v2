"""The model layer: qualifies only when it beats the base rate and the baseline out of sample."""
from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from edge import features as FX, models as MD
from edge.ledger import MemoryStore


def dataset(signal: bool, *, days=60, per_day=8, seed=7):
    rng = random.Random(seed)
    rows, d = [], date(2026, 6, 1)
    while len({r["day"] for r in rows}) < days:
        d += timedelta(days=1)
        if d.weekday() >= 5:
            continue
        for i in range(per_day):
            trend = rng.uniform(-3, 3)
            feats = {"gap_855": rng.uniform(5, 30), "log_price": rng.uniform(0.5, 2.5),
                     "log_dollar_volume": rng.uniform(6.7, 9.0), "pre_volume_ratio": rng.uniform(0, 1),
                     "pre_range_pos": rng.uniform(0, 1), "pre_trend": trend,
                     "catalyst_company": float(rng.random() < 0.4), "catalyst_policy": 0.0,
                     "dilutive": 0.0, "any_news": 1.0, "dow": float(d.weekday())}
            if signal:      # strong premarket trend genuinely follows through
                p = 0.75 if trend > 1 else 0.2
            else:
                p = 0.38
            win = rng.random() < p
            rows.append({"day": d.isoformat(), "symbol": f"S{i}", "features": feats,
                         "avg_dollars": 10 ** feats["log_dollar_volume"],
                         "market": "WIN" if win else "LOSS"})
    return rows


def test_a_real_signal_qualifies_out_of_sample():
    ev = MD.evaluate(dataset(True))
    assert ev["qualified"], ev["unmet"]
    assert ev["brier_skill"] > 0 and ev["model_win_rate"] > ev["baseline_win_rate"]


def test_noise_does_not_qualify():
    ev = MD.evaluate(dataset(False))
    assert not ev["qualified"]
    assert any("base rate" in u or "top-2" in u for u in ev["unmet"])


def test_incomplete_features_are_never_scored():
    art = MD.fit(dataset(True))
    feats = dict(dataset(True)[0]["features"], pre_trend=None)
    assert MD.predict(art, feats) is None


def test_probabilities_are_calibrated_into_0_1():
    art = MD.fit(dataset(True))
    ps = [MD.predict(art, r["features"]) for r in dataset(True, seed=99)[:200]]
    assert all(0.0 <= p <= 1.0 for p in ps)


def test_a_qualified_model_is_frozen_and_a_tampered_one_is_refused():
    store = MemoryStore()
    out = MD.train_and_register(store, dataset(True))
    assert out["status"] == "qualified" and out["experiment_id"].startswith("gap_and_go_model@v")
    art, spec = MD.current(store)
    assert spec.min_prob == MD.MIN_PROB and spec.eligibility["model_sha"] == out["model_sha"]
    rec = store.get("edge_models", out["model_sha"])
    rec["artifact"]["coef"][0] += 1.0                     # tamper
    store.put("edge_models", out["model_sha"], rec)
    assert MD.current(store) is None


def test_an_unqualified_model_is_recorded_but_never_current():
    store = MemoryStore()
    out = MD.train_and_register(store, dataset(False))
    assert out["status"] == "not_qualified" and MD.current(store) is None
    assert store.get("edge_models", "last_attempt")["qualified"] is False


def test_live_card_uses_the_qualified_model_with_the_same_features(monkeypatch):
    import sys
    sys.path.insert(0, "tests")
    from test_edge_pipeline import FakeAlpaca, ts
    from edge import pipeline as P
    from edge.ledger import Ledger
    lg = Ledger(MemoryStore())
    MD.train_and_register(lg.store, dataset(True))
    captured = {}
    real_build = FX.build

    def spy(**kw):
        f = real_build(**kw)
        captured[kw["prev_close"]] = kw["sip_bars"]
        return f
    monkeypatch.setattr(FX, "build", spy)
    out = P.morning_card(FakeAlpaca(), lg, now=ts(9, 10))
    card = lg.store.get("edge_cards", "2026-09-23")
    assert card["model_experiment"].startswith("gap_and_go_model@v")
    assert captured                                        # the shared builder ran for live rows
    abst = lg.store.scan("abstentions", experiment_id=card["model_experiment"])
    assert abst and all(a["reasons"] for a in abst)       # every non-pick says why
