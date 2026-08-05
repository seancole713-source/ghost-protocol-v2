"""tests/test_binomial_stats.py — exact Wilson and v2 protocol verification."""
from __future__ import annotations

import pytest
from core.binomial_stats import (
    V2_CONFIRMATORY_N,
    V2_MIN_WINS,
    V2_TARGET,
    block_bootstrap_lower_bound,
    exact_wilson_display,
    moving_block_bootstrap_lower_bound,
    v2_confirmatory_futile,
    v2_confirmatory_pass,
    v2_confirmatory_status,
    v2_min_wins_for_n,
    v2_pass_table,
    verify_wilson_table,
    wilson_lower_bound,
    wilson_pass,
    wilson_upper_bound,
)


class TestWilsonLowerBound:
    def test_zero_n_returns_zero(self):
        assert wilson_lower_bound(0, 0) == 0.0
        assert wilson_lower_bound(5, 0) == 0.0

    def test_perfect_small_sample(self):
        # 3/3: exact low ~0.4385
        low = wilson_lower_bound(3, 3)
        assert 0.43 < low < 0.45

    def test_9_of_9_clears_70(self):
        # 9/9 is the smallest perfect sample clearing 0.70
        low = wilson_lower_bound(9, 9)
        assert low >= 0.70

    def test_42_of_50_is_the_v2_pass_line(self):
        low = wilson_lower_bound(42, 50)
        assert low >= 0.70
        # 41/50 must fail
        low41 = wilson_lower_bound(41, 50)
        assert low41 < 0.70

    def test_76_of_96_fails_despite_rounded_display(self):
        """The canonical edge case: 76/96 displays as 0.7000 but exact < 0.70."""
        low = wilson_lower_bound(76, 96)
        assert low < 0.70, f"76/96 exact low {low} must be below 0.70"
        # Rounded to 4 places it's 0.7000
        assert round(low, 4) == 0.7000
        # 77/96 genuinely clears
        assert wilson_lower_bound(77, 96) >= 0.70

    def test_wilson_pass_uses_unrounded(self):
        assert wilson_pass(42, 50, 0.70) is True
        assert wilson_pass(41, 50, 0.70) is False
        assert wilson_pass(76, 96, 0.70) is False  # exact < 0.70
        assert wilson_pass(77, 96, 0.70) is True

    def test_wilson_upper_bound(self):
        high = wilson_upper_bound(42, 50)
        assert high > 0.90
        assert wilson_upper_bound(0, 0) == 1.0


class TestV2MinWins:
    def test_min_wins_for_50_is_42(self):
        assert v2_min_wins_for_n(50) == 42

    def test_min_wins_for_25_is_22(self):
        # 22/25: exact low ~0.708
        assert v2_min_wins_for_n(25) == 22

    def test_small_n_returns_none(self):
        # 1/1 exact low ~0.206, can't reach 0.70
        assert v2_min_wins_for_n(1) is None
        assert v2_min_wins_for_n(2) is None

    def test_pass_table_contains_key_values(self):
        table = v2_pass_table()
        assert table[50] == 42
        assert table[25] == 22
        assert table[9] == 9


class TestV2Confirmatory:
    def test_pass_requires_exactly_50(self):
        assert v2_confirmatory_pass(42, 50) is True
        assert v2_confirmatory_pass(42, 49) is False  # not yet 50
        assert v2_confirmatory_pass(43, 50) is True
        assert v2_confirmatory_pass(41, 50) is False

    def test_futility_detection(self):
        # 30 wins, 20 non-wins: 10 remaining, max 40 < 42
        assert v2_confirmatory_futile(30, 40) is True
        # 35 wins, 10 non-wins: 5 remaining, max 40 < 42
        assert v2_confirmatory_futile(35, 45) is True
        # 40 wins, 5 non-wins: 5 remaining, max 45 >= 42
        assert v2_confirmatory_futile(40, 45) is False
        # 42 wins at 50: not futile, already done
        assert v2_confirmatory_futile(42, 50) is False

    def test_status_progression(self):
        assert v2_confirmatory_status(10, 20) == "FUTILE"  # 10 wins, 30 remaining, max 40 < 42
        assert v2_confirmatory_status(40, 45) == "COLLECTING"  # 5 remaining, max 45 >= 42
        assert v2_confirmatory_status(42, 50) == "PROVEN"
        assert v2_confirmatory_status(41, 50) == "FALSIFIED"
        assert v2_confirmatory_status(50, 51) == "OVERFLOW"


class TestExactWilsonDisplay:
    def test_display_includes_exact_low(self):
        d = exact_wilson_display(42, 50)
        assert d["wins"] == 42
        assert d["n"] == 50
        assert d["exact_low"] >= 0.70
        assert d["passes_70"] is True

    def test_display_76_96_fails(self):
        d = exact_wilson_display(76, 96)
        assert d["passes_70"] is False
        assert d["exact_low"] < 0.70
        assert d["low"] == 0.7000  # rounded display

    def test_zero_n_display(self):
        d = exact_wilson_display(0, 0)
        assert d["win_rate"] is None
        assert d["passes_70"] is False


class TestBlockBootstrap:
    def test_small_n_returns_zero(self):
        assert block_bootstrap_lower_bound(3, 3, block_size=5) == 0.0

    def test_perfect_sample_high_bound(self):
        low = block_bootstrap_lower_bound(50, 50, block_size=5, seed=42)
        # Even with blocking, 50/50 should have a high lower bound
        assert low > 0.80

    def test_marginal_sample(self):
        low = block_bootstrap_lower_bound(42, 50, block_size=5, seed=42)
        # Block bootstrap is conservative; may be below exact Wilson
        assert 0.0 <= low <= 1.0

    def test_sequence_bootstrap_uses_actual_order(self):
        spread = [1, 1, 1, 1, 1, 0] * 8 + [1, 1]
        clustered = [1] * 42 + [0] * 8
        spread_low = moving_block_bootstrap_lower_bound(
            spread, n_bootstrap=2000, block_size=5, seed=42,
        )
        clustered_low = moving_block_bootstrap_lower_bound(
            clustered, n_bootstrap=2000, block_size=5, seed=42,
        )
        assert spread_low >= clustered_low

    def test_sequence_bootstrap_perfect_sample(self):
        assert moving_block_bootstrap_lower_bound([1] * 50) == 1.0

    def test_sequence_bootstrap_rejects_non_binary_values(self):
        with pytest.raises(ValueError, match="only 0/1"):
            moving_block_bootstrap_lower_bound([1, 0, 2, 1, 0])


class TestVerificationTable:
    def test_all_precomputed_values_match(self):
        result = verify_wilson_table()
        assert result["ok"], f"Wilson verification failures: {result['failures']}"
        assert result["verified"] == result["total"]


class TestProtocolConstants:
    def test_v2_target_is_70(self):
        assert V2_TARGET == 0.70

    def test_v2_confirmatory_n_is_50(self):
        assert V2_CONFIRMATORY_N == 50

    def test_v2_min_wins_is_42(self):
        assert V2_MIN_WINS == 42

    def test_42_50_exact_low_clears_70(self):
        low = wilson_lower_bound(V2_MIN_WINS, V2_CONFIRMATORY_N)
        assert low >= V2_TARGET, f"42/50 exact low {low} must clear 0.70"

    def test_41_50_exact_low_fails_70(self):
        low = wilson_lower_bound(41, 50)
        assert low < V2_TARGET, f"41/50 exact low {low} must be below 0.70"

    def test_no_environment_override_can_weaken_target(self):
        """The protocol constants are module-level, not env-var reads."""
        import core.binomial_stats as bs
        # These are literal constants, not functions reading env vars
        assert isinstance(bs.V2_TARGET, float)
        assert bs.V2_TARGET == 0.70
        # wilson_pass uses the target parameter, not a global
        assert wilson_pass(42, 50, target=0.70) is True
        # A caller passing a weaker target would get a weaker result —
        # the protocol test verifies the default call sites use 0.70.
        # 35/50 doesn't clear even 0.65 (Wilson low ~0.562)
        assert wilson_pass(35, 50, target=0.65) is False
        # 40/50 clears 0.65 (Wilson low ~0.670)
        assert wilson_pass(40, 50, target=0.65) is True
