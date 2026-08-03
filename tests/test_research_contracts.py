"""Tests for core/research_contracts.py — immutable prediction-task contracts."""
import pytest
from core.research_contracts import (
    PredictionContract,
    OutcomeSpec,
    ProofSpec,
    SourceSpec,
    register_contract,
    get_contract,
    get_contract_by_id,
    list_contracts,
    is_live_compatible,
    live_compatible_contract,
    _REGISTRY,
)


# ── helpers ────────────────────────────────────────────────────────────────

def _make_contract(name="test_task", version="v1", **overrides):
    kwargs = {
        "name": name,
        "version": version,
        "description": "Test contract",
        "output_domain": frozenset({"UP", "DOWN"}),
        "outcome_domain": OutcomeSpec(
            terminal_outcomes=frozenset({"WIN", "LOSS", "EXPIRED"}),
            expired_is_non_win=True,
        ),
        "horizon_bars": 3,
        "feature_schema": "test_features_v1",
        "evidence_schema": "test_evidence_v1",
        "validation_schema": "test_validation_v1",
        "resolver_id": "test_resolver/v1",
        "resolver_version": "1.0.0",
        "proof": ProofSpec(),
        "allowed_sources": (
            SourceSpec("daily_ohlcv", required=True),
        ),
        "live_eligible": False,
    }
    kwargs.update(overrides)
    return PredictionContract(**kwargs)


# ── registry immutability ──────────────────────────────────────────────────

def test_contract_id_is_deterministic():
    c1 = _make_contract()
    c2 = _make_contract()
    assert c1.contract_id() == c2.contract_id()
    assert len(c1.contract_id()) == 64


def test_contract_id_changes_on_any_field():
    c1 = _make_contract()
    c2 = _make_contract(horizon_bars=5)
    assert c1.contract_id() != c2.contract_id()


def test_register_returns_contract_id():
    c = _make_contract(name="reg_test", version="v1")
    cid = register_contract(c)
    assert cid == c.contract_id()
    assert len(cid) == 64


def test_register_rejects_duplicate_name_version_different_payload():
    c1 = _make_contract(name="dup_test", version="v1", horizon_bars=3)
    c2 = _make_contract(name="dup_test", version="v1", horizon_bars=5)
    register_contract(c1)
    with pytest.raises(ValueError, match="already registered"):
        register_contract(c2)


def test_register_idempotent_same_payload():
    c1 = _make_contract(name="idem_test", version="v1")
    cid1 = register_contract(c1)
    cid2 = register_contract(c1)  # same object, same payload
    assert cid1 == cid2


def test_get_contract_by_name_version():
    c = _make_contract(name="lookup_test", version="v2")
    register_contract(c)
    found = get_contract("lookup_test", "v2")
    assert found is not None
    assert found.contract_id() == c.contract_id()


def test_get_contract_missing():
    assert get_contract("nonexistent", "v1") is None


def test_get_contract_by_id():
    c = _make_contract(name="id_lookup", version="v1")
    cid = register_contract(c)
    found = get_contract_by_id(cid)
    assert found is not None
    assert found.name == "id_lookup"


def test_get_contract_by_id_missing():
    assert get_contract_by_id("a" * 64) is None


def test_list_contracts_includes_v1():
    contracts = list_contracts()
    names = {c.name for c in contracts}
    assert "tp_sl_swing" in names
    assert "intraday_continuation" in names
    assert "volatility_expansion" in names
    assert "cross_sectional_ranking" in names
    assert "event_reaction" in names


# ── contract invariants ────────────────────────────────────────────────────

def test_live_eligible_only_tp_sl_swing():
    with pytest.raises(ValueError, match="Only tp_sl_swing"):
        _make_contract(name="other", live_eligible=True)


def test_horizon_bars_must_be_positive():
    with pytest.raises(ValueError, match="horizon_bars"):
        _make_contract(horizon_bars=0)


def test_name_and_version_required():
    with pytest.raises(ValueError):
        _make_contract(name="", version="v1")
    with pytest.raises(ValueError):
        _make_contract(name="test", version="")


def test_contract_is_frozen():
    c = _make_contract()
    with pytest.raises(Exception):
        c.name = "changed"  # type: ignore


# ── live-compatibility boundary ────────────────────────────────────────────

def test_is_live_compatible_true_for_tp_sl_swing(monkeypatch):
    c = get_contract("tp_sl_swing", "v1")
    assert c is not None
    # Mock the live schema functions at their source module
    monkeypatch.setattr(
        "core.signal_engine._v3_feature_schema",
        lambda: c.feature_schema,
    )
    monkeypatch.setattr(
        "core.signal_engine._v3_label_schema",
        lambda: c.evidence_schema,
    )
    monkeypatch.setattr(
        "core.signal_engine._v3_validation_schema",
        lambda: c.validation_schema,
    )
    monkeypatch.setattr(
        "core.signal_engine.V3_LABEL_HOLD_BARS",
        c.horizon_bars,
    )
    assert is_live_compatible(c) is True


def test_is_live_compatible_false_for_research_only():
    for name in ("intraday_continuation", "volatility_expansion",
                 "cross_sectional_ranking", "event_reaction"):
        c = get_contract(name, "v1")
        assert c is not None
        assert is_live_compatible(c) is False


def test_is_live_compatible_false_on_schema_mismatch(monkeypatch):
    c = get_contract("tp_sl_swing", "v1")
    assert c is not None
    monkeypatch.setattr(
        "core.signal_engine._v3_feature_schema",
        lambda: "different_schema",
    )
    assert is_live_compatible(c) is False


def test_live_compatible_contract_returns_tp_sl_swing(monkeypatch):
    c = get_contract("tp_sl_swing", "v1")
    assert c is not None
    monkeypatch.setattr(
        "core.signal_engine._v3_feature_schema",
        lambda: c.feature_schema,
    )
    monkeypatch.setattr(
        "core.signal_engine._v3_label_schema",
        lambda: c.evidence_schema,
    )
    monkeypatch.setattr(
        "core.signal_engine._v3_validation_schema",
        lambda: c.validation_schema,
    )
    monkeypatch.setattr(
        "core.signal_engine.V3_LABEL_HOLD_BARS",
        c.horizon_bars,
    )
    live = live_compatible_contract()
    assert live is not None
    assert live.name == "tp_sl_swing"


# ── outcome domain invariants ──────────────────────────────────────────────

def test_tp_sl_outcomes_include_expired():
    c = get_contract("tp_sl_swing", "v1")
    assert c is not None
    assert "EXPIRED" in c.outcome_domain.terminal_outcomes
    assert c.outcome_domain.expired_is_non_win is True


def test_research_only_tasks_have_no_expired():
    for name in ("intraday_continuation", "volatility_expansion",
                 "cross_sectional_ranking", "event_reaction"):
        c = get_contract(name, "v1")
        assert c is not None
        assert "EXPIRED" not in c.outcome_domain.terminal_outcomes


def test_volatility_output_domain_is_not_directional():
    c = get_contract("volatility_expansion", "v1")
    assert c is not None
    assert c.output_domain == frozenset({"EXPAND", "CONTRACT"})
    assert "UP" not in c.output_domain


def test_cross_sectional_output_domain_is_quartile():
    c = get_contract("cross_sectional_ranking", "v1")
    assert c is not None
    assert c.output_domain == frozenset({"TOP_QUARTILE", "BOTTOM_QUARTILE"})


def test_event_reaction_output_domain():
    c = get_contract("event_reaction", "v1")
    assert c is not None
    assert c.output_domain == frozenset({"POSITIVE", "NEGATIVE"})


# ── proof spec defaults ────────────────────────────────────────────────────

def test_proof_spec_defaults():
    ps = ProofSpec()
    assert ps.target_wilson_low == 0.70
    assert ps.min_support == 20
    assert ps.min_forward_support == 10
    assert ps.z_score == 1.96
    assert ps.precision_applicable is True


def test_source_spec_defaults():
    ss = SourceSpec("test_source")
    assert ss.required is True
    assert ss.max_staleness_s == 86400
