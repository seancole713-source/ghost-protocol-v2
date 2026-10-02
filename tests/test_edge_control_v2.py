"""Control arm v2 (control_arm_v2): approval counted only when known by the entry (EDGE-10)."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from edge import control as CA, control_v2 as V2  # noqa: E402
from edge import intraday as I  # noqa: E402
from edge import pipeline as P  # noqa: E402
from edge import readout as RO  # noqa: E402
from edge import stats  # noqa: E402
from edge.contracts import FrozenSpecError, issue  # noqa: E402
from edge.ledger import Ledger, MemoryStore  # noqa: E402
from test_edge_control import DAY, DS, FakeBars, forecast, radar_row, ts  # noqa: E402


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("EDGE_LIVE_FEED", "iex")
    st = MemoryStore()
    for s in ("WINR", "LOSR", "FLAT", "NODATA"):
        radar_row(st, s)                                      # all first seen at 10:00
    forecast(st, I.INTRADAY_CONTINUATION, "WINR", at=ts(13, 0))      # approved three hours later
    forecast(st, I.INTRADAY_CONTINUATION_V2, "WINR", at=ts(13, 0))
    forecast(st, I.CATALYST_BREAKOUT, "LOSR", at=ts(10, 3))          # between the two entries
    # A premarket (Gap-and-Go) forecast is not an intraday approval.
    g = issue(P.SPEC, symbol="FLAT", session_date=DAY, entry_ref=10.0, issued_at=ts(9, 10))
    st.put("forecasts", g.forecast_id, g.to_dict())
    return st


def rows_of(st, day=DS):
    return {r["symbol"]: r for r in st.get("edge_control_v2", day)["rows"]}


def outcomes(arm):
    return {v: arm["variants"][v]["outcome"] for v in V2.VARIANTS}


# ------------------------------------------------------------------ point-in-time labels
def test_a_name_approved_hours_later_is_not_graded_as_approved_from_first_seen(store):
    """The EDGE-10 case: seen 10:00, approved 13:00. v1 grades it APPROVED from 10:01; v2 grades
    it UNAPPROVED from 10:01 (that was the state then) and APPROVED only from 13:01."""
    CA.grade_day(FakeBars(), store, day=DAY, now=ts(16, 25))
    v1 = {r["symbol"]: r for r in store.get("edge_control", DS)["rows"]}["WINR"]
    assert v1["approved"] is True and v1["refs"]["auto"]["at"] == ts(10, 1)       # v1, unchanged
    assert V2.grade_day(FakeBars(), store, day=DAY, now=ts(16, 25))["status"] == "graded"
    w = rows_of(store)["WINR"]
    assert w["approved_at"] == ts(13, 0) and w["first_seen"] == ts(10, 0)
    u, a = w["arms"][V2.UNAPPROVED], w["arms"][V2.APPROVED]
    assert u["decision_at"] == ts(10, 0) and u["refs"]["auto"]["at"] == ts(10, 1)
    assert outcomes(u) == dict.fromkeys(V2.VARIANTS, "WIN")          # the 10:01 run, as UNAPPROVED
    assert a["decision_at"] == ts(13, 0)
    assert a["refs"]["auto"]["at"] == ts(13, 1) and a["refs"]["human"]["at"] == ts(13, 5)
    assert a["refs"]["auto"]["entry_expiry"] == ts(13, 21)
    assert outcomes(a) == dict.fromkeys(V2.VARIANTS, "NO_FILL")      # by 13:00 the run was over
    assert a["approved_by"] == {"auto": ["intraday_continuation@v1", "intraday_continuation@v2"],
                                "human": ["intraday_continuation@v1", "intraday_continuation@v2"]}


def test_approval_counts_only_when_issued_at_or_before_the_entry(store):
    V2.grade_day(FakeBars(), store, day=DAY, now=ts(16, 25))
    lo = rows_of(store)["LOSR"]
    u = lo["arms"][V2.UNAPPROVED]
    # Approved at 10:03: after the 10:01 entry (still an unapproved comparison), before the 10:05 one.
    assert u["variants"]["auto_10bps"]["outcome"] == "LOSS"
    assert u["variants"]["human_10bps"]["outcome"] == V2.APPROVED_BY_ENTRY
    assert u["variants"]["human_25bps"]["outcome"] == V2.APPROVED_BY_ENTRY
    a = lo["arms"][V2.APPROVED]
    assert a["decision_at"] == ts(10, 3) and a["refs"]["auto"]["at"] == ts(10, 4)
    assert a["approved_by"]["auto"] == ["catalyst_breakout@v1"]
    # Exactly at the entry still counts as approved by then (issued_at <= entry time).
    row = V2.grade_row("X", DAY, ts(10, 0), [(ts(10, 1), "e@v1")], [])
    assert row["arms"][V2.UNAPPROVED]["variants"]["auto_10bps"]["outcome"] == V2.APPROVED_BY_ENTRY
    # Unapproved everywhere: no approved arm at all; a premarket card forecast is not an approval.
    rows = rows_of(store)
    assert V2.APPROVED not in rows["FLAT"]["arms"] and rows["FLAT"]["approved_at"] is None
    assert rows["NODATA"]["arms"][V2.UNAPPROVED]["variants"]["auto_10bps"]["outcome"] == V2.NO_REFERENCE


def test_comparisons_outside_the_approval_window_are_not_counted():
    row = V2.grade_row("X", DAY, ts(9, 40), [(ts(14, 40), "e@v1")], [])
    assert set(outcomes(row["arms"][V2.UNAPPROVED]).values()) == {V2.OUTSIDE_WINDOW}
    assert set(outcomes(row["arms"][V2.APPROVED]).values()) == {V2.OUTSIDE_WINDOW}


def test_unapproved_comparisons_never_become_picks(store):
    before = len(store.scan("forecasts"))
    V2.grade_day(FakeBars(), store, day=DAY, now=ts(16, 25))
    assert len(store.scan("forecasts")) == before
    assert store.scan("edge_paper") == [] and store.scan("experiments") == []
    day = RO.view(store, "control", DS)["day"]
    flat = {r["symbol"]: r for r in day["rows"]}["FLAT"]
    assert flat["unapproved"]["label"] == "UNAPPROVED (control, not a pick)" and flat["approved"] is None


def test_graded_once_truncated_symbols_retried(store):
    get = FakeBars(truncate={"LOSR"})
    out = V2.grade_day(get, store, day=DAY, now=ts(16, 25))
    assert out["status"] == "partial" and out["truncated"] == ["LOSR"]
    assert rows_of(store)["LOSR"]["data"] == V2.DATA_TRUNCATED and rows_of(store)["LOSR"]["arms"] is None
    again = FakeBars()
    assert V2.grade_day(again, store, day=DAY, now=ts(16, 30))["status"] == "graded"
    assert again.minute_symbols() == ["LOSR"]
    assert V2.grade_day(again, store, day=DAY, now=ts(16, 35))["status"] == "already_graded"
    assert V2.grade_day(FakeBars(), MemoryStore(), day=DAY, now=ts(16, 0))["status"] == "too_early"


def test_the_pipeline_runs_v1_and_v2_side_by_side(store):
    lg = Ledger(store)
    out = P.run(FakeBars(), lg, now=ts(16, 25))
    assert out["control"]["status"] == "graded" and out["control_v2"]["status"] == "graded"
    again = P.run(FakeBars(), lg, now=ts(16, 30))
    assert again["control"]["status"] == again["control_v2"]["status"] == "already_graded"
    # v2 grades on the same feed as v1's rows that day.
    assert store.get("edge_control_v2", DS)["feed"] == store.get("edge_control", DS)["feed"] == "iex"


# ------------------------------------------------------------------ the frozen design
def test_v2_is_its_own_frozen_version_and_v1_is_untouched(monkeypatch):
    assert CA.DESIGN_HASH == "605453cd49128631" and CA.DESIGN["version"] == "control_arm_v1"
    assert V2.DESIGN_HASH == "3baa238954712ba2" and V2.DESIGN["version"] == "control_arm_v2"
    import hashlib
    import json
    assert V2.DESIGN_HASH == hashlib.sha256(json.dumps(V2.DESIGN, sort_keys=True).encode()).hexdigest()[:16]
    st = MemoryStore()
    CA.register(st, now=1)
    assert V2.register(st, now=2)["design_hash"] == V2.DESIGN_HASH
    assert {r["version"] for r in st.scan("edge_control_design")} == {"control_arm_v1", "control_arm_v2"}
    monkeypatch.setattr(V2, "DESIGN_HASH", "0" * 16)
    with pytest.raises(FrozenSpecError):
        V2.register(st, now=3)


def test_v2_success_criteria_are_v1s():
    for k in ("break_even", "min_approved_fills", "alpha", "tests", "levels", "levels_spec_hash",
              "entry_delays_s", "cost_bps_per_side", "variants", "kill", "headline_variant", "win", "feed"):
        assert V2.DESIGN[k] == CA.DESIGN[k], k
    assert V2.DESIGN["min_approved_fills"] == 150 and V2.DESIGN["break_even"] == 0.375
    assert "37.5%" in V2.DESIGN["success"] and "session-clustered" in V2.DESIGN["success"]


# ------------------------------------------------------------------ session-clustered intervals
def synth(st, day, approved, n_win, n_loss, feed="iex"):
    rec = st.get("edge_control_v2", day) or {"day": day, "feed": feed, "complete": True, "rows": [],
                                             "resolver_version": V2.V1.RESOLVER_VERSION}
    for i in range(n_win + n_loss):
        v = {"outcome": "WIN" if i < n_win else "LOSS", "pnl_usd": 48.0 if i < n_win else -32.0}
        arm = {"variants": {k: dict(v) for k in V2.VARIANTS}}
        if approved:   # its unapproved moment came after the approval: excluded, never counted
            arms = {V2.APPROVED: arm, V2.UNAPPROVED: {"variants": {k: {"outcome": V2.APPROVED_BY_ENTRY}
                                                                  for k in V2.VARIANTS}}}
        else:
            arms = {V2.UNAPPROVED: arm}
        rec["rows"].append({"symbol": f"S{len(rec['rows'])}", "feed": feed, "arms": arms})
    st.put("edge_control_v2", day, rec)


def days(n):
    from datetime import date, timedelta
    return [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(n)]


def test_success_when_the_edge_holds_session_after_session():
    st = MemoryStore()
    for d in days(40):
        synth(st, d, True, 3, 1)          # 75% approved, every day
        synth(st, d, False, 3, 7)         # 30% unapproved, every day
    s = V2.summary(st)["regimes"]["iex"]
    v = s["variants"]["human_25bps"]
    assert v["approved"]["filled"] == 160 and v["unapproved"]["filled"] == 400
    assert v["approved"]["rows"] == 160 and v["unapproved"]["rows"] == 400     # excluded moments not counted
    assert v["decision"] == "SUCCESS" and s["decision"] == "SUCCESS" and v["sessions_with_fills"] == 40


def test_a_few_lucky_sessions_do_not_pass_on_clustered_intervals():
    """The same pooled 60% vs 30% that passes when rows are treated as independent, but all the
    approved wins came on 5 sessions of 40: clustering widens the interval, and it decides."""
    st = MemoryStore()
    for i, d in enumerate(days(40)):
        if i < 5:
            synth(st, d, True, 19, 0)      # 95 wins on five days
        else:
            synth(st, d, True, 0, 2)       # 70 losses on the other 35 (pooled 95/165 = 57.6%)
        synth(st, d, False, 3, 7)          # 30% unapproved, every day
    v = V2.summary(st)["regimes"]["iex"]["variants"]["auto_10bps"]
    a = v["approved"]
    nc_lo = stats.wilson(a["wins"], a["filled"], z=V2._z())[0]
    assert nc_lo > V2.V1.break_even_after_costs(10)                # independent rows alone: clears 40%
    assert v["approved_ci_95_clustered"][0] < a["wilson_95"][0]    # clustered: honestly wider
    assert v["approved_low_decision"] < nc_lo and v["decision"] != "SUCCESS"


def test_clustered_diff_ci_resamples_whole_sessions():
    same = {d: (6, 10, 3, 10) for d in days(30)}
    r = stats.clustered_diff_ci(same, alpha=0.05)
    assert r["difference"][0] == pytest.approx(0.3) and r["difference"][1] == pytest.approx(0.3)
    assert r["a"] == (pytest.approx(0.6), pytest.approx(0.6))
    mixed = {d: ((10, 10, 3, 10) if i % 2 else (2, 10, 3, 10)) for i, d in enumerate(days(30))}
    lo, hi = stats.clustered_diff_ci(mixed, alpha=0.05)["difference"]
    assert lo < 0.3 < hi and hi - lo > 0.2
    assert stats.clustered_diff_ci({"d": (1, 1, 0, 0)}) is None          # one arm never filled
    multi = stats.clustered_diff_cis(mixed, (0.05, 0.0125))
    assert multi[0.0125]["difference"][0] <= multi[0.05]["difference"][0]
    assert stats.diff_ci(56, 70, 48, 80)[1] == pytest.approx(0.0524, abs=1e-4)   # kept as it was


def test_small_samples_read_too_few():
    st = MemoryStore()
    synth(st, "2026-09-23", True, 3, 0)
    synth(st, "2026-09-23", False, 5, 20)
    s = V2.summary(st)
    v = s["regimes"]["iex"]["variants"]["human_25bps"]
    assert v["decision"] == "TOO_FEW" and "3/150" in v["why"]
    assert v["approved"]["verdict"].startswith("too few trades (n=3)")
    assert "TOO_FEW" in s["headline"] and s["headline"].startswith("control arm v2 (point-in-time; IEX")


def test_the_kill_rule():
    st = MemoryStore()
    for d in days(40):
        synth(st, d, True, 1, 3)           # 25% approved
        synth(st, d, False, 3, 7)          # 30% unapproved
    s = V2.summary(st)["regimes"]["iex"]
    assert s["decision"] == "KILL" and all(v["decision"] == "KILL" for v in s["variants"].values())


# ------------------------------------------------------------------ readout
def test_the_readout_reports_v2_as_primary_and_v1_as_exploratory(store):
    empty = RO.view(MemoryStore(), "control")
    assert empty["day"] is None and empty["regimes"] == {} and empty["exploratory_v1"]["regimes"] == {}
    CA.grade_day(FakeBars(), store, day=DAY, now=ts(16, 25))
    V2.grade_day(FakeBars(), store, day=DAY, now=ts(16, 25))
    v = RO.view(store, "control")
    assert v["design_version"] == "control_arm_v2" and v["design_hash"] == V2.DESIGN_HASH and v["primary"] is True
    assert v["day"]["day"] == DS and v["day"]["approved"] == 2               # WINR and LOSR
    assert v["headline"].startswith("control arm v2 (point-in-time; IEX, 1 sessions, human_25bps)")
    x = v["exploratory_v1"]
    assert x["label"] == "exploratory (full-session approval, not point-in-time)"
    assert x["design_hash"] == "605453cd49128631"
    assert x["headline"] == f"{x['label']}: {CA.summary(store)['headline']}"     # v1's numbers, unchanged
    v1 = CA.summary(store)["regimes"]["iex"]["variants"]["auto_10bps"]
    assert x["regimes"]["iex"]["variants"]["auto_10bps"]["approved_filled"] == v1["approved"]["filled"]
    summ = RO.view(store, "summary")
    assert summ["control_arm"] == v["headline"] and summ["control_arm_v1_exploratory"] == x["headline"]
    evening = {"day": DS, "card_graded": {"status": "graded"}}
    logged = {n: j for n, _, j in RO.views_to_log(store, evening, now=ts(16, 25))}
    assert "control_arm_v2" in logged["control"] and "exploratory_v1" in logged["control"]
