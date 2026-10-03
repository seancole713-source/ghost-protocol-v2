"""Task #65, finished: control-arm days, model rows and card grades are split by headline classifier.

The headline classifier decides which names get a forecast (the control arms' APPROVED label),
the model's catalyst features, and the card's keyword verdict, dilution check and Top 10 score.
So, like resolver versions (audit NEW-02), its versions are cohorts and never pooled: the
headline is the current resolver AND the current classifier; anything decided by an older
classifier is shown beside it, labelled, and decides nothing. headlines_v3 (2026-10-05) is the
current classifier and every row it decides carries its tag; a row without a tag is dated by its
session day (2026-10-02 on = headlines_v2, earlier = headlines_v1), both now legacy cohorts.
Nothing stored is rewritten, and the frozen control-arm designs keep their hashes.
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
from edge.catalysts import CLASSIFIER_LABELS, CLASSIFIER_VERSION  # noqa: E402
from edge.ledger import MemoryStore, cohort_key, resolver_label, row_classifier  # noqa: E402
from edge.resolver import RESOLVER_VERSION  # noqa: E402
from test_edge_control import FakeBars, forecast, radar_row, ts  # noqa: E402
from test_edge_resolver_cohorts import _dataset  # noqa: E402

V2, H1, H2, H3 = "resolver_v2", "headlines_v1", "headlines_v2", "headlines_v3"
OLD = f"{V2}~{H1}"            # graded by the current resolver, decided by the oldest classifier
LEG = f"{V2}~{H2}"            # graded by the current resolver, decided by headlines_v2 (legacy now)


def test_cohort_keys_and_dating():
    assert (RESOLVER_VERSION, CLASSIFIER_VERSION) == (V2, H3)
    assert cohort_key(V2, H3) == V2 and cohort_key(V2, H2) == LEG and cohort_key(V2, H1) == OLD
    assert row_classifier({}, "2026-10-01") == H1
    # an untagged row from 2026-10-02 on ran headlines_v2: now the legacy cohort, never current
    assert row_classifier(None, "2026-10-02") == H2 and cohort_key(V2, row_classifier(None, "2026-10-02")) == LEG
    assert row_classifier({"classifier_version": H1}, "2026-10-05") == H1      # its own tag wins
    assert row_classifier({"classifier_version": H3}, "2026-10-02") == H3
    assert "current" in CLASSIFIER_LABELS[H3] and "current" not in CLASSIFIER_LABELS[H2]
    assert "decided by" in resolver_label(LEG) and CLASSIFIER_LABELS[H2] in resolver_label(LEG)


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
    # 10/02 approvals were decided by headlines_v2 (untagged, dated): legacy now, not pooled either
    _day(st, table, "2026-10-02", [(True, "WIN"), (False, "LOSS")])
    _day(st, table, "2026-10-05", [(True, "LOSS"), (False, "WIN")], day_tag=H3)
    _day(st, table, "2026-10-06", [(True, "LOSS")], day_tag=H3)
    s = arm.summary(st)
    assert s["design_hash"] == arm.DESIGN_HASH and s["days"] == 4
    assert (s["resolver_version"], s["classifier_version"]) == (V2, H3)
    hv = arm.DESIGN["headline_variant"]
    g = s["regimes"]["iex"]
    p = g["variants"][hv]
    assert g["sessions"] == 2 and (p["approved"]["filled"], p["approved"]["wins"]) == (2, 0)   # not 6 / 4
    assert (p["unapproved"]["filled"], p["unapproved"]["wins"]) == (1, 1)
    assert "2 sessions" in s["headline"]
    old = s["other_resolvers"][OLD]
    assert old["headline"] is False and old["days"] == 1 and "never the decision" in old["note"]
    assert "decided by" in old["label"] and H1 in old["label"]
    assert old["regimes"]["iex"]["variants"][hv]["approved"]["wins"] == 3
    leg = s["other_resolvers"][LEG]
    assert leg["headline"] is False and leg["days"] == 1 and "never the decision" in leg["note"]
    assert "decided by" in leg["label"] and CLASSIFIER_LABELS[H2] in leg["label"]
    assert (leg["regimes"]["iex"]["variants"][hv]["approved"]["filled"],
            leg["regimes"]["iex"]["variants"][hv]["approved"]["wins"]) == (1, 1)
    assert set(s["other_resolvers"]) == {OLD, LEG}


def test_control_with_only_older_classifier_days_decides_nothing():
    st = MemoryStore()
    for table in ("edge_control", "edge_control_v2"):
        _day(st, table, "2026-09-30", [(True, "WIN"), (False, "LOSS")])          # headlines_v1
        _day(st, table, "2026-10-02", [(True, "WIN"), (False, "LOSS")])          # untagged: headlines_v2
        _day(st, table, "2026-10-03", [(True, "WIN")], day_tag=H2)               # tagged headlines_v2
    for s in (CA.summary(st), CA2.summary(st)):
        assert s["regimes"] == {} and s["current_regime"] == "iex"
        assert f"nothing graded under {V2} with {H3}" in s["headline"] and "legacy" in s["headline"]
        assert s["other_resolvers"][OLD]["days"] == 1 and s["other_resolvers"][LEG]["days"] == 2
    ex = CA2.v1_exploratory(st)
    assert ex["regimes"] == {} and ex["classifier_version"] == H3
    assert ex["other_resolvers"][OLD]["days"] == 1 and ex["other_resolvers"][LEG]["days"] == 2


def test_a_rows_own_classifier_tag_outranks_its_days():
    st = MemoryStore()
    _day(st, "edge_control", "2026-10-05", [(True, "WIN"), (True, "LOSS"), (True, "WIN")], day_tag=H3,
         row_tags=[H1, None, H2])
    s = CA.summary(st)
    hv = CA.DESIGN["headline_variant"]
    cur = s["regimes"]["iex"]["variants"][hv]["approved"]
    assert (cur["filled"], cur["wins"]) == (1, 0)
    assert s["other_resolvers"][OLD]["regimes"]["iex"]["variants"][hv]["approved"]["wins"] == 1
    assert s["other_resolvers"][LEG]["regimes"]["iex"]["variants"][hv]["approved"]["wins"] == 1
    # and the other way: a headlines_v3 tag on a row outranks the 10/02 day it would be dated by
    st = MemoryStore()
    _day(st, "edge_control", "2026-10-02", [(True, "WIN"), (True, "LOSS")], row_tags=[H3, None])
    s = CA.summary(st)
    assert s["regimes"]["iex"]["variants"][hv]["approved"]["wins"] == 1
    assert s["other_resolvers"][LEG]["regimes"]["iex"]["variants"][hv]["approved"]["filled"] == 1


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
    new, fri, old = date(2026, 10, 5), date(2026, 10, 2), date(2026, 9, 30)
    _seed(radar, new)                     # its forecast was issued (and tagged) by headlines_v3
    _seed(radar, fri, approve=False)      # no forecast to read: dated by the session, headlines_v2
    _seed(radar, old, approve=False)      # no forecast to read: dated by the session, headlines_v1
    for arm, table in ((CA, "edge_control"), (CA2, "edge_control_v2")):
        for d in (new, fri, old):
            assert arm.grade_day(FakeBars(), radar, day=d, now=ts(16, 25, d))["status"] == "graded"
        rec_new, rec_fri, rec_old = (radar.get(table, d.isoformat()) for d in (new, fri, old))
        assert rec_new["classifier_version"] == H3 and {r["classifier_version"] for r in rec_new["rows"]} == {H3}
        assert rec_fri["classifier_version"] == H2 and {r["classifier_version"] for r in rec_fri["rows"]} == {H2}
        assert rec_old["classifier_version"] == H1 and {r["classifier_version"] for r in rec_old["rows"]} == {H1}
        assert rec_new["design_hash"] == arm.DESIGN_HASH                     # the design is untouched
        s = arm.summary(radar)
        assert s["regimes"]["iex"]["sessions"] == 1
        assert s["other_resolvers"][OLD]["days"] == 1 and s["other_resolvers"][LEG]["days"] == 1


def test_the_sessions_classifier_comes_from_its_forecasts():
    st = MemoryStore()
    assert CA.session_classifier(st, "2026-10-02") == H2 and CA.session_classifier(st, "2026-09-30") == H1
    day = date(2026, 9, 30)
    f = forecast(st, I.INTRADAY_CONTINUATION, "AAA", day=day)
    assert CA.session_classifier(st, day.isoformat()) == H3          # its own tag
    row = st.get("forecasts", f.forecast_id)
    row["evidence"] = {k: v for k, v in row["evidence"].items() if k != "classifier_version"}
    st.put("forecasts", f.forecast_id, row)
    assert CA.session_classifier(st, day.isoformat()) == H1          # stored before tags: dated
    forecast(st, I.INTRADAY_CONTINUATION, "BBB", day=day)
    mixed = CA.session_classifier(st, day.isoformat())
    assert mixed == f"{H1}+{H3}"                                      # a mid-session change: mixed
    cohort = CA.row_cohort({"day": day.isoformat(), "classifier_version": mixed}, {"resolver_version": V2})
    assert cohort == f"{V2}~{H1}+{H3}" and "decided by mixed:" in resolver_label(cohort)
    # a 10/02 forecast stored without a tag ran headlines_v2: the session is the legacy cohort
    fri = date(2026, 10, 2)
    g = forecast(st, I.INTRADAY_CONTINUATION, "CCC", day=fri)
    row = st.get("forecasts", g.forecast_id)
    row["evidence"] = {k: v for k, v in row["evidence"].items() if k != "classifier_version"}
    st.put("forecasts", g.forecast_id, row)
    assert CA.session_classifier(st, fri.isoformat()) == H2
    assert CA.row_cohort({"day": fri.isoformat(), "classifier_version": H2}, {"resolver_version": V2}) == LEG


def test_a_regraded_day_keeps_the_classifier_its_rows_were_graded_with():
    row = {"symbol": "OLD", "resolver_version": V2, "variants": {k: {"outcome": "WIN"} for k in CA.VARIANTS}}
    both = {"symbol": "TAG", "resolver_version": V2, "classifier_version": H3, "variants": {}}
    kept = CA.tag_kept_rows({"day": "2026-10-05", "classifier_version": H1}, {"OLD": row, "TAG": both})
    assert kept["OLD"]["classifier_version"] == H1 and kept["OLD"]["variants"] == row["variants"]
    assert kept["TAG"] is both and "classifier_version" not in row       # nothing stored is mutated
    assert CA.tag_kept_rows({"day": "2026-10-05", "classifier_version": H3}, {"OLD": row})["OLD"]["classifier_version"] == H3
    assert CA.tag_kept_rows({"day": "2026-09-30"}, {"OLD": row})["OLD"]["classifier_version"] == H1
    # a day graded before tags on/after 2026-10-02 ran headlines_v2: its kept rows stay in that cohort
    assert CA.tag_kept_rows({"day": "2026-10-02"}, {"OLD": row})["OLD"]["classifier_version"] == H2


# ---- model training ------------------------------------------------------------------------

def test_model_training_reads_only_the_current_classifier():
    current = _dataset(V2)
    assert {r["classifier_version"] for r in current} == {H3}
    older = [{**r, "classifier_version": H1} for r in _dataset(V2, invert=True, seed=11)]
    v2rows = [{**r, "classifier_version": H2} for r in _dataset(V2, invert=True, seed=17)]
    untagged = [{k: v for k, v in r.items() if k != "classifier_version"} for r in _dataset(V2, seed=13)]
    assert all(r["day"] < "2026-10-02" for r in untagged)                 # dated: headlines_v1
    assert MD.training_rows(current + older + v2rows + untagged) == current
    assert MD.fit(current + older) == MD.fit(current) == MD.fit(current + v2rows)
    ev = MD.evaluate(current + older)
    assert ev == {**MD.evaluate(current), "rows_other_resolvers": len(older)}
    assert ev["classifier_version"] == H3 and ev["cohort_rows"] == len(current)
    assert MD.fit(older) is None and MD.evaluate(older)["cohort_rows"] == 0
    assert MD.fit(v2rows) is None and MD.evaluate(v2rows)["cohort_rows"] == 0
    # an untagged row from a session on or after 2026-10-02 ran headlines_v2: legacy, not trained on
    late = {**untagged[0], "day": "2026-10-02"}
    assert MD.training_rows([late]) == []
    assert MD.training_rows([{**late, "classifier_version": H3}]) == [{**late, "classifier_version": H3}]


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
    # 10/02's card ran headlines_v2 (untagged, dated): legacy too
    st.put("edge_card_outcomes", "2026-10-02", {"day": "2026-10-02", "rows": _graded("FRI", 3, 1)})
    st.put("edge_card_outcomes", "2026-10-05", {"day": "2026-10-05", "rows": _graded("NEW", 2, 0, H3)})
    for d, sym, tag in (("2026-09-30", "OLD0", None), ("2026-10-02", "FRI0", None), ("2026-10-05", "NEW0", H3)):
        # a list built today records its card's classifier (top10.build); older lists carry none
        st.put("edge_top10", d, {"day": d, "list": [{"rank": 1, "symbol": sym}],
                                 **({"classifier_version": tag} if tag else {})})
    sc = SC.scorecard(st)
    assert (sc["resolver_version"], sc["classifier_version"]) == (V2, H3)
    assert sc["graded_rows"] == 2 and sc["graded_sessions"] == 1
    assert (sc["keyword_catalyst"]["approved"]["decided"], sc["keyword_catalyst"]["approved"]["wins"]) == (2, 0)
    assert sc["top10"]["sessions"] == 1 and sc["top10"]["approved"]["wins"] == 0
    old = sc["other_resolvers"][OLD]
    assert old["headline"] is False and "decided by" in old["label"] and H1 in old["label"]
    assert (old["base_rate_all_gappers"]["decided"], old["base_rate_all_gappers"]["wins"]) == (4, 4)
    assert old["top10"]["sessions"] == 1
    leg = sc["other_resolvers"][LEG]
    assert leg["headline"] is False and CLASSIFIER_LABELS[H2] in leg["label"]
    assert (leg["base_rate_all_gappers"]["decided"], leg["base_rate_all_gappers"]["wins"]) == (3, 1)
    assert leg["top10"]["sessions"] == 1 and leg["top10"]["approved"]["wins"] == 1
    assert set(sc["other_resolvers"]) == {OLD, LEG}
    # the Top 10 view says which classifier scored each list
    assert T.with_outcomes(st, "2026-09-30")["classifier_version"] == H1
    assert T.with_outcomes(st, "2026-10-02")["classifier_version"] == H2
    assert T.with_outcomes(st, "2026-10-05")["classifier_version"] == H3


def test_a_built_top10_records_its_cards_classifier():
    row = {"symbol": "AA", "ref_price": 11.0, "prev_close": 10.0, "avg_dollars": 3e7, "inputs": {"events": []}}
    assert T.build({"day": "2026-10-05", "rows": [{**row, "classifier_version": H3}]})["classifier_version"] == H3
    assert T.build({"day": "2026-10-02", "rows": [row]})["classifier_version"] == H2        # dated: legacy
    assert T.build({"day": "2026-09-30", "rows": [row]})["classifier_version"] == H1        # dated


def test_card_grades_carry_the_classifier_of_their_card():
    from test_edge_pipeline import DAY, FakeAlpaca, ts as pts
    from edge import pipeline as P
    from edge.ledger import Ledger
    lg = Ledger(MemoryStore())
    P.morning_card(FakeAlpaca(), lg, now=pts(9, 10))
    card = lg.store.get("edge_cards", DAY.isoformat())
    assert {r.get("classifier_version") for r in card["rows"]} == {H3}
    SC.grade_card(FakeAlpaca("evening"), lg.store, day=DAY, now=pts(16, 25))
    rows = lg.store.get("edge_card_outcomes", DAY.isoformat())["rows"]
    assert rows and {r["classifier_version"] for r in rows} == {H3}
    # a card stored before tags existed is dated by its day (2026-09-23: headlines_v1)
    st = MemoryStore()
    st.put("edge_cards", DAY.isoformat(), {**card, "rows": [{k: v for k, v in r.items() if k != "classifier_version"}
                                                             for r in card["rows"]]})
    SC.grade_card(FakeAlpaca("evening"), st, day=DAY, now=pts(16, 25))
    assert {r["classifier_version"] for r in st.get("edge_card_outcomes", DAY.isoformat())["rows"]} == {H1}


def test_an_untagged_session_from_the_v3_start_is_the_current_classifier():
    """A session from 2026-10-05 with no intraday forecast (or an empty card) has nothing to tag; it
    still ran headlines_v3 and must not be filed as the legacy headlines_v2 cohort."""
    from edge import control as CA2, top10 as T2
    from edge.catalysts import CLASSIFIER_SINCE, CLASSIFIER_VERSION, classifier_of
    from edge.ledger import MemoryStore as MS
    assert CLASSIFIER_SINCE == "2026-10-05"
    assert classifier_of(None, "2026-10-05") == CLASSIFIER_VERSION == "headlines_v3"
    assert classifier_of(None, "2026-10-03") == "headlines_v2" and classifier_of(None, "2026-10-01") == "headlines_v1"
    assert CA2.session_classifier(MS(), "2026-10-05") == CLASSIFIER_VERSION
    assert T2.build({"day": "2026-10-05", "rows": []})["classifier_version"] == CLASSIFIER_VERSION
