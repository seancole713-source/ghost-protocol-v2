"""Task #65, finished: control-arm days, model rows and card grades are split by headline classifier.

The headline classifier decides which names get a forecast (the control arms' APPROVED label),
the model's catalyst features, and the card's keyword verdict, dilution check and Top 10 score.
So, like resolver versions (audit NEW-02), its versions are cohorts and never pooled: the
headline is the current resolver AND the current classifier; anything decided by an older
classifier is shown beside it, labelled, and decides nothing. A row without a tag is dated by
its session day (2026-10-02 on = headlines_v2, earlier = headlines_v1). Nothing stored is
rewritten, and the frozen control-arm designs keep their hashes.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from edge import control as CA, control_v2 as CA2  # noqa: E402
from edge import intraday as I  # noqa: E402
from edge import models as MD, scorecard as SC, top10 as T  # noqa: E402
from edge.catalysts import CLASSIFIER_VERSION  # noqa: E402
from edge.ledger import MemoryStore, cohort_key, resolver_label, row_classifier  # noqa: E402
from edge.resolver import RESOLVER_VERSION  # noqa: E402
from test_edge_control import FakeBars, forecast, radar_row, ts  # noqa: E402
from test_edge_resolver_cohorts import _dataset  # noqa: E402

V2, H1, H2 = "resolver_v2", "headlines_v1", "headlines_v2"
OLD = f"{V2}~{H1}"            # graded by the current resolver, decided by the older classifier


def test_cohort_keys_and_dating():
    assert (RESOLVER_VERSION, CLASSIFIER_VERSION) == (V2, H2)
    assert cohort_key(V2, H2) == V2 and cohort_key(V2, H1) == OLD
    assert row_classifier({}, "2026-10-01") == H1 and row_classifier(None, "2026-10-02") == H2
    assert row_classifier({"classifier_version": H1}, "2026-10-05") == H1      # its own tag wins


# ---- the control arms --------------------------------------------------------------------

def _day(store, table, day, rows, *, day_tag=None, row_tags=None):
    """A control day graded by the current resolver; `day_tag` / `row_tags` are classifier tags
    (None = not recorded, so the session day dates it)."""
    built = []
    for i, (approved, outcome) in enumerate(rows):
        v = {"outcome": outcome, "pnl_usd": 48.0 if outcome == "WIN" else -32.0}
        if table == "edge_control":
            r = {"symbol": f"S{i}", "feed": "iex", "approved": approved,
                 "label": CA.APPROVED if approved else CA.UNAPPROVED,
                 "variants": {k: dict(v) for k in CA.VARIANTS}}
        else:
            arm = {"variants": {k: dict(v) for k in CA2.VARIANTS}}
            arms = ({CA2.APPROVED: arm, CA2.UNAPPROVED: {"variants": {k: {"outcome": CA2.APPROVED_BY_ENTRY}
                                                                     for k in CA2.VARIANTS}}}
                    if approved else {CA2.UNAPPROVED: arm})
            r = {"symbol": f"S{i}", "feed": "iex", "arms": arms}
        r["resolver_version"] = V2
        if row_tags and row_tags[i]:
            r["classifier_version"] = row_tags[i]
        built.append(r)
    rec = {"day": day, "feed": "iex", "complete": True, "resolver_version": V2, "rows": built}
    if day_tag:
        rec["classifier_version"] = day_tag
    store.put(table, day, rec)


@pytest.mark.parametrize("arm,table", [(CA, "edge_control"), (CA2, "edge_control_v2")])
def test_control_headline_is_the_current_resolver_and_the_current_classifier(arm, table):
    st = MemoryStore()
    # 9/30 approvals were decided by headlines_v1 (untagged, dated): optimistic history, not pooled
    _day(st, table, "2026-09-30", [(True, "WIN")] * 3 + [(False, "LOSS")] * 3)
    _day(st, table, "2026-10-02", [(True, "LOSS")])                       # untagged, dated current
    _day(st, table, "2026-10-05", [(True, "LOSS"), (False, "WIN")], day_tag=H2)
    s = arm.summary(st)
    assert s["design_hash"] == arm.DESIGN_HASH and s["days"] == 3
    assert (s["resolver_version"], s["classifier_version"]) == (V2, H2)
    g = s["regimes"]["iex"]
    p = g["variants"][arm.DESIGN["headline_variant"]]
    assert g["sessions"] == 2 and (p["approved"]["filled"], p["approved"]["wins"]) == (2, 0)   # not 5 / 3
    assert (p["unapproved"]["filled"], p["unapproved"]["wins"]) == (1, 1)
    assert "2 sessions" in s["headline"]
    old = s["other_resolvers"][OLD]
    assert old["headline"] is False and old["days"] == 1 and "never the decision" in old["note"]
    assert "decided by" in old["label"] and H1 in old["label"]
    assert old["regimes"]["iex"]["variants"][arm.DESIGN["headline_variant"]]["approved"]["wins"] == 3
    assert set(s["other_resolvers"]) == {OLD}


def test_control_with_only_older_classifier_days_decides_nothing():
    st = MemoryStore()
    _day(st, "edge_control", "2026-09-30", [(True, "WIN"), (False, "LOSS")])
    _day(st, "edge_control_v2", "2026-09-30", [(True, "WIN"), (False, "LOSS")])
    for s in (CA.summary(st), CA2.summary(st)):
        assert s["regimes"] == {} and s["current_regime"] == "iex"
        assert f"nothing graded under {V2} with {H2}" in s["headline"] and "legacy" in s["headline"]
    ex = CA2.v1_exploratory(st)
    assert ex["regimes"] == {} and ex["other_resolvers"][OLD]["days"] == 1 and ex["classifier_version"] == H2


def test_a_rows_own_classifier_tag_outranks_its_days():
    st = MemoryStore()
    _day(st, "edge_control", "2026-10-05", [(True, "WIN"), (True, "LOSS")], day_tag=H2, row_tags=[H1, None])
    s = CA.summary(st)
    hv = CA.DESIGN["headline_variant"]
    assert s["regimes"]["iex"]["variants"][hv]["approved"]["wins"] == 0
    assert s["other_resolvers"][OLD]["regimes"]["iex"]["variants"][hv]["approved"]["wins"] == 1


def test_the_frozen_designs_carry_no_classifier_and_keep_their_hashes():
    assert CA.DESIGN_HASH == "605453cd49128631" and CA2.DESIGN_HASH == "3baa238954712ba2"
    for d in (CA.DESIGN, CA2.DESIGN):
        assert "classifier" not in json.dumps(d) and "headlines" not in json.dumps(d)


@pytest.fixture
def radar(monkeypatch):
    monkeypatch.setenv("EDGE_LIVE_FEED", "iex")
    return MemoryStore()


def _seed(st, day, *, approve=True):
    for s in ("WINR", "LOSR"):
        radar_row(st, s, day=day)
    if approve:
        forecast(st, I.INTRADAY_CONTINUATION, "WINR", day=day)


def test_grading_tags_the_day_and_each_row_with_the_sessions_classifier(radar):
    new, old = date(2026, 10, 5), date(2026, 9, 30)
    _seed(radar, new)                     # its forecast was issued (and tagged) by headlines_v2
    _seed(radar, old, approve=False)      # no forecast to read: dated by the session, headlines_v1
    for arm, table in ((CA, "edge_control"), (CA2, "edge_control_v2")):
        for d in (new, old):
            assert arm.grade_day(FakeBars(), radar, day=d, now=ts(16, 25, d))["status"] == "graded"
        rec_new, rec_old = radar.get(table, new.isoformat()), radar.get(table, old.isoformat())
        assert rec_new["classifier_version"] == H2 and {r["classifier_version"] for r in rec_new["rows"]} == {H2}
        assert rec_old["classifier_version"] == H1 and {r["classifier_version"] for r in rec_old["rows"]} == {H1}
        assert rec_new["design_hash"] == arm.DESIGN_HASH                     # the design is untouched
        s = arm.summary(radar)
        assert s["regimes"]["iex"]["sessions"] == 1 and s["other_resolvers"][OLD]["days"] == 1


def test_the_sessions_classifier_comes_from_its_forecasts():
    st = MemoryStore()
    assert CA.session_classifier(st, "2026-10-05") == H2 and CA.session_classifier(st, "2026-09-30") == H1
    day = date(2026, 9, 30)
    f = forecast(st, I.INTRADAY_CONTINUATION, "AAA", day=day)
    assert CA.session_classifier(st, day.isoformat()) == H2          # its own tag
    row = st.get("forecasts", f.forecast_id)
    row["evidence"] = {k: v for k, v in row["evidence"].items() if k != "classifier_version"}
    st.put("forecasts", f.forecast_id, row)
    assert CA.session_classifier(st, day.isoformat()) == H1          # stored before tags: dated
    forecast(st, I.INTRADAY_CONTINUATION, "BBB", day=day)
    mixed = CA.session_classifier(st, day.isoformat())
    assert mixed == f"{H1}+{H2}"                                      # a mid-session change: mixed
    cohort = CA.row_cohort({"day": day.isoformat(), "classifier_version": mixed}, {"resolver_version": V2})
    assert cohort == f"{V2}~{H1}+{H2}" and "decided by mixed:" in resolver_label(cohort)


def test_a_regraded_day_keeps_the_classifier_its_rows_were_graded_with():
    row = {"symbol": "OLD", "resolver_version": V2, "variants": {k: {"outcome": "WIN"} for k in CA.VARIANTS}}
    both = {"symbol": "TAG", "resolver_version": V2, "classifier_version": H2, "variants": {}}
    kept = CA.tag_kept_rows({"day": "2026-10-05", "classifier_version": H1}, {"OLD": row, "TAG": both})
    assert kept["OLD"]["classifier_version"] == H1 and kept["OLD"]["variants"] == row["variants"]
    assert kept["TAG"] is both and "classifier_version" not in row       # nothing stored is mutated
    assert CA.tag_kept_rows({"day": "2026-09-30"}, {"OLD": row})["OLD"]["classifier_version"] == H1
    assert CA.tag_kept_rows({"day": "2026-10-05"}, {"OLD": row})["OLD"]["classifier_version"] == H2


# ---- model training ------------------------------------------------------------------------

def test_model_training_reads_only_the_current_classifier():
    current = _dataset(V2)
    older = [{**r, "classifier_version": H1} for r in _dataset(V2, invert=True, seed=11)]
    untagged = [{k: v for k, v in r.items() if k != "classifier_version"} for r in _dataset(V2, seed=13)]
    assert all(r["day"] < "2026-10-02" for r in untagged)                 # dated: headlines_v1
    assert MD.training_rows(current + older + untagged) == current
    assert MD.fit(current + older) == MD.fit(current)
    ev = MD.evaluate(current + older)
    assert ev == {**MD.evaluate(current), "rows_other_resolvers": len(older)}
    assert ev["classifier_version"] == H2 and ev["cohort_rows"] == len(current)
    assert MD.fit(older) is None and MD.evaluate(older)["cohort_rows"] == 0
    # an untagged row from a session on or after 2026-10-02 ran the current classifier
    late = {**untagged[0], "day": "2026-10-05"}
    assert MD.training_rows([late]) == [late]


# ---- the card scorecard and the Top 10 ---------------------------------------------------

def _graded(prefix, n, wins, tag=None):
    rows = []
    for i in range(n):
        r = {"symbol": f"{prefix}{i}", "outcome": "WIN" if i < wins else "LOSS",
             "execution": "WIN" if i < wins else "LOSS", "baseline": "ELIGIBLE", "auto": "ELIGIBLE",
             "resolver_version": V2}
        if tag:
            r["classifier_version"] = tag
        rows.append(r)
    return rows


def test_the_scorecard_and_top10_comparison_split_by_classifier():
    st = MemoryStore()
    # 9/30's card ran headlines_v1 (HOOD's day); a late resolver_v2 grade does not make it current
    st.put("edge_card_outcomes", "2026-09-30", {"day": "2026-09-30", "rows": _graded("OLD", 4, 4)})
    st.put("edge_card_outcomes", "2026-10-05", {"day": "2026-10-05", "rows": _graded("NEW", 2, 0, H2)})
    for d, sym in (("2026-09-30", "OLD0"), ("2026-10-05", "NEW0")):
        st.put("edge_top10", d, {"day": d, "list": [{"rank": 1, "symbol": sym}]})
    sc = SC.scorecard(st)
    assert (sc["resolver_version"], sc["classifier_version"]) == (V2, H2)
    assert sc["graded_rows"] == 2 and sc["graded_sessions"] == 1
    assert (sc["keyword_catalyst"]["approved"]["decided"], sc["keyword_catalyst"]["approved"]["wins"]) == (2, 0)
    assert sc["top10"]["sessions"] == 1 and sc["top10"]["approved"]["wins"] == 0
    old = sc["other_resolvers"][OLD]
    assert old["headline"] is False and "decided by" in old["label"] and H1 in old["label"]
    assert (old["base_rate_all_gappers"]["decided"], old["base_rate_all_gappers"]["wins"]) == (4, 4)
    assert old["top10"]["sessions"] == 1 and set(sc["other_resolvers"]) == {OLD}
    # the Top 10 view says which classifier scored each list
    assert T.with_outcomes(st, "2026-09-30")["classifier_version"] == H1
    assert T.with_outcomes(st, "2026-10-05")["classifier_version"] == H2


def test_a_built_top10_records_its_cards_classifier():
    row = {"symbol": "AA", "ref_price": 11.0, "prev_close": 10.0, "avg_dollars": 3e7, "inputs": {"events": []}}
    assert T.build({"day": "2026-10-05", "rows": [{**row, "classifier_version": H2}]})["classifier_version"] == H2
    assert T.build({"day": "2026-09-30", "rows": [row]})["classifier_version"] == H1        # dated


def test_card_grades_carry_the_classifier_of_their_card():
    from test_edge_pipeline import DAY, FakeAlpaca, ts as pts
    from edge import pipeline as P
    from edge.ledger import Ledger
    lg = Ledger(MemoryStore())
    P.morning_card(FakeAlpaca(), lg, now=pts(9, 10))
    card = lg.store.get("edge_cards", DAY.isoformat())
    assert {r.get("classifier_version") for r in card["rows"]} == {H2}
    SC.grade_card(FakeAlpaca("evening"), lg.store, day=DAY, now=pts(16, 25))
    rows = lg.store.get("edge_card_outcomes", DAY.isoformat())["rows"]
    assert rows and {r["classifier_version"] for r in rows} == {H2}
    # a card stored before tags existed is dated by its day (2026-09-23: headlines_v1)
    st = MemoryStore()
    st.put("edge_cards", DAY.isoformat(), {**card, "rows": [{k: v for k, v in r.items() if k != "classifier_version"}
                                                             for r in card["rows"]]})
    SC.grade_card(FakeAlpaca("evening"), st, day=DAY, now=pts(16, 25))
    assert {r["classifier_version"] for r in st.get("edge_card_outcomes", DAY.isoformat())["rows"]} == {H1}
