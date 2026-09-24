"""edge detection: detectors, catalysts, strategies, the miss review.

Anchored on things that happened in this build's own history, because those
are the failures it exists to prevent.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from edge import catalysts as C, detectors as D, miss_audit as M, setups as S

ET = ZoneInfo("America/New_York")


def ts(hh, mm, day=23):
    return int(datetime(2026, 9, day, hh, mm, tzinfo=ET).timestamp())


def mbars(start_h, start_m, closes, vol=1000, spread=0.01):
    out = []
    t = ts(start_h, start_m)
    prev = closes[0]
    for c in closes:
        out.append((t, prev, max(prev, c) + spread, min(prev, c) - spread, c, vol))
        prev, t = c, t + 60
    return out


# ---------------------------------------------------------------- detectors --

def test_unknown_is_not_zero():
    assert D.gap(None, 10).state == D.UNKNOWN
    assert D.rvol_time_of_day(5000, [100] * 3).state == D.UNKNOWN          # too little history
    assert D.crowded_short(borrow_fee_pct=None, available_shares=None,
                           short_interest_pct_float=30, short_interest_age_days=3).state == D.UNKNOWN


def test_stale_short_interest_is_unknown_not_evidence():
    s = D.crowded_short(borrow_fee_pct=45, available_shares=1000, short_interest_pct_float=35,
                        short_interest_age_days=33)
    assert s.state == D.UNKNOWN and "33 days old" in s.evidence["missing"]


def test_rvol_compares_like_with_like():
    s = D.rvol_time_of_day(9000, [3000] * 12)
    assert s.state == D.PASS and s.value == 3.0


def test_orb_needs_a_close_not_a_wick():
    bars = mbars(9, 30, [10.0, 10.1, 10.2, 10.1, 10.0])            # OR 9:30-9:34
    wick = bars + [(ts(9, 35), 10.0, 10.5, 9.9, 10.1, 1000)]          # wick above, close inside
    assert D.orb_break(wick, ts(9, 30)).state == D.FAIL
    close = wick + [(ts(9, 36), 10.1, 10.4, 10.1, 10.35, 1000)]
    assert D.orb_break(close, ts(9, 30)).state == D.PASS


def test_vwap_hold_and_acceleration():
    rising = mbars(9, 30, [10 + i * 0.01 for i in range(6)] + [10.06 + i * 0.05 for i in range(6)])
    assert D.vwap_hold(rising).state == D.PASS
    assert D.acceleration(rising).state == D.PASS


def test_liquidity_lists_every_failure():
    s = D.liquidity(price=1.5, avg_shares=90_000)
    assert s.state == D.FAIL and len(s.evidence["reasons"]) == 3


# --------------------------------------------------------------- catalysts --

def test_dilution_is_classified_before_anything_bullish():
    assert C.classify("XYZ announces $50M registered direct offering to fund FDA trial") == C.OFFERING


def test_sympathy_policy_news_is_not_company_specific():
    """USAR on 2026-09-22: up on the Greenland pact, owns nothing there."""
    e = C.make("USAR", "Trump announces Greenland security deal", source="x", url="u",
               published_at=ts(7, 0), first_seen_at=ts(7, 2))
    assert e.kind == C.POLICY and not e.company_specific


def test_news_the_system_had_not_seen_cannot_be_used():
    e = C.make("SHOP", "Shopify announces partnership with Meta", source="x", url="u",
               published_at=ts(8, 0), first_seen_at=ts(9, 20))
    assert C.usable_at([e], "SHOP", issued_at=ts(9, 10)) == []
    assert C.usable_at([e], "SHOP", issued_at=ts(9, 25)) == [e]


def test_one_story_many_outlets_keeps_the_earliest_sighting():
    a = C.make("SHOP", "Shopify announces partnership with Meta Platforms", source="wire", url="1",
               published_at=ts(8, 0), first_seen_at=ts(8, 1))
    b = C.make("SHOP", "Shopify Announces Partnership With Meta Platforms!", source="blog", url="2",
               published_at=ts(8, 5), first_seen_at=ts(8, 20))
    (one,) = C.dedupe([b, a])
    assert one.first_seen_at == ts(8, 1) and set(one.sources_seen) == {"wire", "blog"}


def test_entity_match():
    e = C.make("CRML", "Critical Metals wins Tanbreez permit extension", source="x", url="u",
               published_at=ts(7, 0), first_seen_at=ts(7, 0), tickers=["CRML"])
    assert C.entity_match(e, "Critical Metals Corp") == 1.0


# -------------------------------------------------------------- strategies --

def sigs(**over):
    base = {
        "gap": D.Signal("gap", D.PASS, 7.0), "liquidity": D.Signal("liquidity", D.PASS),
        "catalyst": D.Signal("catalyst", D.PASS), "not_dilutive": D.Signal("not_dilutive", D.PASS),
        "orb_break": D.Signal("orb_break", D.PASS), "rvol_tod": D.Signal("rvol_tod", D.PASS, 3.0),
        "vwap_hold": D.Signal("vwap_hold", D.PASS), "acceleration": D.Signal("acceleration", D.PASS),
        "crowded_short": D.Signal("crowded_short", D.UNKNOWN, evidence={"missing": "borrow fee"}),
    }
    base.update(over)
    return base


def test_a_borrow_outage_does_not_blind_a_catalyst_breakout():
    assert S.decide("catalyst_breakout", sigs()).verdict == S.ELIGIBLE
    d = S.decide("crowded_short_ignition", sigs())
    assert d.verdict == S.DATA_UNAVAILABLE and d.missing == ["crowded_short: borrow fee"]


def test_a_failure_rejects_with_its_reason():
    d = S.decide("premarket_continuation", sigs(catalyst=S.catalyst_signal([])))
    assert d.verdict == S.REJECTED and "sector sympathy" in d.reasons[0]


def test_a_dead_catalyst_feed_is_unavailable_not_rejected():
    d = S.decide("premarket_continuation", sigs(catalyst=S.catalyst_signal(None)))
    assert d.verdict == S.DATA_UNAVAILABLE


def test_only_the_frozen_rule_is_not_marked_a_hypothesis():
    special = {"premarket_continuation", "gap_baseline"}
    assert all(s.note == "v0 hypothesis" for n, s in S.STRATEGIES.items() if n not in special)
    assert S.STRATEGIES["gap_baseline"].note.startswith("baseline")   # the bar, not a hypothesis
    assert not any(s.validated for s in S.STRATEGIES.values())   # nothing is validated yet


# ------------------------------------------------------------- miss review --

def test_a_gap_that_offered_nothing_after_the_open_is_not_a_miss():
    m = M.DayMove("GRML", prev_close=10.0, open=13.0, high=13.2, low=11.0, close=11.5)
    assert M.opportunity(m) == "GAP_ONLY"


def test_minute_bars_settle_ordering():
    m = M.DayMove("ABCD", prev_close=10.0, open=10.0, high=10.8, low=9.6, close=10.5)
    assert M.opportunity(m) == "UNKNOWN_ORDERING"                       # daily bar can't say
    up_first = mbars(9, 30, [10.0, 10.2, 10.4, 10.6, 10.55])
    assert M.opportunity(m, bars=up_first, rth_open_ts=ts(9, 30)) == "EXECUTABLE"
    down_first = mbars(9, 30, [10.0, 9.8, 9.6, 10.0, 10.6])
    assert M.opportunity(m, bars=down_first, rth_open_ts=ts(9, 30)) == "GAP_ONLY"


def test_labels_follow_the_fixed_order():
    radar = {
        "SEEN_REJ": M.RadarRecord(first_seen_ts=ts(9, 0), rejected_reason="E4 no catalyst"),
        "SEEN_LIQ": M.RadarRecord(first_seen_ts=ts(9, 0), rejected_reason="avg volume 90,000 < 500,000"),
        "CAUGHT": M.RadarRecord(first_seen_ts=ts(9, 0), forecast_issued=True, alert_delivered_ts=ts(9, 12)),
        "LATE": M.RadarRecord(first_seen_ts=ts(9, 0), forecast_issued=True, alert_delivered_ts=ts(9, 45)),
    }
    kw = dict(universe={"SEEN_REJ", "SEEN_LIQ", "CAUGHT", "LATE", "DOWN", "UNSEEN", "NEWS"},
              data_down={"DOWN"}, catalyst_symbols={"NEWS"}, radar=radar, alert_deadline_ts=ts(9, 30))
    expect = {"OUTSIDE": "UNIVERSE_COVERAGE", "DOWN": "DATA_INTERRUPTION", "NEWS": "CATALYST_MISSED",
              "UNSEEN": "DETECTION_FAILURE", "SEEN_REJ": "STRATEGY_REJECTION",
              "SEEN_LIQ": "RISK_LIQUIDITY_EXCLUSION", "LATE": "ALERT_EXECUTION_FAILURE", "CAUGHT": "CAUGHT"}
    for sym, lab in expect.items():
        assert M.label(sym, **kw) == lab, sym


def test_the_audit_reports_recall_beside_correct_rejections():
    moves = [
        M.DayMove("RUN", 10, 10.0, 10.8, 9.95, 10.7),      # executable, caught
        M.DayMove("MISS", 10, 10.0, 10.9, 9.99, 10.8),     # executable, rejected -> wrong rejection
        M.DayMove("GAP", 10, 13.0, 13.1, 12.0, 12.2),      # gap only
        M.DayMove("FLAT", 10, 10.0, 10.1, 9.9, 10.0),      # no move
    ]
    radar = {"RUN": M.RadarRecord(ts(9, 0), 10.0, forecast_issued=True, alert_delivered_ts=ts(9, 10)),
             "MISS": M.RadarRecord(ts(9, 0), 10.0, rejected_reason="E4 no catalyst"),
             "FLAT": M.RadarRecord(ts(9, 0), 10.0, rejected_reason="E1 move outside range")}
    rep = M.audit("2026-09-23", moves, universe={"RUN", "MISS", "GAP", "FLAT"}, data_down=set(),
                  catalyst_symbols=set(), radar=radar, alert_deadline_ts=ts(9, 30))
    assert (rep.movers, rep.executable, rep.gap_only, rep.caught) == (3, 2, 1, 1)
    assert rep.recall == 0.5
    assert (rep.correct_rejections, rep.wrong_rejections) == (1, 1)
    assert rep.rejection_precision == 0.5


def test_price_action_and_reverse_splits_are_never_company_catalysts():
    from edge import catalysts as C, setups as S
    for h in ["WHLR Stock Explodes On Volatility As Traders Target Micro-Cap REIT",
              "Why Wheeler Real Estate stock is trading higher today", "12 Real Estate Stocks Moving In Wednesday's Session"]:
        assert C.classify(h) == C.PRICE_ACTION, h
    assert C.classify("Wheeler announces 1-for-9 reverse stock split") == C.REVERSE_SPLIT
    assert C.classify("Board approves 1-for-30 share consolidation") == C.REVERSE_SPLIT
    assert C.classify("Receives FDA approval for Mytesi label") == C.FDA
    assert C.classify("Shareholders approve director slate") == C.OTHER      # bare "approval" is not FDA
    ev = [C.make("WHLR", "Wheeler announces 1-for-9 reverse stock split", source="x", url="u",
                 published_at=1, first_seen_at=1)]
    assert S.dilution_signal(ev).state == "FAIL"                              # E5: not mechanical


def test_market_wraps_never_qualify_a_stock_day1_whlr():
    from edge import catalysts as C
    assert C.classify("Dow Falls 100 Points; General Mills Posts Upbeat Q1 Earnings") == C.PRICE_ACTION
    assert C.classify("Nasdaq jumps 1%; Tesla shares gain") == C.PRICE_ACTION
    assert C.classify("General Mills Posts Upbeat Q1 Earnings") == C.EARNINGS
    assert C.classify("Worthington Enterprises reports fiscal Q1 results, beats estimates") == C.EARNINGS


def test_an_analyst_reiteration_is_not_a_catalyst_but_an_upgrade_is():
    """2026-09-24: 'Guggenheim Reiterates Buy on Everpure, Maintains $150 Price Target'
    qualified P as a catalyst_breakout. Rule E4 counts an UPGRADE, not a repeat."""
    from edge import catalysts as C
    no = ["Guggenheim Reiterates Buy on Everpure, Maintains $150 Price Target",
          "Jefferies Cuts Price Target On DEF", "UBS Downgrades GHI To Neutral"]
    yes = ["Morgan Stanley Upgrades XYZ To Overweight, Raises Price Target To $40",
           "Barclays Raises Price Target On ABC To $30",
           "Needham Initiates Coverage On JKL With Buy, $20 Price Target"]
    for h in no:
        assert C.classify(h) == C.ANALYST_NO_CHANGE and C.ANALYST_NO_CHANGE not in C.COMPANY_SPECIFIC, h
    for h in yes:
        assert C.classify(h) == C.ANALYST, h
