"""A calibration zero must say which kind of zero it is.

checklist_calibration reported total_samples=0 across all four cohorts for
days while nothing was being written at all: production ran
V3_LABEL_HOLD_BARS=5 against a hardcoded HOLD_BARS=3,
validate_outcome_contract() failed closed on every write, and shadow_outcomes
swallowed the error per row as a WARNING (PR #180). The payload read
"0 samples, still accruing" the entire time.

contract_ok (added by #180) says writes are PERMITTED. It does not say they
HAPPENED -- and on 2026-09-08 the live payload showed contract_ok=true with
all four cohorts still at zero, which is genuinely ambiguous: snapshots
waiting out a 5-bar hold look exactly like snapshots never written.

These tests pin that the two are now distinguishable on sight.
"""
from __future__ import annotations

import core.ghost_ask as ga


def _summary(monkeypatch, counts):
    import core.checklist_ledger as ledger

    monkeypatch.setattr(ledger, "validate_outcome_contract", lambda: None)
    monkeypatch.setattr(ledger, "resolved_samples_for_calibration",
                        lambda **kw: [])
    monkeypatch.setattr(ledger, "snapshot_counts", lambda **kw: dict(counts))
    return ga.checklist_calibration_summary()


def test_nothing_written_is_not_reported_as_waiting(monkeypatch):
    """THE case. A dead lane must never read as a patient one."""
    out = _summary(monkeypatch, {"written": 0, "pending": 0, "resolved": 0})

    for key, cohort in out["cohorts"].items():
        assert cohort["lane_state"] == "never_written", key
        assert cohort["total_samples"] == 0


def test_written_but_unresolved_reads_as_waiting(monkeypatch):
    """The benign case: a 5-bar hold has not elapsed. Same zero, different
    cause, and the operator must be able to tell them apart."""
    out = _summary(monkeypatch, {"written": 214, "pending": 214, "resolved": 0,
                                 "oldest_pending_age_s": 3600})

    for cohort in out["cohorts"].values():
        assert cohort["lane_state"] == "waiting_on_hold"
        assert cohort["snapshots"]["written"] == 214


def test_resolved_rows_read_as_accruing(monkeypatch):
    out = _summary(monkeypatch, {"written": 300, "pending": 80, "resolved": 220})

    for cohort in out["cohorts"].values():
        assert cohort["lane_state"] == "accruing"


def test_a_counter_failure_is_named_not_swallowed(monkeypatch):
    out = _summary(monkeypatch, {"error": "db down"})

    for cohort in out["cohorts"].values():
        assert cohort["lane_state"] == "error"


def test_contract_ok_and_never_written_can_both_be_true(monkeypatch):
    """The live 2026-09-08 shape. contract_ok=true says writes are allowed;
    it says nothing about whether any occurred, and reading it as reassurance
    is what cost days last time."""
    out = _summary(monkeypatch, {"written": 0, "pending": 0, "resolved": 0})

    assert out["contract_ok"] is True
    assert all(c["lane_state"] == "never_written" for c in out["cohorts"].values())


def test_all_four_cohorts_carry_the_state(monkeypatch):
    out = _summary(monkeypatch, {"written": 5, "pending": 5, "resolved": 0})

    assert set(out["cohorts"]) == {
        "shadow:UP", "shadow:DOWN", "official:UP", "official:DOWN",
    }
    assert all("snapshots" in c for c in out["cohorts"].values())
