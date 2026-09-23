"""Replay guard + the daily movers digest."""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "tests")

from edge import catalysts as C, notify as N, replay as RP  # noqa: E402
from edge import pipeline as P  # noqa: E402
from edge.ledger import Ledger, MemoryStore  # noqa: E402
from test_edge_pipeline import FakeAlpaca, ts  # noqa: E402


@pytest.fixture
def ledger():
    lg = Ledger(MemoryStore())
    P.morning_card(FakeAlpaca(), lg, now=ts(9, 10))
    return lg


def test_every_card_row_stores_its_full_inputs(ledger):
    card = ledger.store.get("edge_cards", "2026-09-23")
    shop = next(r for r in card["rows"] if r["symbol"] == "SHOP")
    assert shop["inputs"]["prev_close"] == 137.92 and shop["inputs"]["events"][0]["kind"] == "contract"


def test_todays_code_replays_its_own_card_consistently(ledger):
    out = RP.replay_all(ledger.store)
    assert out["status"] == "consistent" and out["rows_checked"] == 5


def test_a_code_change_that_would_decide_differently_is_caught(ledger, monkeypatch):
    """Simulate a well-meant refactor that stops recognising 'partnership' as a contract."""
    patched = [(k, p) for k, p in C._RULES if k != C.CONTRACT]
    monkeypatch.setattr(C, "_RULES", patched)
    out = RP.replay_all(ledger.store)
    assert out["status"] == "drift"
    assert {"symbol": "SHOP", "strategy": "premarket_continuation", "recorded": "ELIGIBLE",
            "replayed": "REJECTED", "day": "2026-09-23"} in out["drift"]


def test_the_digest_says_what_was_catchable_and_why_it_was_missed():
    review = {"day": "2026-09-22", "movers": 14, "executable": 6, "caught": 1, "gap_only": 5,
              "labels": {"DETECTION_FAILURE": 3, "STRATEGY_REJECTION": 2, "DATA_INTERRUPTION": 0},
              "rows": [{"symbol": "GRML", "opportunity": "EXECUTABLE", "move_pct": 43.6, "label": "DETECTION_FAILURE"},
                       {"symbol": "SHOP", "opportunity": "EXECUTABLE", "move_pct": 6.4, "label": "CAUGHT"}]}
    t = N.misses_text(review)
    assert "14 stocks hit +5% at their peak, 6 were tradeable after the open, caught 1" in t
    assert "Biggest (peak / close)" in t
    assert "detection failure 3" in t and "data interruption" not in t
    assert "GRML +44% (detection failure)" in t and "5 more gained only in the gap" in t
    assert N.misses_text({"movers": 0}) is None



def test_the_digest_shows_where_a_spike_closed_not_only_its_peak():
    from edge import notify as N
    t = N.misses_text({"day": "2026-09-22", "movers": 1, "executable": 1, "caught": 0,
                       "rows": [{"symbol": "PAAI", "opportunity": "EXECUTABLE", "move_pct": 44.0,
                                 "close_pct": -6.85, "label": "UNIVERSE_COVERAGE"}]})
    assert "PAAI +44% / -7%" in t
