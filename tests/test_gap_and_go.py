"""Gap-and-Go v1: the frozen rule's arithmetic.

These numbers go into real bracket orders at $1,000 per trade. A rounding slip
on a stop is a real loss, so the levels are code, not 9am mental math -- and
the frozen constants are pinned here so no later edit slips through unnoticed.
"""
from __future__ import annotations

import pytest

import core.gap_and_go as g


def test_frozen_constants_have_not_moved():
    """Changing any of these is v2 with a new ledger -- not an edit."""
    assert g.RULE_VERSION == "gap_and_go_v1"
    assert (g.SIZE_USD, g.ENTRY_TRIGGER, g.ENTRY_LIMIT, g.TARGET, g.STOP) == (1000.0, 1.01, 1.02, 1.05, 0.97)
    assert (g.MIN_MOVE_PCT, g.MAX_MOVE_PCT, g.MIN_PRICE, g.MAX_PRICE) == (5.0, 40.0, 2.0, 500.0)
    assert (g.MIN_AVG_SHARES, g.MIN_AVG_DOLLARS) == (500_000, 5_000_000.0)
    assert g.MAX_TRADES_PER_DAY == 2 and g.PAUSE_LINE_USD == -300.0


def test_break_even_is_37_5_percent():
    assert g.BREAK_EVEN_WIN_RATE == pytest.approx(0.375)


def test_levels_for_a_9_40_reference():
    lv = g.levels(9.40)
    assert lv["entry_stop"] == 9.49
    assert lv["entry_limit"] == 9.59
    assert lv["target"] == 9.96      # 9.49 x 1.05 = 9.9645
    assert lv["stop"] == 9.21        # 9.49 x 0.97 = 9.2053
    assert lv["shares"] == 105       # floor(1000 / 9.49)
    assert lv["max_loss_usd"] == pytest.approx(29.40)


def test_position_never_exceeds_the_size():
    for ref in (2.0, 3.33, 9.99, 47.12, 137.92, 499.0):
        lv = g.levels(ref)
        assert lv["shares"] * lv["entry_stop"] <= 1000.0
        assert lv["max_loss_usd"] <= 31.0   # ~3% of $1,000, plus cent rounding


def test_a_bad_reference_is_refused():
    for bad in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            g.levels(bad)


def test_eligibility_reports_every_failure():
    fails = g.eligibility_failures(
        move_pct=46.0, price=1.50, avg_shares=90_000, avg_dollars=135_000,
        has_dated_catalyst=False, dilutive_or_mechanical=True,
    )
    assert [f[:2] for f in fails] == ["E1", "E2", "E3", "E3", "E4", "E5"]


def test_sympathy_with_no_company_news_is_not_eligible():
    """USAR on 2026-09-22: up on the Greenland theme, owns nothing there."""
    fails = g.eligibility_failures(
        move_pct=9.0, price=18.0, avg_shares=6_000_000, avg_dollars=100e6,
        has_dated_catalyst=False, dilutive_or_mechanical=False,
    )
    assert fails == ["E4 no dated company catalyst in the last 24h"]


def test_a_clean_candidate_passes():
    assert g.eligibility_failures(
        move_pct=6.4, price=146.7, avg_shares=9_300_000, avg_dollars=1.3e9,
        has_dated_catalyst=True, dilutive_or_mechanical=False,
    ) == []


# ---------------------------------------------------------------- grading --

def test_grade_win():
    r = g.grade(entry_stop=9.49, shares=105, open_=9.40, high=10.05, low=9.35)
    assert r["outcome"] == "WIN" and r["pnl_usd"] == pytest.approx(105 * (9.96 - 9.49))


def test_grade_loss():
    r = g.grade(entry_stop=9.49, shares=105, open_=9.40, high=9.60, low=9.10)
    assert r["outcome"] == "LOSS" and r["pnl_usd"] < 0


def test_a_bar_touching_both_is_graded_loss():
    """A daily bar can't order the touches. Assume the worse one."""
    r = g.grade(entry_stop=9.49, shares=105, open_=9.40, high=10.20, low=9.00)
    assert r["outcome"] == "LOSS"


def test_never_reaching_the_buy_stop_is_no_fill():
    r = g.grade(entry_stop=9.49, shares=105, open_=9.40, high=9.45, low=9.00)
    assert r["outcome"] == "NO_FILL"


def test_gapping_past_the_limit_is_no_fill():
    """No chasing: the limit exists so a runaway open does not fill."""
    r = g.grade(entry_stop=9.49, shares=105, open_=9.80, high=10.50, low=9.70)
    assert r["outcome"] == "NO_FILL"


def test_a_gap_through_the_stop_fills_at_the_open():
    r = g.grade(entry_stop=9.49, shares=105, open_=9.55, high=10.10, low=9.50)
    assert r["entry_fill"] == 9.55 and r["outcome"] == "WIN"


def test_time_exit_needs_the_330_price():
    r = g.grade(entry_stop=9.49, shares=105, open_=9.40, high=9.80, low=9.30)
    assert r["outcome"] == "UNGRADED"
    r = g.grade(entry_stop=9.49, shares=105, open_=9.40, high=9.80, low=9.30, close_at_exit=9.70)
    assert r["outcome"] == "TIME_EXIT" and r["pnl_usd"] == pytest.approx(105 * 0.21)


def test_operator_fills_are_the_source_of_truth():
    r = g.grade_from_fills(entry_fill=9.51, exit_price=9.96, shares=105, entry_stop=9.49)
    assert r["outcome"] == "WIN" and r["graded_from"] == "operator_fill"
    r = g.grade_from_fills(entry_fill=9.51, exit_price=9.05, shares=105, entry_stop=9.49)
    assert r["outcome"] == "LOSS"   # slipped through the stop -- recorded as it happened
    assert r["pnl_usd"] == pytest.approx(105 * (9.05 - 9.51))
