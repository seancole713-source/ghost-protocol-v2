"""The observe-all control arm (control_arm_v1): every radar name graded, approved or not."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime

import pytest

from edge import control as CA
from edge import intraday as I
from edge import pipeline as P
from edge import radar as R
from edge import readout as RO
from edge import stats
from edge.contracts import ET, FrozenSpecError, issue_intraday
from edge.ledger import Ledger, MemoryStore

DAY = date(2026, 9, 23)
DS = DAY.isoformat()


def ts(hh, mm, d=DAY):
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET).timestamp())


def iso(t):
    return datetime.fromtimestamp(t, tz=ET).isoformat()


# Closing price by minute since 09:30. First seen at 10:00 (minute 30): the "auto" reference
# is the 10:00 bar's close (it ended 10:01:00), the "human" one the 10:04 bar's (ended 10:05:00).
def winr(m):          # drifts up, then runs: both entries fill and reach +5%
    if m <= 30:
        return 10.0
    if m <= 35:
        return 10.0 + 0.01 * (m - 30)
    return min(12.0, 10.05 + 0.06 * (m - 35))


def losr(m):          # pops through the auto trigger, then collapses below the human trigger
    if m <= 30:
        return 10.0
    if m <= 34:
        return 10.0 + 0.03 * (m - 30)
    return max(9.0, 10.12 - 0.1 * (m - 34))


def flat(m):
    return 10.0


PATHS = {"WINR": winr, "LOSR": losr, "FLAT": flat}


def bars_for(sym, day=DAY):
    fn = PATHS.get(sym)
    if fn is None:
        return []                 # no prints at all: no reference price
    out, prev = [], fn(0)
    for m in range(390):
        c = round(fn(m), 4)
        out.append({"t": iso(ts(9, 30, day) + 60 * m), "o": prev, "h": round(max(prev, c) + 0.01, 4),
                    "l": round(min(prev, c) - 0.01, 4), "c": c, "v": 1000})
        prev = c
    return out


class Resp:
    def __init__(self, payload):
        self._p, self.status_code = payload, 200

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


class FakeBars:
    """Minute bars only. `truncate` symbols make any request containing them page forever."""

    def __init__(self, truncate=()):
        self.calls, self.truncate = [], set(truncate)

    def __call__(self, url, params=None, headers=None, timeout=None):
        params = dict(params or {})
        self.calls.append((url, params))
        if url.endswith("/v2/stocks/bars") and params.get("timeframe") == "1Min":
            syms = params["symbols"].split(",")
            day = datetime.fromisoformat(params["start"]).date()
            page = {"bars": {s: bars_for(s, day) for s in syms}}
            if self.truncate & set(syms):
                page["next_page_token"] = "more"
            return Resp(page)
        if url.endswith("/v2/stocks/bars"):
            return Resp({"bars": {}})
        if "/v2/stocks/snapshots" in url or "screener" in url or "news" in url:
            return Resp({})
        raise AssertionError(f"unrouted {url}")

    def minute_symbols(self):
        return [p["symbols"] for u, p in self.calls if p.get("timeframe") == "1Min"]


def radar_row(store, sym, detected=None, day=DAY, state=R.EXPIRED):
    item = R.RadarItem(sym, day.isoformat(), detected or ts(10, 0, day), 8.0, state=state)
    store.put("edge_radar", f"{day.isoformat()}|{sym}", asdict(item))


def forecast(store, spec, sym, day=DAY, at=None):
    f = issue_intraday(spec, symbol=sym, session_date=day, entry_ref=10.0, issued_at=at or ts(11, 0, day))
    store.put("forecasts", f.forecast_id, f.to_dict())
    return f


@pytest.fixture
def seeded(monkeypatch):
    monkeypatch.setenv("EDGE_LIVE_FEED", "iex")
    store = MemoryStore()
    for s in ("WINR", "LOSR", "FLAT", "NODATA"):
        radar_row(store, s)
    forecast(store, I.INTRADAY_CONTINUATION, "WINR")
    forecast(store, I.INTRADAY_CONTINUATION_V2, "WINR")
    # A premarket (Gap-and-Go) forecast is not an INTRADAY approval.
    from edge.contracts import issue
    g = issue(P.SPEC, symbol="FLAT", session_date=DAY, entry_ref=10.0, issued_at=ts(9, 10))
    store.put("forecasts", g.forecast_id, g.to_dict())
    return store


def rows_of(store, day=DS):
    return {r["symbol"]: r for r in store.get("edge_control", day)["rows"]}


# ------------------------------------------------------------------ grading
def test_every_radar_name_is_graded_at_both_entry_delays(seeded):
    out = CA.grade_day(FakeBars(), seeded, day=DAY, now=ts(16, 25))
    assert out["status"] == "graded" and out["rows"] == 4 and out["feed"] == "iex"
    rows = rows_of(seeded)
    w = rows["WINR"]
    assert w["refs"]["auto"]["at"] == ts(10, 1) and w["refs"]["human"]["at"] == ts(10, 5)
    assert w["refs"]["auto"]["price"] == 10.0 and w["refs"]["human"]["price"] == 10.04
    assert (w["refs"]["auto"]["trigger"], w["refs"]["auto"]["target"], w["refs"]["auto"]["stop"]) == (10.02, 10.52, 9.72)
    assert w["refs"]["auto"]["shares"] == int(1000 // 10.02)
    assert w["refs"]["auto"]["entry_expiry"] == ts(10, 21)            # 20 minutes from the reference time
    assert {v: w["variants"][v]["outcome"] for v in CA.VARIANTS} == dict.fromkeys(CA.VARIANTS, "WIN")
    assert w["variants"]["auto_25bps"]["pnl_usd"] < w["variants"]["auto_10bps"]["pnl_usd"]
    lo = rows["LOSR"]
    assert lo["variants"]["auto_10bps"]["outcome"] == "LOSS"          # the fast entry got caught
    assert lo["variants"]["human_10bps"]["outcome"] == "NO_FILL"      # the slow one never triggered
    assert rows["FLAT"]["variants"]["auto_25bps"]["outcome"] == "NO_FILL"
    assert rows["NODATA"]["variants"]["auto_10bps"]["outcome"] == CA.NO_REFERENCE


def test_approved_and_unapproved_are_labelled_and_unapproved_never_become_picks(seeded):
    before = len(seeded.scan("forecasts"))
    CA.grade_day(FakeBars(), seeded, day=DAY, now=ts(16, 25))
    rows = rows_of(seeded)
    assert rows["WINR"]["approved"] is True and rows["WINR"]["label"] == CA.APPROVED
    assert rows["WINR"]["approved_by"] == ["intraday_continuation@v1", "intraday_continuation@v2"]
    # FLAT only had a premarket card forecast: not an intraday approval.
    assert [rows[s]["approved"] for s in ("LOSR", "FLAT", "NODATA")] == [False, False, False]
    assert all(rows[s]["label"] == CA.UNAPPROVED for s in ("LOSR", "FLAT", "NODATA"))
    assert all(r["feed"] == "iex" and r["first_seen"] == ts(10, 0) for r in rows.values())
    # A control row is never a forecast, a paper order or an experiment.
    assert len(seeded.scan("forecasts")) == before
    assert seeded.scan("edge_paper") == [] and seeded.scan("experiments") == []
    day = RO.view(seeded, "control", DS)["day"]
    assert {r["symbol"]: r["label"] for r in day["rows"]}["LOSR"] == "UNAPPROVED (control, not a pick)"


def test_the_day_is_graded_once_and_a_rerun_fetches_nothing(seeded):
    get = FakeBars()
    CA.grade_day(get, seeded, day=DAY, now=ts(16, 25))
    n = len(get.calls)
    assert CA.grade_day(get, seeded, day=DAY, now=ts(16, 30)) == {"status": "already_graded", "rows": 4}
    assert len(get.calls) == n
    assert CA.grade_day(get, MemoryStore(), day=DAY, now=ts(16, 0))["status"] == "too_early"


def test_bars_come_in_batches_of_ten_and_a_truncated_symbol_is_never_kept(seeded):
    for i in range(12):
        radar_row(seeded, f"X{i:02d}")
    get = FakeBars(truncate={"LOSR"})
    out = CA.grade_day(get, seeded, day=DAY, now=ts(16, 25))
    assert out["status"] == "partial" and out["truncated"] == ["LOSR"]
    assert all(len(s.split(",")) <= CA.CHUNK for s in get.minute_symbols())
    rows = rows_of(seeded)
    assert rows["LOSR"]["data"] == CA.DATA_TRUNCATED and rows["LOSR"]["variants"] is None
    assert seeded.get("edge_control", DS)["complete"] is False
    # The next tick re-asks only what was missing, and completes the day.
    again = FakeBars()
    assert CA.grade_day(again, seeded, day=DAY, now=ts(16, 30))["status"] == "graded"
    assert again.minute_symbols() == ["LOSR"]
    assert rows_of(seeded)["LOSR"]["variants"]["auto_10bps"]["outcome"] == "LOSS"
    assert CA.grade_day(again, seeded, day=DAY, now=ts(16, 35))["status"] == "already_graded"


def test_feed_regimes_are_recorded_per_row_and_never_pooled(seeded, monkeypatch):
    get = FakeBars()
    CA.grade_day(get, seeded, day=DAY, now=ts(16, 25))
    day2 = date(2026, 9, 24)
    for s in ("WINR", "LOSR"):
        radar_row(seeded, s, day=day2)
    forecast(seeded, I.CATALYST_BREAKOUT, "LOSR", day=day2)
    monkeypatch.setenv("EDGE_LIVE_FEED", "sip")
    get2 = FakeBars()
    CA.grade_day(get2, seeded, day=day2, now=ts(16, 25, day2))
    assert {p["feed"] for _, p in get.calls} == {"iex"} and {p["feed"] for _, p in get2.calls} == {"sip"}
    assert {r["feed"] for r in rows_of(seeded, day2.isoformat()).values()} == {"sip"}
    s = CA.summary(seeded)
    assert set(s["regimes"]) == {"iex", "sip"} and s["current_regime"] == "sip"
    iex, sip = s["regimes"]["iex"], s["regimes"]["sip"]
    assert (iex["sessions"], iex["graded_names"], iex["approved_names"]) == (1, 4, 1)
    assert (sip["sessions"], sip["graded_names"], sip["approved_names"]) == (1, 2, 1)
    # SIP: approved LOSR (auto: LOSS) vs unapproved WINR (auto: WIN) -- nothing from IEX leaks in.
    a = sip["variants"]["auto_10bps"]
    assert (a["approved"]["filled"], a["approved"]["wins"], a["unapproved"]["wins"]) == (1, 0, 1)


def test_the_pipeline_runs_it_once_in_the_grading_window(seeded):
    lg = Ledger(seeded)
    out = P.run(FakeBars(), lg, now=ts(16, 25))
    assert out["control"]["status"] == "graded"
    assert P.run(FakeBars(), lg, now=ts(16, 30))["control"]["status"] == "already_graded"
    assert "control" not in P.run(FakeBars(), Ledger(MemoryStore()), now=ts(15, 0))


def test_a_day_without_radar_names_is_stored_as_empty():
    store = MemoryStore()
    assert CA.grade_day(FakeBars(), store, day=DAY, now=ts(16, 25))["status"] == "no_radar"
    assert CA.grade_day(FakeBars(), store, day=DAY, now=ts(16, 30))["status"] == "already_graded"
    assert CA.summary(store)["headline"].startswith("control arm: nothing graded yet")


# ------------------------------------------------------------------ summary math
def synth(store, day, feed, approved, n_win, n_loss, extra=()):
    rows = store.get("edge_control", day) or {"day": day, "feed": feed, "complete": True, "rows": []}
    for i in range(n_win + n_loss):
        win = i < n_win
        v = {"outcome": "WIN" if win else "LOSS", "pnl_usd": 48.0 if win else -32.0}
        rows["rows"].append({"symbol": f"{'A' if approved else 'U'}{len(rows['rows'])}", "feed": feed,
                             "approved": approved, "label": CA.APPROVED if approved else CA.UNAPPROVED,
                             "variants": {k: dict(v) for k in CA.VARIANTS}})
    for o in extra:
        rows["rows"].append({"symbol": "Z", "feed": feed, "approved": approved, "label": "",
                             "variants": {k: {"outcome": o} for k in CA.VARIANTS}})
    store.put("edge_control", day, rows)


def test_newcombe_difference_interval_matches_the_published_example():
    d, lo, hi = stats.diff_ci(56, 70, 48, 80)          # Newcombe (1998), method 10
    assert round(d, 4) == 0.2 and round(lo, 4) == 0.0524 and round(hi, 4) == 0.3339
    assert stats.diff_ci(1, 0, 1, 5) is None


def test_small_samples_read_too_few():
    store = MemoryStore()
    synth(store, "2026-09-23", "iex", True, 3, 0, extra=("NO_FILL", "NO_REFERENCE"))
    synth(store, "2026-09-23", "iex", False, 5, 20)
    v = CA.summary(store)["regimes"]["iex"]["variants"]["human_25bps"]
    assert v["approved"]["filled"] == 3 and v["approved"]["rows"] == 5 and v["approved"]["no_fill"] == 1
    assert v["approved"]["verdict"].startswith("too few trades (n=3)")
    assert v["decision"] == "TOO_FEW" and "3/150" in v["why"]
    assert v["difference"] == round(1.0 - 0.2, 4) and v["difference_ci_95"][0] > 0   # shown, not judged
    assert CA.summary(store)["regimes"]["iex"]["decision"] == "TOO_FEW"
    assert "TOO_FEW" in CA.summary(store)["headline"] and "37.5%" in CA.summary(store)["headline"]


def test_success_needs_a_positive_difference_and_approved_above_break_even_after_costs():
    store = MemoryStore()
    synth(store, "2026-09-23", "iex", True, 96, 64)            # 60% of 160
    synth(store, "2026-09-23", "iex", False, 120, 280)         # 30% of 400
    s = CA.summary(store)["regimes"]["iex"]
    v = s["variants"]["auto_25bps"]
    assert v["decision"] == "SUCCESS" and s["decision"] == "SUCCESS"
    assert v["approved"]["win_rate"] == 0.6 and v["unapproved"]["win_rate"] == 0.3
    assert round(v["difference"], 4) == 0.3 and v["difference_ci_decision"][0] > 0
    # The decision interval is Bonferroni-wider than the displayed 95% one.
    assert v["difference_ci_decision"][0] < v["difference_ci_95"][0]
    assert v["break_even_after_costs"] == round(CA.break_even_after_costs(25), 4) > 0.4


def test_the_kill_rule_and_selection_without_edge():
    store = MemoryStore()
    synth(store, "2026-09-23", "iex", True, 45, 105)           # 30% of 150
    synth(store, "2026-09-23", "iex", False, 120, 280)         # 30% of 400
    s = CA.summary(store)["regimes"]["iex"]
    assert s["decision"] == "KILL" and all(v["decision"] == "KILL" for v in s["variants"].values())
    store = MemoryStore()
    synth(store, "2026-09-23", "iex", True, 64, 96)            # 40%: beats 15%, not break-even
    synth(store, "2026-09-23", "iex", False, 60, 340)
    v = CA.summary(store)["regimes"]["iex"]["variants"]["human_10bps"]
    assert v["decision"] == "SELECTION_ONLY"


def test_break_even_after_costs():
    assert round(CA.break_even_after_costs(0), 4) == 0.375
    assert round(CA.break_even_after_costs(10), 3) == 0.400
    assert round(CA.break_even_after_costs(25), 3) == 0.438


# ------------------------------------------------------------------ the frozen design
def test_the_design_hash_is_stable_and_a_changed_design_is_refused(monkeypatch):
    assert CA.DESIGN_HASH == "605453cd49128631"
    assert len(CA.DESIGN_HASH) == 16 and CA.DESIGN["version"] == "control_arm_v1"
    store = MemoryStore()
    assert CA.register(store, now=1)["design_hash"] == CA.DESIGN_HASH
    assert CA.register(store, now=2)["registered_at"] == 1            # stored once
    monkeypatch.setattr(CA, "DESIGN_HASH", "0" * 16)
    with pytest.raises(FrozenSpecError):
        CA.register(store, now=3)


def test_the_control_levels_are_the_intraday_specs_levels():
    fields = ("trigger_mult", "limit_mult", "target_mult", "stop_mult", "time_exit_et", "size_usd")
    for spec in I.INTRADAY_SPECS:
        assert all(getattr(spec, f) == getattr(CA.CONTROL_SPEC, f) for f in fields), spec.experiment_id
        assert spec.eligibility["entry_window_min"] == CA.DESIGN["levels"]["entry_window_min"]
        assert {f: getattr(spec, f) for f in fields if f != "time_exit_et"} == {
            f: CA.DESIGN["levels"][f] for f in fields if f != "time_exit_et"}
    assert CA.DESIGN["levels_spec_hash"] == CA.CONTROL_SPEC.spec_hash()
    assert "150" in CA.DESIGN["success"] and "37.5%" in CA.DESIGN["success"] and "150" in CA.DESIGN["kill"]


# ------------------------------------------------------------------ readout
def test_the_readout_view_and_the_evening_log(seeded):
    assert "control" in RO.VIEWS
    empty = RO.view(MemoryStore(), "control")
    assert empty["day"] is None and empty["regimes"] == {}
    CA.grade_day(FakeBars(), seeded, day=DAY, now=ts(16, 25))
    v = RO.view(seeded, "control")
    assert v["design_hash"] == CA.DESIGN_HASH and v["day"]["day"] == DS and v["day"]["approved"] == 1
    assert v["headline"].startswith("control arm (IEX, 1 sessions, human_25bps)")
    assert RO.view(seeded, "summary")["control_arm"] == v["headline"]
    evening = {"day": DS, "card_graded": {"status": "graded"}}
    assert "control" in [n for n, _, _ in RO.views_to_log(seeded, evening, now=ts(16, 25))]


def test_the_mcp_enum_lists_the_control_view():
    from mcp import ghost_server
    tools = {t["name"]: t for t in ghost_server.list_tools()}
    enum = tools["ghost_edge_report"]["inputSchema"]["properties"]["view"]["enum"]
    assert set(enum) == set(RO.VIEWS)
