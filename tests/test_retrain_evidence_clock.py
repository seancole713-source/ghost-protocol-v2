"""The retrain loop was shredding the evidence it needed to finish.

proven_skill_gate counts resolved shadow outcomes WHERE model_sha256=%s and
requires 10. Every retrain rewrites that sha. Both retrain jobs decided what to
train from get_model_status()["symbols"], which is built only where
model_serve_guard returns None -- proven tier only. With every model stamped
"research" that map is empty, so coverage saw missing=107 forever and retrained
the whole fleet every cycle, and the map it read could never be raised by the
loop reading it. Its own docstring said "lack a LOADABLE v3 model"; the
implementation never matched.

Measured live on 2026-09-05, 30 days of shadow outcomes:

    identity groups (symbol, direction, sha, schemas, hold_bars)   3,252
    distinct model_sha256                                          3,252
    max n in any group                                                 6
    groups reaching the gate's n>=10                                    0
    pooled by LANE instead of by sha: mean 19.3, and 153/207 lanes >= 10

The evidence was never thin. It was being split across 3,252 buckets holding
about one outcome each, and no model could ever prove skill -- however good.
"""
from __future__ import annotations

import wolf_app


def _stored(**models):
    """stored_symbols as get_model_status() returns it."""
    return {k: dict(v) for k, v in models.items()}


def _patch_status(monkeypatch, stored):
    import core.signal_engine as se
    monkeypatch.setattr(se, "get_model_status",
                        lambda *a, **k: {"stored_symbols": stored,
                                         "symbols": {}, "models": 0})


def _patch_watchlist(monkeypatch, symbols):
    monkeypatch.setattr(wolf_app, "_build_train_symbol_list",
                        lambda: [(s, "stock") for s in symbols])


# ------------------------------------------------- loadable, not proven --

def test_a_research_model_counts_as_covered(monkeypatch):
    """THE bug. A research-tier model loads, matches every schema, and scores a
    shadow probability every cycle -- CAT produced up_prob 0.8966 from one.
    Counting it as missing is what made the loop unable to ever finish."""
    _patch_status(monkeypatch, _stored(
        AAPL_up={"serve_reject": "tier_unproven", "tier": "research", "age_days": 1.0},
    ))
    _patch_watchlist(monkeypatch, ["AAPL"])

    assert wolf_app._watchlist_missing_symbol_pairs() == []


def test_a_genuinely_unusable_model_still_counts_as_missing(monkeypatch):
    """tier_unproven is the ONLY reject that leaves a usable artifact. A stale
    schema or an expired model means there is nothing to serve."""
    for reject in ("label_schema_stale", "model_expired", "missing_pickle",
                   "feature_schema_stale", "model_sha256_invalid"):
        _patch_status(monkeypatch, _stored(
            AAPL_up={"serve_reject": reject, "age_days": 1.0}))
        _patch_watchlist(monkeypatch, ["AAPL"])

        assert wolf_app._watchlist_missing_symbol_pairs() == [("AAPL", "stock")], reject


def test_a_proven_model_still_counts_as_covered(monkeypatch):
    _patch_status(monkeypatch, _stored(
        AAPL_up={"serve_reject": None, "tier": "proven", "age_days": 1.0}))
    _patch_watchlist(monkeypatch, ["AAPL"])

    assert wolf_app._watchlist_missing_symbol_pairs() == []


def test_directional_keys_map_back_to_their_symbol(monkeypatch):
    _patch_status(monkeypatch, _stored(
        AAPL_down={"serve_reject": "tier_unproven", "age_days": 1.0},
        MSFT={"serve_reject": None, "age_days": 1.0},
    ))
    _patch_watchlist(monkeypatch, ["AAPL", "MSFT", "NVDA"])

    assert wolf_app._watchlist_missing_symbol_pairs() == [("NVDA", "stock")]


# --------------------------------------------- do not rotate a good sha --

def test_a_fresh_loadable_model_is_not_retrained(monkeypatch):
    """Retraining is not free: it rewrites model_sha256 and zeroes the skill
    gate's forward-evidence count. A model that still loads and is nowhere near
    expiry must be left alone so its evidence can accumulate."""
    _patch_status(monkeypatch, _stored(
        AAPL_up={"serve_reject": "tier_unproven", "age_days": 2.0}))
    _patch_watchlist(monkeypatch, ["AAPL"])

    assert wolf_app._models_needing_retrain() == []


def test_a_model_near_the_serve_expiry_is_refreshed(monkeypatch):
    """model_serve_guard expires an unactivated model at 14 days. Refresh it
    before it stops loading, not on a fixed clock."""
    _patch_status(monkeypatch, _stored(
        AAPL_up={"serve_reject": "tier_unproven", "age_days": 12.0}))
    _patch_watchlist(monkeypatch, ["AAPL"])

    assert wolf_app._models_needing_retrain() == [("AAPL", "stock")]


def test_an_unreadable_age_is_treated_as_due(monkeypatch):
    """A model whose trained_at cannot be read is exactly the one that might
    already be expired; fail toward refreshing it."""
    _patch_status(monkeypatch, _stored(
        AAPL_up={"serve_reject": "tier_unproven", "age_days": None}))
    _patch_watchlist(monkeypatch, ["AAPL"])

    assert wolf_app._models_needing_retrain() == [("AAPL", "stock")]


def test_a_missing_model_is_always_retrained(monkeypatch):
    _patch_status(monkeypatch, _stored())
    _patch_watchlist(monkeypatch, ["AAPL"])

    assert wolf_app._models_needing_retrain() == [("AAPL", "stock")]


def test_the_refresh_window_is_configurable(monkeypatch):
    monkeypatch.setenv("MODEL_REFRESH_WITHIN_DAYS", "7")
    _patch_status(monkeypatch, _stored(
        AAPL_up={"serve_reject": "tier_unproven", "age_days": 8.0}))
    _patch_watchlist(monkeypatch, ["AAPL"])

    assert wolf_app._models_needing_retrain() == [("AAPL", "stock")]


def test_the_refresh_window_can_never_exceed_the_serve_expiry(monkeypatch):
    """A window wider than the 14-day life would mark every model due forever
    and restore the exact loop this fixes."""
    monkeypatch.setenv("MODEL_REFRESH_WITHIN_DAYS", "999")

    assert wolf_app._model_refresh_within_days() <= 13.0


# ------------------------------------------------- neither job blankets --

def test_no_retrain_path_falls_back_to_the_whole_fleet():
    """Both jobs used to train every symbol -- coverage when nothing was
    missing, weekly on a 7-day clock. At ~0.64 resolved outcomes per lane per
    day a 7-day rotation caps a lane at ~4.5, so the gate's 10 was unreachable
    by any model however good."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "wolf_app.py").read_text(encoding="utf-8")
    weekly = src.index("def _weekly_retrain")
    window = src[weekly:weekly + 3000]

    assert "_models_needing_retrain()" in window
    assert "syms = _v3_train_collect_symbols()" not in window, \
        "weekly retrain still blankets the fleet"

    cov = src.index("def _coverage_maintenance_job")
    cov_window = src[cov:cov + 4000]
    assert "missing if missing else _build_train_symbol_list()" not in cov_window, \
        "coverage still falls back to the whole fleet"


def test_a_status_failure_degrades_to_missing_not_to_covered(monkeypatch):
    """Failing closed here would mean a broken status read silently reports
    full coverage and no model ever gets trained again."""
    import core.signal_engine as se

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(se, "get_model_status", boom)

    cov = wolf_app._model_fleet_coverage()

    assert cov["loadable_symbols"] == set()
    assert cov["loadable_models"] == 0
