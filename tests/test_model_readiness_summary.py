"""Regression tests for the model-readiness/liveness boundary."""


def test_model_readiness_reads_authoritative_stored_symbols():
    import wolf_app

    out = wolf_app._model_readiness_summary({
        "fleet_summary": {"serveable": 1, "fireable_now": 0, "precision_ok": 1},
        "stored_symbols": {
            "ABC_up": {"serveable": True, "fire_block_reason": "precision_unproven"},
        },
    })

    assert out["ready_to_trade"] is False
    assert out["fire_block_reasons"] == [
        {"model": "ABC_up", "reason": "precision_unproven"}
    ]


def test_model_readiness_does_not_claim_ready_when_count_is_unknown():
    import wolf_app

    out = wolf_app._model_readiness_summary({"fleet_summary": {"fireable_now": None}})

    assert out["ready_to_trade"] is None
