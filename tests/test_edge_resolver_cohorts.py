"""Audit NEW-02: resolver versions are cohorts, never pooled.

Stored outcomes are never re-graded (docs/resolver_versions.md). A row without
`resolver_version` was graded by resolver_v1, whose fill-bar target touches may be
optimistic and whose missing data finalized as NO_FILL. A resolver_v1 WIN beside a
resolver_v2 LOSS used to report n=2, 50%. Now the headline (and promotion, retirement,
calibration, model training and the scorecard) reads only the current resolver's cohort;
older cohorts stay visible, labelled.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from edge import models as MD, promotion, readout as RO, scorecard as SC, top10 as T
from edge.catalysts import CLASSIFIER_VERSION
from edge.contracts import issue
from edge.ledger import LEGACY_RESOLVER, Ledger, MemoryStore, resolver_cohort, resolver_of
from edge.pipeline import EXPERIMENTS, GAP_AND_GO_AUTO
from edge.resolver import RESOLVER_VERSION, Resolution

ET = ZoneInfo("America/New_York")
EID = GAP_AND_GO_AUTO.experiment_id
V1, V2 = "resolver_v1", "resolver_v2"
# An untagged row from a session before 2026-10-02 was also decided by headlines_v1 (task #65), so it
# sits in the qualified cohort, never in the bare resolver_v1 one. A row of the current cohort carries
# the current classifier's tag (every row headlines_v3 decides is tagged); an untagged row from
# 2026-10-02 on would be the headlines_v2 legacy cohort, not the current one.
V1_OLD = f"{V1}~headlines_v1"


def ts(d: date, hh: int, mm: int) -> int:
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET).timestamp())


def ledger() -> Ledger:
    lg = Ledger(MemoryStore())
    for spec in EXPERIMENTS:
        lg.register(spec, now=ts(date(2026, 9, 21), 8, 0))
    return lg


def forecast(lg: Ledger, sym: str, day: date, *, prob=None):
    f = issue(GAP_AND_GO_AUTO, symbol=sym, session_date=day, entry_ref=10.0,
              issued_at=ts(day, 9, 10), prob=prob)
    lg.record(f, now=ts(day, 9, 11))
    return f


def settle(lg: Ledger, f, outcome: str, version, *, records=("forecast", "simulated"), pnl=None):
    """`version` None = a row stored before resolver versioning existed (resolver_v1)."""
    pnl = pnl if pnl is not None else {"WIN": 49.0, "LOSS": -30.0}.get(outcome)
    for rec in records:
        lg.settle(f.forecast_id, Resolution(outcome, pnl_usd=pnl, resolver_version=version),
                  now=ts(date.fromisoformat(f.session_date), 16, 25), record=rec)


def test_resolver_of_mirrors_feed_of_and_old_rows_are_resolver_v1():
    assert RESOLVER_VERSION == V2 and LEGACY_RESOLVER == V1
    assert resolver_of({"outcome": "WIN"}) == V1             # stored before versioning
    assert resolver_of({"outcome": "WIN", "resolver_version": None}) == V1
    assert resolver_of({"outcome": "WIN", "resolver_version": V2}) == V2
    assert resolver_of(None) == V1
    assert Ledger.resolver_of({"resolver_version": V2}) == V2


def test_a_v1_win_and_a_v2_loss_are_two_cohorts_not_n2_at_50pct():
    """The audit's reproduction: before the fix the headline read 1 win in 2 (50%)."""
    lg, day = ledger(), date(2026, 9, 23)
    old, new = forecast(lg, "OLD", day), forecast(lg, "NEW", day)
    settle(lg, old, "WIN", None)                # resolver_v1, stored before versioning
    settle(lg, new, "LOSS", V2)
    rep = lg.report(EID)
    assert rep["feed_regime"] == "iex" and rep["forecasts_in_regime"] == 2
    assert rep["resolver_version"] == V2 and rep["forecasts_in_cohort"] == 1
    sim = rep["records"]["simulated"]
    assert (sim["filled"], sim["wins"], sim["win_rate"]) == (1, 0, 0.0)          # headline = v2 only
    assert (rep["filled"], rep["wins"]) == (1, 0)                                 # flat legacy view too
    assert rep["records"]["forecast"]["filled"] == 1
    legacy = rep["other_resolvers"][V1]
    assert legacy["headline"] is False and legacy["forecasts"] == 1
    assert legacy["label"].startswith("resolver_v1 (legacy") and "optimistic" in legacy["label"]
    assert "NO_FILL" in legacy["label"]
    assert (legacy["records"]["simulated"]["filled"], legacy["records"]["simulated"]["wins"]) == (1, 1)
    assert "never pooled" in rep["cohort_rule"]


def test_the_broker_record_follows_its_forecasts_cohort():
    """"actual" is the broker's, not a resolver grade (no version on it): it is reported with the
    forecast it belongs to, so a cohort's three records describe the same forecasts."""
    lg, day = ledger(), date(2026, 9, 23)
    old, new = forecast(lg, "OLD", day), forecast(lg, "NEW", day)
    settle(lg, old, "WIN", None)
    settle(lg, old, "WIN", None, records=("actual",))
    settle(lg, new, "LOSS", V2)
    settle(lg, new, "LOSS", None, records=("actual",))
    rep = lg.report(EID)
    assert rep["records"]["actual"]["by_outcome"] == {"LOSS": 1}
    assert rep["other_resolvers"][V1]["records"]["actual"]["by_outcome"] == {"WIN": 1}


def test_pending_and_unresolved_rows_belong_to_the_current_resolver():
    """Only a FINAL row fixes a cohort: anything not final will be graded by the current resolver."""
    lg, day = ledger(), date(2026, 9, 23)
    pending, unresolved = forecast(lg, "PEND", day), forecast(lg, "UNRS", day)
    settle(lg, unresolved, "UNRESOLVED", None)               # a v1 row waiting on data
    assert resolver_cohort(lg.store, pending.to_dict()) == lg.cohort_of(unresolved.to_dict()) == V2
    rep = lg.report(EID)
    assert rep["forecasts_in_cohort"] == 2 and "other_resolvers" not in rep
    assert rep["records"]["simulated"]["by_outcome"] == {"PENDING": 1, "UNRESOLVED": 1}


def test_a_forecast_finalized_by_two_resolvers_is_its_own_cohort_never_the_headline():
    lg, day = ledger(), date(2026, 9, 23)
    f = forecast(lg, "MIX", day)
    settle(lg, f, "WIN", None, records=("forecast",))       # final under v1
    settle(lg, f, "LOSS", V2, records=("simulated",))       # simulated left UNRESOLVED then, v2 now
    rep = lg.report(EID)
    assert rep["forecasts_in_cohort"] == 0 and rep["records"]["simulated"]["filled"] == 0
    mixed = rep["other_resolvers"][f"{V1}+{V2}"]
    assert mixed["label"].startswith("mixed:") and mixed["records"]["simulated"]["filled"] == 1


def test_the_other_feed_regime_is_split_by_resolver_too():
    lg, iex_day, sip_day = ledger(), date(2026, 9, 23), date(2026, 9, 28)
    lg.store.put("edge_cards", sip_day.isoformat(), {"day": sip_day.isoformat(), "live_feed": "sip"})
    settle(lg, forecast(lg, "OLD", iex_day), "WIN", None)
    settle(lg, forecast(lg, "MID", iex_day), "LOSS", V2)
    settle(lg, forecast(lg, "NEW", sip_day), "WIN", V2)
    rep = lg.report(EID)
    assert rep["feed_regime"] == "sip" and rep["records"]["simulated"]["wins"] == 1
    iex = rep["other_regimes"]["iex"]
    assert (iex["simulated"]["filled"], iex["simulated"]["wins"]) == (1, 0)          # v2 in IEX
    assert iex["other_resolvers"][V1]["records"]["simulated"]["wins"] == 1           # v1 in IEX


# ---- promotion, retirement and the day-clustered sample ------------------------------------

def test_promotion_and_its_by_day_sample_ignore_resolver_v1_outcomes():
    lg = ledger()
    days = [date(2026, 9, 21) + timedelta(days=i) for i in range(4)]      # Mon-Thu
    for d in days[:3]:
        settle(lg, forecast(lg, "OLD", d), "WIN", None)                   # optimistic v1 history
    settle(lg, forecast(lg, "NEW", days[3]), "LOSS", V2)
    assert RO._by_day(lg.store, EID, "iex") == {days[3].isoformat(): [False]}
    assert RO._by_day(lg.store, EID, "iex", V1) == {d.isoformat(): [True] for d in days[:3]}
    ex = RO.experiments(lg.store)[EID]
    assert ex["resolver_version"] == V2 and ex["forecasts_in_cohort"] == 1
    assert ex["records"]["simulated"]["filled"] == 1 and ex["records"]["simulated"]["wins"] == 0
    assert ex["other_resolvers"][V1]["records"]["simulated"]["wins"] == 3
    p = ex["promotion"]
    assert "simulated trades 1/100" in p["unmet"] and "sessions 1/40" in p["unmet"]
    assert p["resolver_version"] == V2
    # the frozen criteria are untouched
    assert p["criteria_version"] == "promotion_v1" and p["criteria_hash"] == promotion.CRITERIA_HASH


def test_retirement_never_judges_on_resolver_v1_outcomes():
    """30 resolver_v1 losses would retire the rule if pooled; they are not the current cohort."""
    lg, day = ledger(), date(2026, 9, 23)
    for i in range(30):
        settle(lg, forecast(lg, f"L{i}", day), "LOSS", None)
    r = promotion.retirement(lg.report(EID))
    assert r["retire"] is False and "0/30" in r["why"] and r["resolver_version"] == V2
    assert r["retirement_version"] == "retirement_v1" and r["retirement_hash"] == promotion.RETIREMENT_HASH
    # the same thirty under the current resolver: the unchanged bar still retires it
    lg2 = ledger()
    for i in range(30):
        settle(lg2, forecast(lg2, f"L{i}", day), "LOSS", V2)
    assert promotion.retirement(lg2.report(EID))["retire"] is True


def test_a_report_of_an_older_cohort_is_never_judged():
    lg, day = ledger(), date(2026, 9, 23)
    settle(lg, forecast(lg, "X", day), "LOSS", V2)
    rep = {**lg.report(EID), "resolver_version": V1}
    assert promotion.retirement(rep)["retire"] is False
    out = promotion.evaluate(rep, baseline=lg.report(EID), sessions=50, by_day={}, n_candidates=1)
    assert any("resolver_v1 cohort" in u for u in out["unmet"]) and out["stage"] != "proposable"


# ---- calibration and model training ------------------------------------------------------

def test_the_ledgers_calibration_bands_read_only_the_current_cohort():
    lg, day = ledger(), date(2026, 9, 23)
    settle(lg, forecast(lg, "OLD", day, prob=0.9), "WIN", None)
    settle(lg, forecast(lg, "NEW", day, prob=0.2), "LOSS", V2)
    bands = lg.report(EID)["records"]["simulated"]["calibration"]
    assert sum(b["n"] for b in bands) == 1 and sum(b["hits"] for b in bands) == 0
    legacy = lg.report(EID)["other_resolvers"][V1]["records"]["simulated"]["calibration"]
    assert sum(b["n"] for b in legacy) == 1 and sum(b["hits"] for b in legacy) == 1


def _dataset(version, *, invert=False, days=60, per_day=8, seed=7):
    rng = random.Random(seed)
    rows, d = [], date(2026, 6, 1)
    while len({r["day"] for r in rows}) < days:
        d += timedelta(days=1)
        if d.weekday() >= 5:
            continue
        for i in range(per_day):
            trend = rng.uniform(-3, 3)
            feats = {"gap_855": rng.uniform(5, 30), "log_price": rng.uniform(0.5, 2.5),
                     "log_dollar_volume": rng.uniform(6.7, 9.0), "pre_volume_ratio": rng.uniform(0, 1),
                     "pre_range_pos": rng.uniform(0, 1), "pre_trend": trend,
                     "catalyst_company": float(rng.random() < 0.4), "catalyst_policy": 0.0,
                     "dilutive": 0.0, "any_news": 1.0, "dow": float(d.weekday())}
            win = rng.random() < (0.75 if trend > 1 else 0.2)
            row = {"day": d.isoformat(), "symbol": f"S{i}", "features": feats,
                   "avg_dollars": 10 ** feats["log_dollar_volume"],
                   "market": "WIN" if win != invert else "LOSS"}
            if version:      # a row made by a backtest run today: both tags
                row.update(resolver_version=version, classifier_version=CLASSIFIER_VERSION)
            rows.append(row)
    return rows


def test_model_training_and_calibration_exclude_resolver_v1_rows():
    current = _dataset(V2)
    legacy = _dataset(None, invert=True, seed=11)          # v1 rows saying the opposite
    assert MD.training_rows(current + legacy) == current
    assert MD.fit(current + legacy) == MD.fit(current)    # same logistic AND isotonic steps
    ev = MD.evaluate(current + legacy)
    assert ev == {**MD.evaluate(current), "rows_other_resolvers": len(legacy)}
    assert ev["resolver_version"] == V2 and ev["cohort_rows"] == len(current)
    only_v1 = MD.evaluate(legacy)
    assert only_v1["qualified"] is False and only_v1["cohort_rows"] == 0
    assert MD.fit(legacy) is None


# ---- the card scorecard and the Top 10 ---------------------------------------------------

def _card_rows(prefix, n, wins, version, **stamp):
    rows = []
    for i in range(n):
        r = {"symbol": f"{prefix}{i}", "outcome": "WIN" if i < wins else "LOSS",
             "execution": "WIN" if i < wins else "LOSS", "baseline": "ELIGIBLE", **stamp}
        if version:      # graded by a versioned resolver today: decided by the current classifier too
            r.update(resolver_version=version, classifier_version=CLASSIFIER_VERSION)
        rows.append(r)
    return rows


def test_the_scorecard_separates_resolver_cohorts_and_legacy_rows_have_no_version():
    """Card rows graded before resolver_v2 carry no resolver_version: they are resolver_v1."""
    st = MemoryStore()
    st.put("edge_card_outcomes", "2026-09-29", {"day": "2026-09-29", "rows":
           _card_rows("OLD", 4, 4, None, auto="ELIGIBLE", research="ELIGIBLE", model_prob=0.6)})
    st.put("edge_card_outcomes", "2026-10-05", {"day": "2026-10-05", "rows":
           _card_rows("NEW", 2, 0, V2, auto="ELIGIBLE", research="ELIGIBLE", model_prob=0.6)})
    sc = SC.scorecard(st)
    assert sc["resolver_version"] == V2 and sc["sessions"] == 2
    assert sc["graded_rows"] == 2 and sc["graded_sessions"] == 1
    base = sc["base_rate_all_gappers"]
    assert (base["decided"], base["wins"]) == (2, 0)                     # not 4/6
    assert sc["keyword_catalyst"]["approved"]["decided"] == 2
    assert sc["ai_research"]["approved"]["decided"] == 2
    assert sc["model"]["approved"]["decided"] == 2
    legacy = sc["other_resolvers"][V1_OLD]
    assert legacy["headline"] is False and legacy["label"].startswith("resolver_v1 (legacy")
    assert (legacy["base_rate_all_gappers"]["decided"], legacy["base_rate_all_gappers"]["wins"]) == (4, 4)
    assert legacy["graded_sessions"] == 1


def test_the_top10_comparison_and_view_separate_resolver_cohorts():
    st = MemoryStore()
    for d, version, (pick_win, other_win) in (("2026-09-29", None, ("WIN", "WIN")),
                                               ("2026-10-05", V2, ("LOSS", "LOSS"))):
        st.put("edge_top10", d, {"day": d, "list": [{"rank": 1, "symbol": "PICK"}]})
        rows = [{"symbol": "PICK", "outcome": pick_win, "execution": pick_win, "baseline": "ELIGIBLE"},
                {"symbol": "REST", "outcome": other_win, "execution": other_win, "baseline": "ELIGIBLE"}]
        for r in rows:
            if version:
                r.update(resolver_version=version, classifier_version=CLASSIFIER_VERSION)
        st.put("edge_card_outcomes", d, {"day": d, "rows": rows})
    sc = SC.scorecard(st)
    assert sc["top10"]["sessions"] == 1
    assert (sc["top10"]["approved"]["decided"], sc["top10"]["approved"]["wins"]) == (1, 0)
    assert (sc["top10"]["rejected"]["decided"], sc["top10"]["rejected"]["wins"]) == (1, 0)
    old = sc["other_resolvers"][V1_OLD]["top10"]
    assert old["sessions"] == 1 and old["approved"]["wins"] == 1 and old["rejected"]["wins"] == 1
    assert T.with_outcomes(st, "2026-09-29")["list"][0]["resolver_version"] == V1
    assert T.with_outcomes(st, "2026-10-05")["list"][0]["resolver_version"] == V2
    st.put("edge_top10", "2026-10-06", {"day": "2026-10-06", "list": [{"rank": 1, "symbol": "PICK"}]})
    assert T.with_outcomes(st, "2026-10-06")["list"][0]["resolver_version"] is None      # not graded yet


def test_the_stored_history_is_never_rewritten_by_the_cohort_split():
    lg, day = ledger(), date(2026, 9, 23)
    old = forecast(lg, "OLD", day)
    settle(lg, old, "WIN", None)
    before = lg.store.get("outcomes", f"{old.forecast_id}|simulated")
    lg.report(EID)
    RO.experiments(lg.store)
    assert lg.store.get("outcomes", f"{old.forecast_id}|simulated") == before
    assert "resolver_version" not in before or before["resolver_version"] is None


# ---- the control arms (control_arm_v1 and v2 summaries) ----------------------------------

def _control_day(store, table, day, rows, version):
    """A graded control day; version None = a day graded before resolver_version was recorded (and
    before classifier tags). A versioned day records the current classifier, as grading does today."""
    from edge import control as CA, control_v2 as CA2
    built = []
    for i, (approved, outcome) in enumerate(rows):
        v = {"outcome": outcome, "pnl_usd": 48.0 if outcome == "WIN" else -32.0}
        if table == "edge_control":
            built.append({"symbol": f"S{i}", "feed": "iex", "approved": approved,
                          "label": CA.APPROVED if approved else CA.UNAPPROVED,
                          "variants": {k: dict(v) for k in CA.VARIANTS}})
        else:
            arm = {"variants": {k: dict(v) for k in CA2.VARIANTS}}
            arms = ({CA2.APPROVED: arm, CA2.UNAPPROVED: {"variants": {k: {"outcome": CA2.APPROVED_BY_ENTRY}
                                                                     for k in CA2.VARIANTS}}}
                    if approved else {CA2.UNAPPROVED: arm})
            built.append({"symbol": f"S{i}", "feed": "iex", "arms": arms})
    rec = {"day": day, "feed": "iex", "complete": True, "rows": built}
    if version:
        rec.update(resolver_version=version, classifier_version=CLASSIFIER_VERSION)
    store.put(table, day, rec)


def test_control_v1_summary_decides_on_current_resolver_days_only():
    from edge import control as CA
    st = MemoryStore()
    _control_day(st, "edge_control", "2026-09-29", [(True, "WIN")] * 3 + [(False, "LOSS")] * 3, None)
    _control_day(st, "edge_control", "2026-10-05", [(True, "LOSS"), (False, "WIN")], V2)
    s = CA.summary(st)
    assert s["design_hash"] == CA.DESIGN_HASH == "605453cd49128631"          # design untouched
    assert s["resolver_version"] == V2 and s["current_regime"] == "iex" and s["days"] == 2
    g = s["regimes"]["iex"]
    assert g["sessions"] == 1 and g["graded_names"] == 2
    p = g["variants"][CA.DESIGN["headline_variant"]]
    assert (p["approved"]["filled"], p["approved"]["wins"]) == (1, 0)        # not 4 fills, 3 wins
    assert (p["unapproved"]["filled"], p["unapproved"]["wins"]) == (1, 1)
    assert "1 sessions" in s["headline"]
    old = s["other_resolvers"][V1_OLD]
    assert old["headline"] is False and old["days"] == 1 and old["label"].startswith("resolver_v1 (legacy")
    assert "never the decision" in old["note"]
    assert old["regimes"]["iex"]["variants"][CA.DESIGN["headline_variant"]]["approved"]["wins"] == 3


def test_control_headline_with_only_legacy_days_decides_nothing():
    from edge import control as CA, control_v2 as CA2
    st = MemoryStore()
    _control_day(st, "edge_control", "2026-09-29", [(True, "WIN"), (False, "LOSS")], None)
    _control_day(st, "edge_control_v2", "2026-09-29", [(True, "WIN"), (False, "LOSS")], None)
    for s in (CA.summary(st), CA2.summary(st)):
        assert s["regimes"] == {} and s["current_regime"] == "iex"
        assert f"nothing graded under {V2}" in s["headline"] and "legacy" in s["headline"]
        assert s["other_resolvers"][V1_OLD]["days"] == 1


def test_control_v2_summary_decides_on_current_resolver_days_only():
    from edge import control_v2 as CA2
    st = MemoryStore()
    _control_day(st, "edge_control_v2", "2026-09-29", [(True, "WIN")] * 3 + [(False, "LOSS")] * 3, None)
    _control_day(st, "edge_control_v2", "2026-10-05", [(True, "LOSS"), (False, "WIN")], V2)
    _control_day(st, "edge_control", "2026-09-29", [(True, "WIN")], None)
    s = CA2.summary(st)
    assert s["design_hash"] == CA2.DESIGN_HASH == "3baa238954712ba2"
    g = s["regimes"]["iex"]
    p = g["variants"][CA2.DESIGN["headline_variant"]]
    assert g["sessions"] == 1 and (p["approved"]["filled"], p["approved"]["wins"]) == (1, 0)
    assert p["sessions_with_fills"] == 1                         # the clustered sample is one cohort too
    assert s["other_resolvers"][V1_OLD]["regimes"]["iex"]["sessions"] == 1
    ex = CA2.v1_exploratory(st)
    assert ex["regimes"] == {} and ex["other_resolvers"][V1_OLD]["days"] == 1


def test_a_rows_own_resolver_tag_outranks_its_days():
    from edge import control as CA
    st = MemoryStore()
    _control_day(st, "edge_control", "2026-10-05", [(True, "WIN"), (True, "LOSS")], V2)
    rec = st.get("edge_control", "2026-10-05")
    rec["rows"][0]["resolver_version"] = V1          # graded before the day was re-graded under v2
    st.put("edge_control", "2026-10-05", rec)
    s = CA.summary(st)
    hv = CA.DESIGN["headline_variant"]
    assert s["regimes"]["iex"]["variants"][hv]["approved"]["filled"] == 1
    assert s["other_resolvers"][V1]["regimes"]["iex"]["variants"][hv]["approved"]["wins"] == 1


def test_a_regraded_day_keeps_its_older_rows_in_their_own_cohort():
    """A day left partial by truncated data is re-graded later; its kept rows are not re-graded and
    are labelled with the version their day recorded (none = resolver_v1), outcomes untouched."""
    from edge import control as CA
    row = {"symbol": "OLD", "variants": {k: {"outcome": "WIN"} for k in CA.VARIANTS}}
    tagged = {"symbol": "TAG", "resolver_version": V2, "classifier_version": CLASSIFIER_VERSION, "variants": {}}
    kept = CA.tag_kept_rows({"day": "2026-09-29"}, {"OLD": row, "TAG": tagged})
    assert kept["OLD"]["resolver_version"] == V1 and kept["OLD"]["variants"] == row["variants"]
    assert kept["TAG"] is tagged and "resolver_version" not in row          # the stored dict is not mutated
    assert CA.tag_kept_rows({"resolver_version": V2}, {"OLD": row})["OLD"]["resolver_version"] == V2
    assert CA.tag_kept_rows(None, {"OLD": row})["OLD"]["resolver_version"] == V1
