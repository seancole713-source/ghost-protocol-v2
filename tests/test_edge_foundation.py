"""edge foundation: the contract, the resolver, the ledger's refusals, the stats.

Each refusal the ledger makes is the answer to a way a track record gets
faked -- usually by accident. These tests are the list of those ways.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from edge import risk, stats
from edge.contracts import (
    GAP_AND_GO_V1, ContractError, FrozenSpecError, issue, forecast_id_for,
)
from edge.ledger import Ledger, MemoryStore
from edge.resolver import Resolution, resolve

ET = ZoneInfo("America/New_York")
DAY = date(2026, 9, 23)


def ts(hh, mm):
    return int(datetime(2026, 9, 23, hh, mm, tzinfo=ET).timestamp())


def fc(ref=9.40, **kw):
    return issue(GAP_AND_GO_V1, symbol="ABCD", session_date=DAY, entry_ref=ref,
                 issued_at=ts(9, 10), **kw)


def bars(*rows):
    """rows: (hh, mm, o, h, l, c)"""
    return [(ts(h, m), o, hi, lo, c, 1000) for h, m, o, hi, lo, c in rows]


# ----------------------------------------------------------------- contract --

def test_levels_match_the_frozen_card_arithmetic():
    """Must agree to the cent with the operator's live card (docs/gap_and_go_v1.md).
    Literal values, not an import: edge stays free of Ghost code."""
    f = fc()
    assert (f.entry_trigger, f.entry_limit, f.target, f.stop, f.shares) == (9.49, 9.59, 9.96, 9.21, 105)


def test_a_forecast_after_the_open_is_refused():
    with pytest.raises(ContractError):
        issue(GAP_AND_GO_V1, symbol="ABCD", session_date=DAY, entry_ref=9.4, issued_at=ts(9, 31))


def test_forecast_ids_are_one_per_symbol_and_session():
    assert fc().forecast_id == forecast_id_for("gap_and_go@v1", "abcd", "2026-09-23")


def test_spec_hash_changes_with_any_number():
    assert replace(GAP_AND_GO_V1, stop_mult=0.96).spec_hash() != GAP_AND_GO_V1.spec_hash()


def test_break_even():
    assert GAP_AND_GO_V1.break_even_win_rate() == pytest.approx(0.375)


# ----------------------------------------------------------------- resolver --

def test_clean_win():
    f = fc()   # trigger 9.49, limit 9.59, target 9.96, stop 9.21
    r = resolve(f, bars((9, 30, 9.40, 9.50, 9.38, 9.48), (9, 31, 9.48, 9.70, 9.45, 9.68),
                        (9, 45, 9.80, 9.99, 9.78, 9.95)))
    assert r.outcome == "WIN" and r.entry_fill == 9.49 and r.exit_price == 9.96
    assert r.pnl_usd == pytest.approx(105 * (9.96 - 9.49))


def test_clean_loss_and_gap_through_stop_fills_at_the_open():
    f = fc()
    r = resolve(f, bars((9, 30, 9.40, 9.52, 9.38, 9.50), (9, 50, 9.10, 9.12, 9.00, 9.05)))
    assert r.outcome == "LOSS" and r.exit_price == 9.10   # worse than the 9.21 stop


def test_one_bar_touching_both_is_an_ambiguous_loss():
    f = fc()
    r = resolve(f, bars((9, 30, 9.40, 9.52, 9.38, 9.50), (10, 5, 9.50, 10.10, 9.10, 9.60)))
    assert r.outcome == "LOSS" and r.ambiguous


def test_stop_touched_inside_the_fill_bar_is_a_loss():
    f = fc()
    r = resolve(f, bars((9, 30, 9.40, 9.55, 9.15, 9.30)))
    assert r.outcome == "LOSS" and r.ambiguous


def test_never_triggered_is_no_fill():
    f = fc()
    r = resolve(f, bars((9, 30, 9.40, 9.45, 9.30, 9.35), (10, 30, 9.35, 9.60, 9.30, 9.55)))
    assert r.outcome == "NO_FILL"


def test_gap_past_the_limit_then_fade_back_fills_at_the_limit():
    f = fc()
    r = resolve(f, bars((9, 30, 9.80, 9.85, 9.70, 9.72), (9, 40, 9.70, 9.72, 9.55, 9.60),
                        (10, 0, 9.60, 10.00, 9.58, 9.98)))
    assert r.entry_fill == 9.59 and r.outcome == "WIN"


def test_time_exit_at_the_first_bar_after_1530():
    f = fc()
    r = resolve(f, bars((9, 30, 9.40, 9.52, 9.38, 9.50), (12, 0, 9.50, 9.70, 9.40, 9.60),
                        (15, 30, 9.62, 9.65, 9.55, 9.60)))
    assert r.outcome == "TIME_EXIT" and r.exit_price == 9.62


def test_missing_data_is_unresolved_not_a_loss():
    f = fc()
    assert resolve(f, []).outcome == "UNRESOLVED"
    r = resolve(f, bars((9, 30, 9.40, 9.52, 9.38, 9.50)))
    assert r.outcome == "UNRESOLVED" and r.entry_fill == 9.49


# ------------------------------------------------------------------- ledger --

@pytest.fixture
def ledger():
    lg = Ledger(MemoryStore())
    lg.register(GAP_AND_GO_V1, now=ts(8, 0))
    return lg


def test_a_changed_spec_under_the_same_version_is_refused(ledger):
    with pytest.raises(FrozenSpecError):
        ledger.register(replace(GAP_AND_GO_V1, target_mult=1.04), now=ts(8, 1))
    ledger.register(replace(GAP_AND_GO_V1, version=2, target_mult=1.04), now=ts(8, 1))


def test_recording_after_the_open_is_refused(ledger):
    with pytest.raises(ContractError):
        ledger.record(fc(), now=ts(9, 30))


def test_recording_twice_is_idempotent_but_a_different_forecast_is_refused(ledger):
    f = fc()
    ledger.record(f, now=ts(9, 11))
    ledger.record(f, now=ts(9, 12))
    assert ledger.report("gap_and_go@v1")["forecasts"] == 1
    with pytest.raises(ContractError):
        ledger.record(fc(ref=9.60), now=ts(9, 13))


def test_final_outcomes_cannot_be_rewritten(ledger):
    f = fc()
    ledger.record(f, now=ts(9, 11))
    ledger.settle(f.forecast_id, Resolution("LOSS", pnl_usd=-29.4, exit_price=9.21), now=ts(16, 0))
    with pytest.raises(ContractError):
        ledger.settle(f.forecast_id, Resolution("WIN", pnl_usd=49.35, exit_price=9.96), now=ts(16, 5))


def test_unresolved_can_be_resolved_later(ledger):
    f = fc()
    ledger.record(f, now=ts(9, 11))
    ledger.settle(f.forecast_id, Resolution("UNRESOLVED"), now=ts(16, 0))
    ledger.settle(f.forecast_id, Resolution("WIN", pnl_usd=49.35, exit_price=9.96), now=ts(17, 0))


def test_a_settled_loss_cannot_be_excluded(ledger):
    f = fc()
    ledger.record(f, now=ts(9, 11))
    ledger.settle(f.forecast_id, Resolution("LOSS", pnl_usd=-29.4), now=ts(16, 0))
    with pytest.raises(ContractError):
        ledger.exclude(f.forecast_id, reason="felt unlucky", now=ts(16, 1))


def test_report_states_the_interval_and_an_honest_verdict(ledger):
    for i, (outcome, pnl) in enumerate([("WIN", 49.35), ("LOSS", -29.4), ("WIN", 49.35)]):
        f = issue(GAP_AND_GO_V1, symbol=f"S{i}", session_date=DAY, entry_ref=9.4, issued_at=ts(9, 10))
        ledger.record(f, now=ts(9, 11))
        ledger.settle(f.forecast_id, Resolution(outcome, pnl_usd=pnl), now=ts(16, 0))
    ledger.abstain(experiment_id="gap_and_go@v1", symbol="USAR", session_date="2026-09-23",
                   reasons=["E4 sympathy, no company catalyst"], now=ts(9, 12))
    rep = ledger.report("gap_and_go@v1")
    assert rep["filled"] == 3 and rep["wins"] == 2 and rep["abstentions"] == 1
    assert rep["win_rate_ci"][0] < 0.375 < rep["win_rate_ci"][1]
    assert rep["verdict"].startswith("too few trades (n=3)") and "is not evidence" in rep["verdict"]


def test_three_straight_wins_are_never_edge_shown(ledger):
    """Audit 2026-09-25: 3/3 wins has a Wilson range (44%-100%) wholly above a 37.5% break-even.
    Below MIN_FILLED that is not evidence, in the ledger and in both backtests alike."""
    for i in range(3):
        f = issue(GAP_AND_GO_V1, symbol=f"W{i}", session_date=DAY, entry_ref=9.4, issued_at=ts(9, 10))
        ledger.record(f, now=ts(9, 11))
        ledger.settle(f.forecast_id, Resolution("WIN", pnl_usd=49.35), now=ts(16, 0))
    rep = ledger.report("gap_and_go@v1")
    assert rep["win_rate_ci"][0] > rep["break_even"]            # the interval alone would say "edge"
    assert rep["verdict"] == "too few trades (n=3): Wilson range 44%-100% is not evidence"
    assert "edge shown" not in rep["verdict"]
    from edge import backtest as BT, backtest_postsplit as PS
    sess = [{"day": "2026-09-23", "results": [
        {"experiment": "gap_and_go_auto@v1", "symbol": f"W{i}", "forecast": "WIN", "simulated": "WIN",
         "pnl_usd": 49.35, "ambiguous": False, "stress": {}} for i in range(3)]}]
    assert BT.summarize(sess, 0.375)["experiments"]["gap_and_go_auto@v1"]["verdict"] == rep["verdict"]
    rows = [{"cost_0bps": {"simulated": "WIN", "pnl_usd": 10.0}} for _ in range(3)]
    assert PS._summ(rows, 0, 0.375)["verdict"] == rep["verdict"]


def test_the_shared_verdict_keeps_its_wording_once_the_sample_is_big_enough():
    be = 0.375
    assert stats.break_even_verdict(0, 0, be) == "no filled trades"
    assert stats.break_even_verdict(29, 29, be).startswith("too few trades (n=29)")
    assert stats.break_even_verdict(30, 30, be) == "above break-even across the whole interval"
    assert stats.break_even_verdict(30, 30, be, style="ledger") == "edge shown: the whole interval is above break-even"
    assert stats.break_even_verdict(0, 40, be) == "below break-even across the whole interval"
    assert stats.break_even_verdict(0, 40, be, style="ledger") == "no edge: the whole interval is below break-even"
    assert stats.break_even_verdict(15, 40, be).startswith("undecided")
    assert stats.break_even_verdict(15, 40, be, style="ledger") == "undecided: the interval straddles break-even (37.5%)"


# -------------------------------------------------------------------- stats --

def test_wilson_is_wide_at_twenty_trades():
    lo, hi = stats.wilson(9, 20)
    assert lo < 0.30 and hi > 0.65


def test_trades_needed_to_tell_45_from_break_even():
    assert 200 < stats.trades_needed(0.45, 0.375) < 320


def test_isotonic_is_monotone():
    steps = stats.isotonic_fit([0.5, 0.6, 0.7, 0.8], [1, 0, 1, 1])
    vals = [v for _, v in steps]
    assert vals == sorted(vals)


def test_inverted_calibration_is_detected():
    probs = [0.52] * 30 + [0.75] * 30
    hits = [True] * 20 + [False] * 10 + [True] * 12 + [False] * 18
    assert stats.is_monotonic(stats.calibration_bands(probs, hits)) is False


def test_walk_forward_never_trains_on_the_future():
    for tr, te in stats.walk_forward_folds(100, train=50, test=10):
        assert max(tr) < min(te)


# --------------------------------------------------------------------- risk --

def test_operator_policy_is_coherent():
    assert risk.OPERATOR_V1.problems() == []


def test_ghosts_shipped_settings_are_flagged():
    bad = risk.RiskPolicy(account_usd=25_000, max_position_usd=25_000, max_risk_per_trade_pct=10,
                          max_daily_loss_pct=1, max_trades_per_day=3, pause_line_usd=-300)
    assert any("exceeds the daily limit" in p for p in bad.problems())


def test_sizing_respects_both_caps_and_the_locks():
    d = risk.size(risk.OPERATOR_V1, entry=9.49, stop=9.21, trades_today=0,
                  realised_today_usd=0, experiment_pnl_usd=0)
    assert d.allowed and d.shares == 105 and d.risk_usd <= 50
    d = risk.size(risk.OPERATOR_V1, entry=9.49, stop=9.21, trades_today=0,
                  realised_today_usd=0, experiment_pnl_usd=-300)
    assert not d.allowed and "pause line" in d.reasons[0]


def test_a_bar_that_runs_through_the_stop_limit_band_is_not_a_fill():
    f = fc()   # trigger 9.49, limit 9.59
    # 9:30 bar opens below the trigger and runs to 9.90, closing above the limit: a narrow
    # stop-limit would not have filled (the GLND case). No later bar returns to 9.59 -> NO_FILL.
    r = resolve(f, bars((9, 30, 9.40, 9.95, 9.38, 9.90), (9, 31, 9.90, 9.99, 9.70, 9.80),
                        (10, 31, 9.80, 9.85, 9.75, 9.80)))
    assert r.outcome == "NO_FILL" and "limit" in r.note
    # ...but if price comes back to the limit inside the window, the resting limit fills there.
    r = resolve(f, bars((9, 30, 9.40, 9.95, 9.38, 9.90), (9, 31, 9.90, 9.92, 9.55, 9.60),
                        (9, 45, 9.80, 9.99, 9.78, 9.97)))
    assert r.outcome == "WIN" and r.entry_fill == 9.59
