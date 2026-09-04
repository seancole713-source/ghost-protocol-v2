"""The checklist lane recorded nothing for days, silently.

Found 2026-09-04 while trying to answer whether the catalyst checklist has any
predictive edge. All four calibration cohorts read total_samples=0 — not thin,
empty. Root cause chain, all verified live:

  1. production runs V3_LABEL_HOLD_BARS=5
  2. core/catalyst_checklist.py hardcoded HOLD_BARS = 3, with a comment
     asserting it "matches the trained lane" — it did not
  3. checklist_ledger.validate_outcome_contract() fails closed on that
     divergence, and it is the FIRST statement of record_snapshot()
  4. so every snapshot write raised
  5. shadow_outcomes caught it per row ("one bad row must not stop the rest"),
     logged a warning, and continued

Net effect: a configuration error wearing the costume of flaky data. Zero
snapshots, zero calibration samples, no error-level line, for days.

The guard was correct; the constant was the bug. Two fixes here:
  * HOLD_BARS is derived from the same source the resolver reads, so the
    horizons cannot diverge at all rather than merely being caught when they do
  * the contract is validated ONCE before the shadow loop, at ERROR level, so a
    config fault can never again be mistaken for per-row noise
"""
from __future__ import annotations

import importlib

import core.catalyst_checklist as cc
import core.checklist_ledger as cl


def _reload_at(monkeypatch, hold_bars: str):
    monkeypatch.setenv("V3_LABEL_HOLD_BARS", hold_bars)
    importlib.reload(cc)
    importlib.reload(cl)
    return cc, cl


# ------------------------------------------------- horizons cannot diverge --

def test_hold_bars_tracks_the_resolver_at_any_horizon(monkeypatch):
    """The regression: a hardcoded 3 against a runtime 5."""
    for horizon in ("3", "5", "7"):
        checklist, _ = _reload_at(monkeypatch, horizon)
        assert checklist.HOLD_BARS == int(horizon)

    monkeypatch.delenv("V3_LABEL_HOLD_BARS", raising=False)
    importlib.reload(cc)
    importlib.reload(cl)


def test_contract_validates_at_the_production_horizon(monkeypatch):
    """V3_LABEL_HOLD_BARS=5 is what production actually runs, and is exactly
    the configuration under which every write used to raise."""
    _, ledger = _reload_at(monkeypatch, "5")

    ledger.validate_outcome_contract()  # must not raise
    assert ledger.DEFAULT_OUTCOME_CONTRACT.endswith("5_daily_bars")

    monkeypatch.delenv("V3_LABEL_HOLD_BARS", raising=False)
    importlib.reload(cc)
    importlib.reload(cl)


def test_outcome_contract_embeds_the_runtime_horizon(monkeypatch):
    """Cohort identity must keep snapshots from different horizons apart, so
    rows written under the old 3-bar contract never pool with 5-bar rows."""
    _, ledger = _reload_at(monkeypatch, "3")
    three = ledger.DEFAULT_OUTCOME_CONTRACT
    _, ledger = _reload_at(monkeypatch, "5")
    five = ledger.DEFAULT_OUTCOME_CONTRACT

    assert three != five
    assert "3_daily_bars" in three
    assert "5_daily_bars" in five

    monkeypatch.delenv("V3_LABEL_HOLD_BARS", raising=False)
    importlib.reload(cc)
    importlib.reload(cl)


def test_the_hardcoded_constant_is_gone():
    """Guards the specific line that caused this.

    Matched as an assignment at column 0, so the phrase appearing inside the
    explanatory comment does not trip it.
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "core" / "catalyst_checklist.py").read_text(encoding="utf-8")

    assert not re.search(r"^HOLD_BARS\s*=\s*\d+\s*$", src, re.MULTILINE), (
        "the horizon is hardcoded again"
    )
    assert re.search(r"^HOLD_BARS\s*=\s*_label_hold_bars\(\)", src, re.MULTILINE)


# ------------------------------------------------ config faults must be loud --

def test_shadow_snapshots_abort_once_and_loudly_on_a_broken_contract(monkeypatch, caplog):
    """A process-wide config fault must not be reported as N per-row warnings.

    Before: every row raised, each logged at WARNING, the run reported success
    with written=0, and nothing surfaced as an error.
    """
    import logging

    import core.shadow_outcomes as so

    monkeypatch.setattr(
        cl, "validate_outcome_contract",
        lambda: (_ for _ in ()).throw(
            RuntimeError("checklist hold-bars contract mismatch: checklist=3, resolver=5")
        ),
    )
    # If the loop were still entered, this would raise a different error and
    # prove the guard did not short-circuit.
    monkeypatch.setattr(
        cl, "store_snapshot",
        lambda **kw: (_ for _ in ()).throw(AssertionError("loop must not run")),
    )

    rows = [{"symbol": "AAPL", "direction": "UP", "eval_ts": 1,
             "shadow_outcome_id": 1, "scores": {}}]
    with caplog.at_level(logging.ERROR):
        written = so.snapshot_shadow_checklists(rows, budget=10)

    assert written == 0
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "a broken contract must log at ERROR, not WARNING"
    assert "DISABLED" in errors[0].getMessage()
    assert "contract mismatch" in errors[0].getMessage()


def test_context_summary_reports_contract_health_next_to_the_zeros(monkeypatch):
    """Empty cohorts mean different things depending on this flag: not enough
    data yet, versus nothing can ever be written."""
    import core.ghost_ask as ga

    monkeypatch.setattr(
        cl, "resolved_samples_for_calibration",
        lambda **kw: [],
    )
    monkeypatch.setattr(
        cl, "validate_outcome_contract",
        lambda: (_ for _ in ()).throw(RuntimeError("contract mismatch: 3 vs 5")),
    )

    summary = ga.checklist_calibration_summary()

    assert summary["contract_ok"] is False
    assert "3 vs 5" in summary["contract_error"]


def test_context_summary_reports_healthy_contract_when_coherent(monkeypatch):
    import core.ghost_ask as ga

    monkeypatch.setattr(cl, "resolved_samples_for_calibration", lambda **kw: [])
    monkeypatch.setattr(cl, "validate_outcome_contract", lambda: None)

    summary = ga.checklist_calibration_summary()

    assert summary["contract_ok"] is True
    assert "contract_error" not in summary
