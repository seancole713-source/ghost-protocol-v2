"""A premarket row must not carry yesterday's move.

The change percent used to come from regularMarketChangePercent regardless of
which session produced the price and timestamp. Before the open that field
still reports the PREVIOUS completed session, so a premarket row paired a
timestamp from this morning with a move from yesterday.

Caught live on 2026-09-09: ROIV showed +18.75% on the premarket discovery
screen -- exactly where it CLOSED on 2026-09-08, after its mosliciguat Phase 2b
readout. The row read as a stock moving now. It was a stock that had finished
moving the day before. Anyone reading the list before the bell was reading
yesterday's scoreboard believing it was today's.

Same rule as market_wide_snapshot: one basis, from the session that produced
the price, or none.
"""
from __future__ import annotations

import core.external_screener_ingest as esi


def _quote(state, **over):
    q = {
        "symbol": "ROIV", "marketState": state,
        "regularMarketPrice": 41.48, "regularMarketTime": 1_788_910_800,
        "regularMarketChangePercent": 18.75,      # yesterday's close move
        "preMarketPrice": 41.07, "preMarketTime": 1_788_957_000,
        "preMarketChangePercent": -0.99,          # today, so far
        "postMarketPrice": 41.60, "postMarketTime": 1_788_915_000,
        "postMarketChangePercent": 0.29,
        "regularMarketVolume": 24_595_112,
        "averageDailyVolume3Month": 6_597_504,
    }
    q.update(over)
    return q


# ------------------------------------------------------- the ROIV case --

def test_premarket_uses_the_premarket_move_not_yesterdays_close():
    """THE bug. +18.75% was yesterday's close; -0.99% is today."""
    price, ts, move, basis = esi._quote_point(_quote("PRE"))

    assert basis == "premarket"
    assert move == -0.99, "still reporting the previous session's move"
    assert price == 41.07
    assert ts == 1_788_957_000


def test_postmarket_uses_the_postmarket_move():
    price, ts, move, basis = esi._quote_point(_quote("POST"))

    assert basis == "postmarket"
    assert move == 0.29
    assert price == 41.60


def test_regular_session_is_unchanged():
    """The live-session path must behave exactly as before."""
    price, ts, move, basis = esi._quote_point(_quote("REGULAR"))

    assert basis == "regular"
    assert move == 18.75
    assert price == 41.48


def test_price_and_move_always_come_from_the_same_session():
    """The invariant, stated directly: never mix sessions in one row."""
    pairs = {
        "PRE": (41.07, -0.99),
        "POST": (41.60, 0.29),
        "REGULAR": (41.48, 18.75),
    }
    for state, (want_price, want_move) in pairs.items():
        price, _ts, move, _basis = esi._quote_point(_quote(state))
        assert (price, move) == (want_price, want_move), state


# ------------------------------------------------------------ fallback --

def test_a_missing_premarket_print_falls_back_to_regular_as_a_pair():
    """Before the first premarket trade there is no premarket price. Falling
    back is fine -- falling back to a regular PRICE with a premarket MOVE
    would not be."""
    q = _quote("PRE", preMarketPrice=None, preMarketTime=None)

    price, _ts, move, basis = esi._quote_point(q)

    assert basis == "regular"
    assert (price, move) == (41.48, 18.75)


def test_no_usable_session_returns_nothing_rather_than_a_half_row():
    q = _quote("PRE", preMarketPrice=None, preMarketTime=None,
               regularMarketPrice=None, regularMarketTime=None,
               postMarketPrice=None, postMarketTime=None)

    assert esi._quote_point(q) == (None, None, None, "none")


# ------------------------------------------------------- through parse --

def test_the_parsed_row_carries_the_premarket_move():
    payload = {"finance": {"result": [{"quotes": [_quote("PRE")]}]}}

    row = esi.parse_yahoo_screen(payload, screen="day_gainers",
                                 received_ts=1_788_957_100)[0]

    assert row["move_pct"] == -0.99
    assert row["raw_payload"]["move_basis"] == "premarket"


def test_the_basis_is_recorded_on_the_stored_row():
    """A stored row must be traceable to the session its move came from."""
    payload = {"finance": {"result": [{"quotes": [_quote("REGULAR")]}]}}

    row = esi.parse_yahoo_screen(payload, screen="day_gainers",
                                 received_ts=1_788_911_000)[0]

    assert row["raw_payload"]["move_basis"] == "regular"


def test_most_shorted_still_scores_on_short_float():
    """external_score for that screen is short float, not the move."""
    payload = {"finance": {"result": [{"quotes": [
        _quote("REGULAR", shortPercentOfFloat=0.31)]}]}}

    row = esi.parse_yahoo_screen(payload, screen="most_shorted_stocks",
                                 received_ts=1_788_911_000)[0]

    assert row["external_score"] == 0.31
    assert row["move_pct"] == 18.75
