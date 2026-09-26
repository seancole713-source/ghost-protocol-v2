"""edge operations: three records, real fills, the radar, data health, portfolio risk.

Each test is a way a trading system misleads the person using it from a phone.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from edge import fills as F, health as H, radar as R, risk, stats
from edge.contracts import GAP_AND_GO_V1, ContractError, issue
from edge.ledger import Ledger, MemoryStore
from edge.resolver import resolve_execution, resolve_market

ET = ZoneInfo("America/New_York")
DAY = date(2026, 9, 23)


def ts(hh, mm):
    return int(datetime(2026, 9, 23, hh, mm, tzinfo=ET).timestamp())


def fc(sym="ABCD", ref=9.40):
    return issue(GAP_AND_GO_V1, symbol=sym, session_date=DAY, entry_ref=ref, issued_at=ts(9, 10))


def bars(*rows):
    return [(ts(h, m), o, hi, lo, c, 1000) for h, m, o, hi, lo, c in rows]


# ----------------------------------------------------- forecast vs execution --

def test_a_right_forecast_can_be_an_unfilled_order():
    """Gapped past the limit and never came back: the prediction came true,
    the order never filled. Both facts must survive."""
    f = fc()
    tape = bars((9, 30, 9.80, 10.05, 9.75, 10.00), (10, 30, 10.10, 10.20, 10.00, 10.15))
    assert resolve_market(f, tape).outcome == "WIN"
    assert resolve_execution(f, tape).outcome == "NO_FILL"


def test_execution_costs_are_charged_and_stated():
    f = fc()
    tape = bars((9, 30, 9.40, 9.50, 9.38, 9.48), (9, 45, 9.80, 9.99, 9.78, 9.95))
    ideal = resolve_market(f, tape)
    real = resolve_execution(f, tape, cost_bps_per_side=10)
    assert real.pnl_usd < ideal.pnl_usd
    assert "cost_bps:10" in real.flags


def test_ledger_keeps_three_records_apart():
    lg = Ledger(MemoryStore())
    lg.register(GAP_AND_GO_V1, now=ts(8, 0))
    f = fc()
    lg.record(f, now=ts(9, 11))
    tape = bars((9, 30, 9.80, 10.05, 9.75, 10.00), (10, 30, 10.10, 10.20, 10.00, 10.15))
    lg.settle(f.forecast_id, resolve_market(f, tape), now=ts(16, 0), record="forecast")
    lg.settle(f.forecast_id, resolve_execution(f, tape), now=ts(16, 0), record="simulated")
    rep = lg.report("gap_and_go@v1")
    assert rep["records"]["forecast"]["wins"] == 1
    assert rep["records"]["simulated"]["by_outcome"] == {"NO_FILL": 1}
    assert rep["records"]["actual"]["by_outcome"] == {"PENDING": 1}


def test_an_excluded_forecast_takes_no_outcome_in_any_record():
    lg = Ledger(MemoryStore())
    lg.register(GAP_AND_GO_V1, now=ts(8, 0))
    f = fc()
    lg.record(f, now=ts(9, 11))
    lg.exclude(f.forecast_id, reason="halted 09:31-11:40, through the entry window", now=ts(12, 0))
    with pytest.raises(ContractError):
        lg.settle(f.forecast_id, resolve_market(f, []), now=ts(16, 0), record="forecast")
    assert lg.report("gap_and_go@v1")["excluded"] == 1


# ------------------------------------------------------------- actual fills --

def E(h, m, oid, role, status, qty=0, px=None, note=""):
    return F.OrderEvent(ts(h, m), oid, role, status, qty, px, note)


def test_alert_sent_is_not_position_closed():
    f = fc()
    p = F.reconcile(f, [
        E(9, 25, "e1", F.ENTRY, F.ACCEPTED), E(9, 25, "s1", F.STOP, F.ACCEPTED),
        E(9, 31, "e1", F.ENTRY, F.FILLED, 105, 9.50),
        E(10, 2, "s1", F.STOP, F.TRIGGERED),
    ])
    assert p.state == "EXIT_PENDING" and p.qty_open == 105
    assert F.to_resolution(f, p).outcome == "UNRESOLVED"
    assert any("fill pending" in m for _, m in p.messages)


def test_a_stop_fill_records_the_real_price_not_the_stop_level():
    f = fc()
    p = F.reconcile(f, [
        E(9, 25, "e1", F.ENTRY, F.ACCEPTED), E(9, 25, "s1", F.STOP, F.ACCEPTED),
        E(9, 31, "e1", F.ENTRY, F.FILLED, 105, 9.50),
        E(10, 2, "s1", F.STOP, F.FILLED, 105, 9.02),    # gapped through 9.21
    ])
    r = F.to_resolution(f, p)
    assert r.outcome == "LOSS" and r.exit_price == 9.02
    assert r.pnl_usd == pytest.approx(105 * (9.02 - 9.50))


def test_open_without_a_protective_stop_is_flagged():
    f = fc()
    p = F.reconcile(f, [
        E(9, 25, "e1", F.ENTRY, F.ACCEPTED),
        E(9, 25, "s1", F.STOP, F.REJECTED, note="bracket not supported in this session"),
        E(9, 31, "e1", F.ENTRY, F.FILLED, 105, 9.50),
    ])
    assert "OPEN WITHOUT A CONFIRMED PROTECTIVE STOP" in p.warnings


def test_protection_confirmed_message_fires_once_the_position_exists():
    f = fc()
    p = F.reconcile(f, [
        E(9, 25, "e1", F.ENTRY, F.ACCEPTED), E(9, 25, "s1", F.STOP, F.ACCEPTED),
        E(9, 31, "e1", F.ENTRY, F.FILLED, 105, 9.50),
    ])
    assert p.protected and [m for _, m in p.messages].count("Position filled. Protective order confirmed.") == 1


def test_partial_entry_is_flagged():
    f = fc()
    p = F.reconcile(f, [E(9, 31, "e1", F.ENTRY, F.PARTIAL, 40, 9.50), E(9, 31, "s1", F.STOP, F.ACCEPTED)])
    assert any("partially filled" in w for w in p.warnings)


def test_expired_entry_is_a_no_fill_with_a_plain_message():
    f = fc()
    p = F.reconcile(f, [E(9, 25, "e1", F.ENTRY, F.ACCEPTED), E(10, 30, "e1", F.ENTRY, F.EXPIRED)])
    assert F.to_resolution(f, p).outcome == "NO_FILL"
    assert "Entry expired" in p.messages[-1][1]


def test_a_target_fill_is_an_actual_win_at_the_real_price():
    f = fc()
    p = F.reconcile(f, [
        E(9, 25, "e1", F.ENTRY, F.ACCEPTED), E(9, 25, "s1", F.STOP, F.ACCEPTED), E(9, 25, "t1", F.TARGET, F.ACCEPTED),
        E(9, 31, "e1", F.ENTRY, F.FILLED, 105, 9.50),
        E(10, 14, "t1", F.TARGET, F.FILLED, 105, 9.97),
        E(10, 14, "s1", F.STOP, F.CANCELED),
    ])
    assert p.state == "CLOSED" and p.qty_open == 0 and p.last_exit_role == F.TARGET
    r = F.to_resolution(f, p)
    assert r.outcome == "WIN" and r.exit_price == 9.97 and r.entry_fill == 9.50
    assert r.entry_ts == ts(9, 31) and "entry_after_window" not in r.flags
    assert r.pnl_usd == pytest.approx(105 * (9.97 - 9.50))
    assert r.note == "closed via target" and "record:actual" in r.flags


def test_a_time_exit_sell_is_an_actual_time_exit_not_a_win_or_loss():
    f = fc()
    p = F.reconcile(f, [
        E(9, 25, "e1", F.ENTRY, F.ACCEPTED), E(9, 25, "s1", F.STOP, F.ACCEPTED),
        E(9, 31, "e1", F.ENTRY, F.FILLED, 105, 9.50),
        E(15, 30, "s1", F.STOP, F.CANCELED),
        E(15, 30, "x1", F.TIME_EXIT_ROLE, F.SUBMITTED),
    ])
    assert p.state == "EXIT_PENDING" and F.to_resolution(f, p).outcome == "UNRESOLVED"   # sent is not sold
    p = F.reconcile(f, [
        E(9, 25, "e1", F.ENTRY, F.ACCEPTED), E(9, 25, "s1", F.STOP, F.ACCEPTED),
        E(9, 31, "e1", F.ENTRY, F.FILLED, 105, 9.50),
        E(15, 30, "s1", F.STOP, F.CANCELED),
        E(15, 30, "x1", F.TIME_EXIT_ROLE, F.SUBMITTED),
        E(15, 31, "x1", F.TIME_EXIT_ROLE, F.FILLED, 105, 9.71),      # above entry, below target: still TIME_EXIT
    ])
    r = F.to_resolution(f, p)
    assert p.state == "CLOSED" and r.outcome == "TIME_EXIT" and r.exit_price == 9.71
    assert r.pnl_usd == pytest.approx(105 * (9.71 - 9.50)) and r.note == "closed via time_exit"


def test_a_rejected_entry_is_an_actual_no_fill_never_pending():
    f = fc()
    p = F.reconcile(f, [E(9, 11, "e1", F.ENTRY, F.SUBMITTED),
                        E(9, 11, "e1", F.ENTRY, F.REJECTED, note="insufficient buying power")])
    assert p.state == "ENTRY_REJECTED" and p.qty_bought == 0
    assert p.messages[-1][1] == "Entry rejected by the broker. insufficient buying power"
    r = F.to_resolution(f, p)
    assert r.outcome == "NO_FILL" and r.note == "entry_rejected" and r.pnl_usd is None
    # No broker events at all is NOT a rejection: it stays open for a later reconcile.
    assert F.to_resolution(f, F.reconcile(f, [])).outcome == "UNRESOLVED"


@pytest.mark.parametrize("exit_px,outcome", [(9.99, "WIN"), (9.96, "WIN"), (9.21, "LOSS"), (9.10, "LOSS"),
                                             (9.60, "TIME_EXIT")])
def test_a_manual_close_is_judged_by_where_it_closed_not_by_intent(exit_px, outcome):
    f = fc()
    p = F.reconcile(f, [E(9, 25, "e1", F.ENTRY, F.ACCEPTED), E(9, 31, "e1", F.ENTRY, F.FILLED, 105, 9.50),
                        E(11, 0, "m1", F.MANUAL, F.FILLED, 105, exit_px)])
    assert F.to_resolution(f, p).outcome == outcome


def test_an_entry_filled_after_the_window_is_flagged_in_the_actual_record():
    f = fc()                                   # entry window ends 10:30
    p = F.reconcile(f, [E(9, 25, "e1", F.ENTRY, F.ACCEPTED), E(9, 25, "s1", F.STOP, F.ACCEPTED),
                        E(10, 31, "e1", F.ENTRY, F.FILLED, 105, 9.55),
                        E(11, 5, "t1", F.TARGET, F.FILLED, 105, 9.96)])
    assert F.LATE_WARNING in p.warnings
    r = F.to_resolution(f, p)
    assert r.outcome == "WIN" and r.entry_ts == ts(10, 31) and "entry_after_window" in r.flags
    assert "outside the rule" in r.note
    assert F.filled_after_window(ts(10, 30), f.entry_expiry)          # at the expiry instant is already late
    assert not F.filled_after_window(ts(10, 29), f.entry_expiry) and not F.filled_after_window(None, f.entry_expiry)


# -------------------------------------------------------------------- radar --

def test_a_name_that_ran_away_is_shown_not_hidden():
    item = R.RadarItem("ABCD", "2026-09-23", ts(8, 2), 3.2, strategy="premarket_continuation")
    item.transition(R.WATCHING, ts=ts(8, 5))
    item.transition(R.SETUP_FORMING, ts=ts(9, 0))
    item.transition(R.ENTRY_ELIGIBLE, ts=ts(9, 10))
    item.transition(R.EXPIRED, ts=ts(9, 34), reason="gapped past the permitted entry range")
    item.transition(R.WATCHING, ts=ts(9, 40), strategy="intraday_pullback")
    line = item.describe(current_move_pct=15.0)
    assert line == ("Detected at +3.2%. Currently +15.0%. Original entry expired; "
                    "waiting for a separately validated intraday_pullback setup.")


def test_rejections_need_reasons_and_illegal_jumps_fail():
    item = R.RadarItem("ABCD", "2026-09-23", ts(8, 2), 6.0)
    with pytest.raises(R.TransitionError):
        item.transition(R.REJECTED, ts=ts(8, 3))
    with pytest.raises(R.TransitionError):
        item.transition(R.POSITION_OPEN, ts=ts(8, 3))


def test_an_expired_name_cannot_be_rewatched_under_the_old_setup():
    item = R.RadarItem("ABCD", "2026-09-23", ts(8, 2), 6.0, strategy="premarket_continuation")
    item.transition(R.EXPIRED, ts=ts(9, 34), reason="ran away")
    with pytest.raises(R.TransitionError):
        item.transition(R.WATCHING, ts=ts(9, 40))


# ------------------------------------------------------------------- health --

def test_empty_screens_say_which_kind_of_empty():
    now = ts(9, 0)
    rep = H.assess([
        H.SourceHealth("quotes", now - 20, 120, covered=480, expected=500),
        H.SourceHealth("borrow", None, 86400, error="HTTP 403"),
        H.SourceHealth("news", now - 60, 900),
    ], now)
    blocking = {
        "catalyst_breakout": H.release_allowed(["quotes", "news"], rep),
        "crowded_short_ignition": H.release_allowed(["quotes", "borrow"], rep),
    }
    assert blocking["catalyst_breakout"] == []          # borrow outage does not blind it
    assert blocking["crowded_short_ignition"] == ["borrow: DOWN"]
    assert H.banner(0, blocking) == "No qualifying setups. Partial coverage -- paused: crowded_short_ignition."
    assert H.banner(0, {"catalyst_breakout": []}) == "No qualifying setups. Coverage healthy."
    assert H.banner(0, {"x": ["quotes: STALE"]}).startswith("Trading signals paused.")


def test_a_card_that_recorded_forecasts_never_says_signals_paused_beside_them():
    # Audit 2026-09-25 U52: "Trading signals paused" printed next to a forecast and paper orders.
    from edge import cards, notify as N
    b = H.banner(1, {"x": ["quotes_iex: STALE"]})
    assert "Trading signals paused" not in b
    assert b.startswith("1 setup recorded (shadow + paper only).")
    assert "live release would be paused" in b and "quotes_iex: STALE" in b
    # the phone card still carries it as a data warning; the headline keeps the reason
    assert "DATA WARNING" in (N._data_warning({"health_banner": b}) or "")
    assert cards.morning_headline([], b) == b


def test_low_coverage_is_stale_even_when_recent():
    now = ts(9, 0)
    s = H.SourceHealth("quotes", now - 5, 120, covered=9, expected=107)   # the 2026-09-22 IEX probe
    assert s.status(now) == "STALE"


# --------------------------------------------------------------------- risk --

def test_three_stocks_on_one_catalyst_are_one_bet():
    pol = risk.OPERATOR_V1
    open_ = [risk.OpenRisk("CRML", 31.0, "greenland_minerals"), risk.OpenRisk("USAR", 31.0, "greenland_minerals")]
    probs = risk.portfolio_check(pol, open_, risk.OpenRisk("MP", 31.0, "greenland_minerals"))
    assert any("theme 'greenland_minerals'" in p for p in probs)
    assert risk.portfolio_check(pol, open_[:1], risk.OpenRisk("SHOP", 31.0, "agentic_commerce")) == []


def test_stressed_risk_exceeds_the_stop_distance():
    assert risk.stressed_risk_usd(105, 9.49, 9.21) > 105 * (9.49 - 9.21)


# -------------------------------------------------------------------- stats --

def test_clustered_interval_is_wider_than_pretending_trades_are_independent():
    days = {"d1": [True, True, True], "d2": [False, False, False], "d3": [True, True, False],
            "d4": [False, True, False], "d5": [True, True, True], "d6": [False, False, True]}
    k = sum(sum(v) for v in days.values()); n = sum(len(v) for v in days.values())
    lo_w, hi_w = stats.wilson(k, n)
    lo_c, hi_c = stats.clustered_bootstrap_ci(days)
    assert (hi_c - lo_c) > (hi_w - lo_w) * 0.9


def test_testing_many_strategies_tightens_the_bar():
    assert stats.bonferroni_alpha(0.05, 4) == pytest.approx(0.0125)
