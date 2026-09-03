"""The staged 55% contract.

The operator set a staged goal on 2026-09-03: reach 55% first, then ratchet
up. This adds a named "55" contract rather than hand-tuning env vars across
modules, because core/accuracy_contract.py is the declared single source of
truth and scattered per-knob overrides are exactly how targets drift silently
(see PR #135's GOVERNANCE DRIFT finding).

The load-bearing test here is
test_lowering_the_target_to_55_does_not_manufacture_a_pass: the precision gate
compares the WILSON LOWER BOUND to the target, so at the pooled proof measured
that day (339/576 = 58.85%, LB 54.79%) a 55% target still FAILS. Lowering the
goal moves the goal; it does not fabricate evidence. If that ever stops being
true, someone has changed the gate to compare the point estimate instead, and
this test is the alarm.
"""
from __future__ import annotations

import importlib

import pytest

import core.accuracy_contract as ac
from core.precision_gate import wilson_lower_bound


# The pooled cross-symbol proof as measured live on 2026-09-03.
POOLED_WINS, POOLED_N = 339, 576


@pytest.fixture
def contract_55(monkeypatch):
    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "55")
    importlib.reload(ac)
    yield ac
    monkeypatch.delenv("GHOST_ACCURACY_CONTRACT", raising=False)
    importlib.reload(ac)


# ------------------------------------------------ the honesty guarantee --

def test_lowering_the_target_to_55_does_not_manufacture_a_pass():
    """A 55% target still fails on the current pooled proof, by 0.21pp.

    precision_gate.py fires on `wilson_lower_bound(wins, n) >= target`. The
    observed rate (58.85%) is already above 55%; the sample is just not large
    enough to PROVE 55% at 95% confidence. So the staged target sets an
    ambition, it does not hand anything a pass.
    """
    lb = wilson_lower_bound(POOLED_WINS, POOLED_N)

    assert POOLED_WINS / POOLED_N > 0.55, "observed rate should already exceed 55%"
    assert lb < 0.55, (
        "the 55% contract now passes on the pooled proof — either the sample "
        "grew or the gate stopped using the Wilson lower bound; verify which"
    )
    assert 0.547 < lb < 0.548


def test_more_evidence_at_the_same_rate_is_what_earns_the_pass():
    """The route to firing is samples, not a smaller number.

    Holding the measured hit rate fixed, the lower bound clears 0.55 at
    roughly 651 independent samples.
    """
    rate = POOLED_WINS / POOLED_N

    assert wilson_lower_bound(round(rate * 576), 576) < 0.55
    assert wilson_lower_bound(round(rate * 651), 651) >= 0.55


# ------------------------------------------------------- contract shape --

def test_contract_55_is_registered_and_selectable(contract_55):
    assert "55" in contract_55.CONTRACTS
    assert contract_55.contract_name() == "55"

    spec = contract_55.active_contract()
    assert spec.target_win_rate == 0.55
    assert spec.precision_target == 0.55


def test_default_contract_is_still_70(monkeypatch):
    """Adding 55 must not change what an unconfigured deployment runs."""
    monkeypatch.delenv("GHOST_ACCURACY_CONTRACT", raising=False)
    importlib.reload(ac)

    assert ac.contract_name() == "70"
    assert ac.active_contract().precision_target == 0.70


def test_admission_sits_below_firing_but_above_a_coin_flip(contract_55):
    """Mirrors the 70 contract's structure (admit 0.60, fire 0.70) without
    admitting models that are worse than chance."""
    spec = contract_55.active_contract()

    assert spec.min_holdout_acc < spec.precision_target
    assert spec.min_wf_acc_mean < spec.precision_target
    assert spec.min_holdout_acc > 0.50
    assert spec.min_wf_acc_mean > 0.50


def test_validation_rigor_and_kill_floor_are_not_relaxed(contract_55):
    """Fold count and the kill floor are target-independent, so the staged
    contract must not weaken them relative to the 70 contract."""
    spec = contract_55.active_contract()
    seventy = contract_55.CONTRACTS["70"]

    assert spec.min_wf_folds == seventy.min_wf_folds
    assert spec.kill_winrate_floor == seventy.kill_winrate_floor


def test_edge_floor_stays_positive(contract_55):
    """A model with no out-of-time edge must never count, at any target --
    the rule PR #135 established and PR #172's sibling fix restored."""
    assert contract_55.active_contract().min_edge > 0.0


# ------------------------------------------------ no-weakening guarantee --

def test_env_may_tighten_the_55_contract_but_never_weaken_it(monkeypatch):
    """The new contract gets the same protection as 70/80, not legacy's
    escape hatch: a floor field can be raised by env, never lowered."""
    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "55")
    importlib.reload(ac)

    monkeypatch.setenv("V3_PRECISION_TARGET", "0.65")
    assert ac.resolve_float("V3_PRECISION_TARGET", "precision_target",
                            lo=0.50, hi=0.95) == 0.65

    monkeypatch.setenv("V3_PRECISION_TARGET", "0.50")
    assert ac.resolve_float("V3_PRECISION_TARGET", "precision_target",
                            lo=0.50, hi=0.95) == 0.55, "env weakened the contract floor"

    monkeypatch.delenv("V3_PRECISION_TARGET", raising=False)
    monkeypatch.delenv("GHOST_ACCURACY_CONTRACT", raising=False)
    importlib.reload(ac)


def test_fold_count_cannot_be_lowered_by_env_under_contract_55(monkeypatch):
    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "55")
    importlib.reload(ac)

    monkeypatch.setenv("V3_MIN_WF_FOLDS", "1")
    assert ac.resolve_int("V3_MIN_WF_FOLDS", "min_wf_folds", lo=1) == 4

    monkeypatch.delenv("V3_MIN_WF_FOLDS", raising=False)
    monkeypatch.delenv("GHOST_ACCURACY_CONTRACT", raising=False)
    importlib.reload(ac)


def test_legacy_remains_the_only_weakenable_contract():
    assert "legacy" not in ac._NO_WEAKENING_CONTRACTS
    for name in ("55", "70", "80"):
        assert name in ac._NO_WEAKENING_CONTRACTS
