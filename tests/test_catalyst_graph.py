"""tests/test_catalyst_graph.py — cross-symbol catalyst propagation."""
from __future__ import annotations

import core.catalyst_graph as cg


def test_peers_of_mrna_cohort():
    peers = cg.peers_of("MRNA")
    assert "ARCT" in peers
    assert "BNTX" in peers
    assert "MRNA" not in peers  # no self


def test_arct_belongs_to_mrna_group():
    assert "mrna_vaccine" in cg.groups_for_symbol("ARCT")


def test_propagate_fda_approval_to_peers():
    out = cg.propagate_catalyst(
        "MRNA", "fda_approval", materiality=0.9, asof_ts=1780000000,
        direction_hint="bullish",
    )
    syms = {o["symbol"] for o in out}
    assert "ARCT" in syms
    assert "BNTX" in syms
    for o in out:
        assert o["derived"] is True
        assert o["origin_symbol"] == "MRNA"
        assert o["event_type"] == "fda_approval"
        # Sector repricing is weaker than a direct event.
        assert o["materiality"] < 0.9


def test_no_propagation_for_non_catalyst_event():
    out = cg.propagate_catalyst("MRNA", "officer_change", materiality=0.9)
    assert out == []


def test_no_propagation_below_materiality_floor():
    out = cg.propagate_catalyst("MRNA", "fda_approval", materiality=0.5)
    assert out == []
