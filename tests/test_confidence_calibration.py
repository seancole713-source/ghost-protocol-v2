"""Ghost's confidence, shown with what it has actually been worth.

The operator asked for a ranked list -- "stock x 58%, stock y 63%, stock z
73%" -- so the highest-conviction name is visible at a glance. Ghost already
computes that number every scan, so the list is a display change.

Shipping it raw would be a trap. Over 4,424 resolved shadow outcomes the
ordering is INVERTED: up fireable (>=0.55) realised 56.6% while up weak
(<0.50) realised 61.5%; down fireable 55.1% against down weak 65.8%. A list
sorted by confidence descending would steer the reader to the worse bet every
day, and would look exactly like the tool they asked for while doing it.

These tests pin that the number is never shown without its receipt, and that
an inversion is stated rather than left to be inferred from a table.
"""
from __future__ import annotations

import core.confidence_calibration as cc


def _row(prob, outcome, direction="UP"):
    return {"up_prob": prob, "outcome": outcome, "direction": direction}


# ------------------------------------------------------------- banding --

def test_bands_are_finer_than_fireable_near_weak():
    """A single 'fireable' bucket spans 0.55-1.00 and hides whether 0.90 beats
    0.60 -- which is the entire question."""
    assert cc.band_for(0.56) != cc.band_for(0.95)
    assert cc.band_for(0.62) != cc.band_for(0.75)


def test_an_unusable_probability_gets_no_band():
    for bad in (None, "", "abc", float("nan")):
        assert cc.band_for(bad) is None


def test_band_edges_are_upper_exclusive():
    assert cc.band_for(0.55) == "55-60%"
    assert cc.band_for(0.5499) == "50-55%"
    assert cc.band_for(1.0) == "90%+"


# ------------------------------------------------- the inversion finding --

def test_an_inverted_calibration_is_reported_as_inverted():
    """THE case. High confidence losing more than low confidence is the
    finding that makes a ranked list dangerous, so it must be detected."""
    rows = ([_row(0.95, "LOSS") for _ in range(30)]
            + [_row(0.95, "WIN") for _ in range(10)]      # 90%+ -> 25%
            + [_row(0.45, "WIN") for _ in range(30)]
            + [_row(0.45, "LOSS") for _ in range(10)])    # <50%  -> 75%

    calib = cc.build_calibration(rows, direction="UP")

    assert calib["monotonic"] is False


def test_a_healthy_calibration_reads_monotonic():
    rows = ([_row(0.45, "LOSS") for _ in range(30)]
            + [_row(0.45, "WIN") for _ in range(10)]
            + [_row(0.95, "WIN") for _ in range(30)]
            + [_row(0.95, "LOSS") for _ in range(10)])

    assert cc.build_calibration(rows, direction="UP")["monotonic"] is True


def test_too_little_evidence_says_unknown_not_healthy():
    """Absence of a detected inversion must never read as a clean bill."""
    rows = [_row(0.95, "WIN") for _ in range(3)]

    assert cc.build_calibration(rows, direction="UP")["monotonic"] is None


def test_the_report_states_the_inversion_in_words(monkeypatch):
    rows = ([_row(0.95, "LOSS", d) for d in ("UP", "DOWN") for _ in range(30)]
            + [_row(0.45, "WIN", d) for d in ("UP", "DOWN") for _ in range(30)])
    import core.shadow_outcomes as so
    monkeypatch.setattr(so, "load_shadow_rows", lambda **kw: rows)

    out = cc.calibrated_confidence_report()

    assert "INVERTED" in out.get("warning", "")


# -------------------------------------------------------- the receipt --

def test_confidence_is_never_shown_without_its_history():
    rows = [_row(0.62, "WIN") for _ in range(40)] + [_row(0.62, "LOSS") for _ in range(60)]
    calib = cc.build_calibration(rows, direction="UP")

    out = cc.annotate(0.62, calib)

    assert out["band"] == "60-70%"
    assert out["history"]["realized_pct"] == 40.0
    assert out["history"]["n"] == 100


def test_a_band_with_no_history_returns_none_not_a_number():
    """Rendering a rate nothing backs is the failure this exists to stop."""
    calib = cc.build_calibration([_row(0.62, "WIN")], direction="UP")

    out = cc.annotate(0.95, calib)

    assert out["history"] is None
    assert "no resolved history" in out["note"]


def test_a_thin_band_is_flagged_as_noise():
    """A 100% band on n=3 is the most misleading number this could print."""
    calib = cc.build_calibration([_row(0.95, "WIN") for _ in range(3)], direction="UP")

    out = cc.annotate(0.95, calib)

    assert out["history"]["thin"] is True
    assert "thin sample" in out["note"]


# ---------------------------------------------------------- hygiene --

def test_pending_rows_never_count():
    """Including unresolved rows would quietly deflate every rate."""
    rows = [_row(0.62, "WIN") for _ in range(10)] + [_row(0.62, None) for _ in range(90)]

    calib = cc.build_calibration(rows, direction="UP")

    assert calib["bands"][0]["n"] == 10
    assert calib["bands"][0]["realized_pct"] == 100.0


def test_the_other_direction_is_never_pooled_in():
    rows = [_row(0.62, "WIN", "UP") for _ in range(10)] + [_row(0.62, "LOSS", "DOWN") for _ in range(10)]

    assert cc.build_calibration(rows, direction="UP")["total_samples"] == 10
    assert cc.build_calibration(rows, direction="DOWN")["total_samples"] == 10


def test_a_load_failure_degrades_instead_of_raising(monkeypatch):
    import core.shadow_outcomes as so

    def boom(**kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(so, "load_shadow_rows", boom)

    out = cc.calibrated_confidence_report()

    assert "db down" in out["error"]
    assert out["lanes"] == {}
