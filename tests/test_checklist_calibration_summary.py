"""Surface whether the catalyst checklist actually predicts anything.

The checklist pipeline (evaluate -> snapshot -> resolve -> calibrate) has been
in place since PR #165/#166, but its central empirical claim — that a higher
completeness score wins more often — was only observable through
/api/ghost/checklist/{symbol}/calibration, which needs direct API access.
Nobody had checked it.

That matters because 2026-09-04's full-fleet retrain showed the price-derived
models carry no edge: they score 60-67% accuracy purely by predicting the
majority class (edge = holdout_acc - max(natural_rate, 1-natural_rate) came
back at 0.0% or negative across ~200 lanes). The checklist is the proposed
replacement, so the same question has to be asked of it BEFORE anything is
built on top — otherwise one non-predictive number replaces another.

`spread_pp` is that question in one figure: realized hit rate of the highest
populated band minus the lowest. Flat means the score carries no information.
"""
from __future__ import annotations

import core.ghost_ask as ga
from core.checklist_calibration import MIN_BAND_SAMPLES


def _samples(pairs):
    """(score_pct, n_rows, n_wins) -> calibration sample dicts."""
    out = []
    for score, n, wins in pairs:
        for i in range(n):
            out.append({"score_pct": float(score), "won": i < wins})
    return out


def _patch_samples(monkeypatch, by_cohort):
    """Feed a fixed sample set per (lane, direction)."""
    import core.checklist_ledger as ledger

    def fake(*, checklist_version, hold_bars, outcome_contract,
             direction, symbol=None, min_issued_before=None, lane="official"):
        return by_cohort.get((lane, direction), [])

    monkeypatch.setattr(ledger, "resolved_samples_for_calibration", fake)


# ------------------------------------------------------ the edge question --

def test_spread_is_large_when_the_checklist_discriminates(monkeypatch):
    """A checklist that works: low bands lose, high bands win."""
    _patch_samples(monkeypatch, {
        ("shadow", "UP"): _samples([(15.0, 20, 4), (85.0, 20, 16)]),
    })

    cohort = ga.checklist_calibration_summary()["cohorts"]["shadow:UP"]

    assert cohort["total_samples"] == 40
    assert cohort["populated_bands"] == 2
    assert cohort["spread_pp"] == 60.0
    assert cohort["any_proven"] is True


def test_spread_is_flat_when_the_checklist_carries_no_information(monkeypatch):
    """The failure mode this exists to detect: every band wins at the same
    rate, so completeness tells you nothing. Must NOT read as signal."""
    _patch_samples(monkeypatch, {
        ("shadow", "UP"): _samples([(15.0, 20, 11), (85.0, 20, 11)]),
    })

    cohort = ga.checklist_calibration_summary()["cohorts"]["shadow:UP"]

    assert cohort["spread_pp"] == 0.0
    assert cohort["total_samples"] == 40


def test_thin_bands_report_unproven_rather_than_a_confident_number(monkeypatch):
    """Below MIN_BAND_SAMPLES a band must not read as established, however
    good its raw rate looks — the small-sample discipline this project's
    accountability doctrine exists to enforce."""
    thin = MIN_BAND_SAMPLES - 1
    _patch_samples(monkeypatch, {
        ("shadow", "UP"): _samples([(85.0, thin, thin)]),  # a perfect record
    })

    cohort = ga.checklist_calibration_summary()["cohorts"]["shadow:UP"]
    band = cohort["bands"][0]

    assert band["raw_rate_pct"] == 100.0
    assert band["proven"] is False
    assert cohort["proven_bands"] == 0
    assert cohort["any_proven"] is False
    # The Wilson bound must be far below the raw rate on this few samples.
    assert band["proven_rate_pct"] < 85.0


def test_empty_cohort_reports_zero_not_an_error(monkeypatch):
    """No samples yet is a legitimate answer, not a failure."""
    _patch_samples(monkeypatch, {})

    summary = ga.checklist_calibration_summary()

    for key in ("shadow:UP", "shadow:DOWN", "official:UP", "official:DOWN"):
        assert summary["cohorts"][key]["total_samples"] == 0
        assert summary["cohorts"][key]["spread_pp"] is None


# ------------------------------------------------------------- plumbing --

def test_all_four_cohorts_are_reported(monkeypatch):
    """Shadow accrues while the engine is paused; official only fires when it
    is live. Both directions matter, so all four are surfaced separately and
    never pooled."""
    _patch_samples(monkeypatch, {})

    summary = ga.checklist_calibration_summary()

    assert set(summary["cohorts"]) == {
        "shadow:UP", "shadow:DOWN", "official:UP", "official:DOWN",
    }
    assert summary["min_band_samples"] == MIN_BAND_SAMPLES
    assert summary["checklist_version"]


def test_summary_failure_never_breaks_the_context_payload(monkeypatch):
    """Diagnostics must never take down /api/wolf/ask/context."""
    monkeypatch.setattr(
        ga, "checklist_calibration_summary",
        lambda: (_ for _ in ()).throw(RuntimeError("db down")),
    )

    ctx = ga.build_ask_context()

    assert "checklist_calibration_error" in ctx
    assert "db down" in ctx["checklist_calibration_error"]
