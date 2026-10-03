"""Task #65: the headline classifier is versioned; its versions are cohorts, never pooled.

The 2026-10-03 replay found HOOD 2026-09-30: recorded ELIGIBLE (a Gap-and-Go v1 forecast), today
REJECTED -- "Robinhood Announces Cboe Earnings Contracts..." stopped counting as an earnings
catalyst in #229. A code change in what a frozen rule selects is a new version, so (operator
decision, option 1): forecasts keep the classifier that decided them, sessions before 2026-10-02
are headlines_v1, the headline reads only the current classifier, and the replay flags only
drift under the classifier that made the card.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from edge import catalysts as C, replay as RP
from edge.contracts import issue
from edge.ledger import Ledger, MemoryStore, resolver_label
from edge.pipeline import EXPERIMENTS, GAP_AND_GO_AUTO
from edge.resolver import RESOLVER_VERSION, Resolution

ET = ZoneInfo("America/New_York")
EID = GAP_AND_GO_AUTO.experiment_id
HOOD = "Robinhood Announces Cboe Earnings Contracts On Company Metrics In Prediction Markets Hub, Rolling Out In Coming Weeks"


def ts(d: date, hh: int, mm: int) -> int:
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET).timestamp())


def ledger() -> Ledger:
    lg = Ledger(MemoryStore())
    for spec in EXPERIMENTS:
        lg.register(spec, now=ts(date(2026, 9, 21), 8, 0))
    return lg


def record(lg: Ledger, sym: str, day: date, *, untagged: bool = False):
    f = issue(GAP_AND_GO_AUTO, symbol=sym, session_date=day, entry_ref=10.0, issued_at=ts(day, 9, 10))
    if untagged:                         # a forecast stored before classifier tags existed
        f.evidence.pop("classifier_version")
    lg.record(f, now=ts(day, 9, 11))
    lg.settle(f.forecast_id, Resolution("WIN", pnl_usd=49.0, resolver_version=RESOLVER_VERSION),
              now=ts(day, 16, 25), record="forecast")
    return f


def test_new_forecasts_carry_the_current_classifier():
    f = issue(GAP_AND_GO_AUTO, symbol="X", session_date=date(2026, 10, 5), entry_ref=10.0,
              issued_at=ts(date(2026, 10, 5), 9, 0))
    assert f.evidence["classifier_version"] == C.CLASSIFIER_VERSION == "headlines_v2"
    # the classifier that changed HOOD's verdict is the current one
    assert C.classify(HOOD) == "product_news"


def test_untagged_forecasts_are_dated_and_old_ones_leave_the_headline():
    lg = ledger()
    old = record(lg, "HOOD", date(2026, 9, 30), untagged=True)
    fri = record(lg, "APLD", date(2026, 10, 2), untagged=True)       # ran #229's classifier
    new = record(lg, "ABCD", date(2026, 10, 5))
    assert lg.cohort_of(lg.store.get("forecasts", old.forecast_id)) == f"{RESOLVER_VERSION}~headlines_v1"
    assert lg.cohort_of(lg.store.get("forecasts", fri.forecast_id)) == RESOLVER_VERSION
    assert lg.cohort_of(lg.store.get("forecasts", new.forecast_id)) == RESOLVER_VERSION
    rep = lg.report(EID)
    assert rep["forecasts_in_cohort"] == 2                          # HOOD 9/30 is not pooled in
    other = rep["other_resolvers"][f"{RESOLVER_VERSION}~headlines_v1"]
    assert other["forecasts"] == 1 and other["headline"] is False
    assert "headlines_v1" in other["label"] and "HOOD" in other["label"]
    assert "decided by" in resolver_label(f"{RESOLVER_VERSION}~headlines_v1")


def _card(day: str, tagged: bool):
    inputs = {"ref_price": 122.28, "prev_close": 116.22, "avg_shares": 30_000_000,
              "events": [{"headline": HOOD, "url": "", "published_at": 1, "first_seen_at": 1}]}
    row = {"symbol": "HOOD", "verdict": "ELIGIBLE", "baseline_verdict": "ELIGIBLE", "inputs": inputs}
    if tagged:
        row["classifier_version"] = C.CLASSIFIER_VERSION
    return {"day": day, "rows": [row]}


def test_replay_explains_old_classifier_rows_and_still_flags_same_classifier_drift():
    store = MemoryStore()
    store.put("edge_cards", "2026-09-30", _card("2026-09-30", tagged=False))
    out = RP.replay_all(store)
    assert out["status"] == "consistent" and out["drift"] == []
    assert out["explained_by_classifier_change"][0]["symbol"] == "HOOD"
    assert out["explained_by_classifier_change"][0]["classifier"] == "headlines_v1"
    # The same mismatch on a card decided by the CURRENT classifier is real drift.
    store.put("edge_cards", "2026-10-05", _card("2026-10-05", tagged=True))
    out = RP.replay_all(store)
    assert out["status"] == "drift" and out["drift"][0]["day"] == "2026-10-05"
