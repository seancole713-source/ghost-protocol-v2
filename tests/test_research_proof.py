"""Tests for core/research_proof.py — forward proof engine."""
import pytest
from core.research_proof import (
    wilson_interval,
    compute_proof,
    ProofResult,
    ensure_research_forward_registrations,
    register_forward_experiment,
    get_forward_registration,
    evaluate_forward_proof,
)


# ── Wilson interval ────────────────────────────────────────────────────────

def test_wilson_interval_perfect():
    wi = wilson_interval(10, 10)
    assert wi["point"] == 1.0
    assert wi["low"] > 0.65  # small-sample Wilson is conservative


def test_wilson_interval_zero_n():
    wi = wilson_interval(0, 0)
    assert wi["point"] == 0.0
    assert wi["low"] == 0.0


def test_wilson_interval_70_pct_large_n():
    wi = wilson_interval(70, 100)
    assert wi["point"] == 0.7
    assert wi["low"] < 0.7  # Wilson lower bound is below point estimate


def test_wilson_interval_88_pct_60_n():
    """~88% win rate with n=60 should clear 70% Wilson lower bound."""
    wi = wilson_interval(53, 60)
    assert wi["low"] >= 0.70


# ── compute_proof ──────────────────────────────────────────────────────────

def test_compute_proof_all_wins():
    # 3/3 wins has Wilson low ~0.44 — too few samples to prove 70%
    preds = [
        {"outcome": "WIN", "calibrated_prob": 0.75, "artifact_sha": "a" * 64, "contract_id": "abc"},
        {"outcome": "WIN", "calibrated_prob": 0.80, "artifact_sha": "a" * 64, "contract_id": "abc"},
        {"outcome": "WIN", "calibrated_prob": 0.72, "artifact_sha": "a" * 64, "contract_id": "abc"},
    ]
    result = compute_proof(preds, min_support=3)
    assert result.wins == 3
    assert result.losses == 0
    assert result.win_rate == 1.0
    # 3/3 is not enough for Wilson to clear 0.70
    assert result.proven is False
    assert result.wilson["low"] < 0.70


def test_compute_proof_mixed():
    preds = [
        {"outcome": "WIN", "calibrated_prob": 0.70, "artifact_sha": "a" * 64, "contract_id": "abc"},
        {"outcome": "WIN", "calibrated_prob": 0.75, "artifact_sha": "a" * 64, "contract_id": "abc"},
        {"outcome": "LOSS", "calibrated_prob": 0.60, "artifact_sha": "a" * 64, "contract_id": "abc"},
        {"outcome": "WIN", "calibrated_prob": 0.80, "artifact_sha": "a" * 64, "contract_id": "abc"},
    ]
    result = compute_proof(preds, min_support=4)
    assert result.wins == 3
    assert result.losses == 1
    assert result.win_rate == 0.75
    # 3/4 = 75%, Wilson low ~ 0.30 — not proven at 70% with n=4
    assert result.proven is False


def test_compute_proof_with_expired():
    preds = [
        {"outcome": "WIN", "calibrated_prob": 0.75, "artifact_sha": "a" * 64, "contract_id": "abc"},
        {"outcome": "EXPIRED", "calibrated_prob": 0.70, "artifact_sha": "a" * 64, "contract_id": "abc"},
    ]
    result = compute_proof(preds, min_support=2, expired_is_non_win=True)
    assert result.wins == 1
    assert result.expired == 1
    assert result.actionable == 2
    assert result.win_rate == 0.5


def test_compute_proof_with_data_invalid():
    preds = [
        {"outcome": "WIN", "calibrated_prob": 0.75, "artifact_sha": "a" * 64, "contract_id": "abc"},
        {"outcome": "DATA_INVALID", "artifact_sha": "a" * 64, "contract_id": "abc"},
    ]
    result = compute_proof(preds, min_support=1)
    assert result.data_invalid == 1
    assert result.actionable == 1  # DATA_INVALID excluded
    assert result.invalid_rate == 0.5


def test_compute_proof_brier():
    preds = [
        {"outcome": "WIN", "calibrated_prob": 0.80, "artifact_sha": "a" * 64, "contract_id": "abc"},
        {"outcome": "LOSS", "calibrated_prob": 0.30, "artifact_sha": "a" * 64, "contract_id": "abc"},
    ]
    result = compute_proof(preds, min_support=2)
    assert result.brier is not None
    # Brier = ((0.8-1)^2 + (0.3-0)^2) / 2 = (0.04 + 0.09) / 2 = 0.065
    assert abs(result.brier - 0.065) < 0.001


def test_proof_result_is_frozen():
    pr = ProofResult(
        artifact_sha="a" * 64, contract_id="abc", window="forward",
        total_predictions=10, actionable=8, wins=6, losses=2,
    )
    with pytest.raises(Exception):
        pr.wins = 7  # type: ignore


# ── forward registration ───────────────────────────────────────────────────

@pytest.mark.integration
def test_register_and_get_forward():
    from core.db import db_conn
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_research_forward_registrations(cur)
        assert register_forward_experiment(
            contract_id="abc", artifact_sha="a" * 64,
            universe_symbols=["WOLF", "NVDA"], threshold=0.55,
            cur=cur,
        )
        conn.commit()

    with db_conn() as conn:
        cur = conn.cursor()
        reg = get_forward_registration("abc", "a" * 64, cur=cur)
        assert reg is not None
        assert reg["threshold"] == 0.55
        assert "WOLF" in reg["universe_symbols"]


@pytest.mark.integration
def test_forward_registration_idempotent():
    from core.db import db_conn
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_research_forward_registrations(cur)
        assert register_forward_experiment(
            contract_id="idem", artifact_sha="b" * 64,
            universe_symbols=["WOLF"], threshold=0.55, cur=cur,
        )
        assert not register_forward_experiment(
            contract_id="idem", artifact_sha="b" * 64,
            universe_symbols=["WOLF"], threshold=0.55, cur=cur,
        )
        conn.commit()


# ── evaluate_forward_proof ─────────────────────────────────────────────────

def test_evaluate_forward_proof_only_counts_after_registration():
    reg = {
        "registered_at_ts": 1_720_000_000,
        "threshold": 0.50,
        "universe_symbols": ["WOLF"],
        "target_wilson_low": 0.70,
        "min_support": 3,
    }
    preds = [
        {"issued_ts": 1_720_000_001, "symbol": "WOLF", "calibrated_prob": 0.75,
         "outcome": "WIN", "artifact_sha": "a" * 64, "contract_id": "abc"},
        {"issued_ts": 1_720_000_002, "symbol": "WOLF", "calibrated_prob": 0.80,
         "outcome": "WIN", "artifact_sha": "a" * 64, "contract_id": "abc"},
        {"issued_ts": 1_719_999_999, "symbol": "WOLF", "calibrated_prob": 0.90,
         "outcome": "WIN", "artifact_sha": "a" * 64, "contract_id": "abc"},  # before registration
    ]
    result = evaluate_forward_proof(preds, reg)
    assert result.total_predictions == 2  # only the 2 after registration
    assert result.wins == 2


def test_evaluate_forward_proof_respects_threshold():
    reg = {
        "registered_at_ts": 1_720_000_000,
        "threshold": 0.70,
        "universe_symbols": ["WOLF"],
        "target_wilson_low": 0.70,
        "min_support": 1,
    }
    preds = [
        {"issued_ts": 1_720_000_001, "symbol": "WOLF", "calibrated_prob": 0.75,
         "outcome": "WIN", "artifact_sha": "a" * 64, "contract_id": "abc"},
        {"issued_ts": 1_720_000_002, "symbol": "WOLF", "calibrated_prob": 0.60,
         "outcome": "WIN", "artifact_sha": "a" * 64, "contract_id": "abc"},  # below threshold
    ]
    result = evaluate_forward_proof(preds, reg)
    assert result.total_predictions == 1  # only the one above threshold
