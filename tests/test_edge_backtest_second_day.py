"""second_day_open@v1 (preregistered 2026-10-03): a history replay, tested with a fake `get`."""
from __future__ import annotations

import ast
import pathlib
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from edge import backtest_second_day as SD
from edge import readout as R
from edge.ledger import MemoryStore

ET = ZoneInfo("America/New_York")
D = date(2026, 9, 18)          # Friday: the decision day
D1, D2 = date(2026, 9, 17), date(2026, 9, 16)
ROOT = pathlib.Path(__file__).resolve().parents[1]


def ts(d, hh, mm):
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET).timestamp())


def iso(t):
    return datetime.fromtimestamp(t, tz=ET).isoformat()


class Resp:
    def __init__(self, p=None, status=200, text=""):
        self._p, self.status_code, self.text = p, status, text
        self.headers = {}

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            err = RuntimeError(f"HTTP {self.status_code}")
            err.response = self
            raise err


def day_bar(t, c2, c1, *, h=None, l=None, v=2_000_000, vw="c"):
    """(D-2 row, D-1 row) for ticker t."""
    h = c1 if h is None else h
    l = min(c2, c1) if l is None else l
    row1 = {"T": t, "o": c2, "h": h, "l": l, "c": c1, "v": v}
    if vw is not None:
        row1["vw"] = c1 if vw == "c" else vw
    return {"T": t, "o": c2, "h": c2, "l": c2, "c": c2, "v": v, "vw": c2}, row1


def minute_rows(day, path, flat=None, *, start=(9, 30)):
    """path: [(o, h, l, c)] from 09:30 on; then flat bars at the last close until 15:59."""
    rows, t = [], ts(day, *start)
    last = path[-1][3] if path else flat
    for o, h, l, c in path:
        rows.append({"t": iso(t), "o": o, "h": h, "l": l, "c": c, "v": 10_000})
        t += 60
    while t < ts(day, 16, 0):
        rows.append({"t": iso(t), "o": last, "h": last, "l": last, "c": last, "v": 1_000})
        t += 60
    return rows


# Every ticker opens D at a level relative to close(D-1) = 12.80 (limit 14.72).
UNIVERSE = {
    # name: (c2, c1, kwargs for D-1)
    "RUN": (10.0, 12.8, dict(h=13.0, l=10.5)),               # +28%, upper half, $25.6M  -> main
    "CTL": (10.0, 11.5, dict(h=11.6, l=10.2)),               # +15% -> control
    "WRNT": (10.0, 12.8, dict(h=13.0, l=10.5)),              # not CS (a warrant)
    "LOWH": (10.0, 12.6, dict(h=15.5, l=10.0)),              # +26%, (12.6-10)/(15.5-10) = 0.47 < 0.50
    "THIN": (10.0, 12.8, dict(h=13.0, l=10.5, v=300_000)),   # $3.8M < $10M
    "PRICY": (90.0, 117.0, dict(h=118.0, l=95.0)),           # close > $100
    "PENNY": (1.5, 1.95, dict(h=1.96, l=1.6, v=20_000_000)), # close < $2
    "RSPL": (10.0, 12.8, dict(h=13.0, l=10.5)),              # reverse split on 2026-09-14 (inside D-5..D)
    "DIL": (10.0, 12.8, dict(h=13.0, l=10.5)),               # 8-K Item 3.02 accepted D-1 18:00 ET
    "DILX": (10.0, 12.8, dict(h=13.0, l=10.5)),              # 8-K exhibit "Form of Securities Purchase Agreement"
    "OLD8K": (10.0, 12.8, dict(h=13.0, l=10.5, v=1_500_000)),  # 3.02 accepted before D-1: still eligible
    "NOVW": (10.0, 12.8, dict(h=13.0, l=10.5, vw=None)),     # no VWAP: dollar volume unknown
    "FLAT": (10.0, 10.5, dict(h=10.6, l=10.0)),              # +5%: in neither arm
}
CIK = {t: str(i + 1).zfill(10) for i, t in enumerate(sorted(UNIVERSE))}


def eight_k(acc, accepted, items):
    return {"form": "8-K", "acc": acc, "accepted": accepted, "items": items}


FILINGS = {
    "DIL": [eight_k("0000000009-26-000001", "2026-09-17T18:00:00.000Z", "3.02,9.01")],
    "DILX": [eight_k("0000000010-26-000001", "2026-09-18T07:30:00.000Z", "1.01,9.01")],
    "OLD8K": [eight_k("0000000011-26-000001", "2026-09-15T16:00:00.000Z", "3.02")],
}
INDEX = """<table><tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
<tr><td>1</td><td>8-K</td><td><a href="/Archives/edgar/data/10/x/d8k.htm">d8k.htm</a></td><td>8-K</td><td>1</td></tr>
<tr><td>2</td><td></td><td><a href="/Archives/edgar/data/10/x/ex10.htm">ex10.htm</a></td><td>EX-10.1</td><td>1</td></tr>
</table>"""
EX10 = "<html><body><p>Exhibit 10.1</p><p>FORM OF SECURITIES PURCHASE AGREEMENT</p><p>This agreement</p></body></html>"


def default_minutes(day):
    rise = [(13.0, 13.1, 12.95, 13.05), (13.05, 13.2, 13.0, 13.15), (13.15, 13.7, 13.1, 13.6)]   # -> +5% target
    sink = [(11.6, 11.65, 11.5, 11.5), (11.5, 11.5, 11.2, 11.25)]                                # -> -3% stop
    return {"RUN": minute_rows(day, rise), "CTL": minute_rows(day, sink), "OLD8K": minute_rows(day, rise),
            "DILX": minute_rows(day, rise), "DIL": minute_rows(day, rise)}


class Market:
    """The fake world. Grouped daily is served as traded (adjusted=false is asserted)."""

    def __init__(self, *, universe=None, splits=None, minutes=None, sec_status=200, denied_before=None,
                 cs=None, daily_override=None):
        self.universe = UNIVERSE if universe is None else universe
        self.splits = splits if splits is not None else [
            {"ticker": "RSPL", "execution_date": "2026-09-14", "split_from": 10, "split_to": 1}]
        self.minutes = minutes or default_minutes
        self.sec_status, self.denied_before = sec_status, denied_before
        self.cs = set(self.universe) - {"WRNT"} if cs is None else cs
        self.daily_override = daily_override or {}
        self.log = []

    def daily(self, d):
        if d in self.daily_override:
            return self.daily_override[d]
        out = []
        for t, (c2, c1, kw) in self.universe.items():
            r2, r1 = day_bar(t, c2, c1, **kw)
            # D-1 of the decision day shows the move; every other session sits at close(D-2).
            out.append(r1 if d == D1 else r2)
        return out

    def __call__(self, url, params=None, headers=None, timeout=None):
        params = params or {}
        if "/v3/reference/splits" in url:
            self.log.append(("splits", None))
            return Resp({"results": self.splits})
        if "/v3/reference/tickers" in url:
            self.log.append(("tickers", params.get("date")))
            assert params.get("type") == "CS" and params.get("date")
            return Resp({"results": [{"ticker": t, "type": "CS", "cik": CIK[t]} for t in sorted(self.cs)]})
        if "/v2/aggs/grouped/" in url:
            assert params.get("adjusted") == "false", params
            d = date.fromisoformat(url.rstrip("/").split("/")[-1])
            self.log.append(("grouped", d))
            if self.denied_before and d < self.denied_before:
                return Resp({"status": "NOT_AUTHORIZED"}, status=403)
            return Resp({"results": self.daily(d)})
        if url.endswith("/v2/stocks/bars"):
            assert params.get("adjustment") == "raw", params
            d = datetime.fromisoformat(params["start"]).date()
            self.log.append(("minute", d))
            m = self.minutes(d)
            return Resp({"bars": {s: m.get(s, []) for s in params["symbols"].split(",") if s in m}})
        if "sec.gov" in url:
            self.log.append(("sec", url))
            if self.sec_status != 200:
                return Resp(None, status=self.sec_status)
            if "/submissions/CIK" in url:
                cik = url.split("CIK")[1].split(".")[0]
                t = next(k for k, v in CIK.items() if v == cik)
                fl = FILINGS.get(t, [])
                return Resp({"filings": {"recent": {
                    "form": [f["form"] for f in fl], "accessionNumber": [f["acc"] for f in fl],
                    "filingDate": [f["accepted"][:10] for f in fl],
                    "acceptanceDateTime": [f["accepted"] for f in fl], "items": [f["items"] for f in fl]},
                    "files": []}})
            if url.endswith("-index.htm"):
                return Resp(text=INDEX)
            if url.endswith("ex10.htm"):
                return Resp(text=EX10)
            return Resp(None, status=404)
        raise AssertionError(url)


def run(get=None, *, start=D, end=D, store=None, **kw):
    store = store or MemoryStore()
    get = get or Market()
    out = SD.run(get, store, start_day=start, end_day=end, pace_s=0, sec_pace_s=0, sleep=lambda s: None, **kw)
    return store, get, out


def day_rec(store, d=D):
    return store.get(SD.SESSIONS_TABLE, f"{SD.VERSION}|{d.isoformat()}")


def trades(store, d=D):
    return {t["symbol"]: t for t in day_rec(store, d)["trades"]}


# --------------------------------------------------------------------------------- eligibility --
def test_each_eligibility_filter_excludes_its_name():
    store, _get, out = run()
    assert out["status"] == "complete"
    rec = day_rec(store)
    by = trades(store)
    assert set(by) == {"RUN", "OLD8K", "CTL"}
    assert by["RUN"]["arm"] == SD.MAIN and by["OLD8K"]["arm"] == SD.MAIN and by["CTL"]["arm"] == SD.CONTROL
    ex = rec["counts"][SD.MAIN]["excluded"]
    assert ex == {"not_common_stock": 1, "lower_half_of_range": 1, "dollar_volume": 2, "price": 2,
                  "reverse_split": 1, "dilution_8k": 2}
    assert "FLAT" not in by                      # +5%: neither arm
    assert by["OLD8K"]["dilution"]["status"] == "clear"     # its 3.02 was accepted before D-1


def test_dilution_filter_reads_items_and_exhibit_titles():
    sec = SD.SecFilings(Market(), sleep=lambda s: None, pace_s=0)
    dil = SD.dilution_8k_filter(sec, cik=int(CIK["DIL"]), d1=D1, day=D)
    assert dil["status"] == "hit" and "3.02" in dil["why"]
    dilx = SD.dilution_8k_filter(sec, cik=int(CIK["DILX"]), d1=D1, day=D)
    assert dilx["status"] == "hit" and "securities purchase agreement" in dilx["why"]
    assert SD.dilution_8k_filter(sec, cik=int(CIK["RUN"]), d1=D1, day=D)["status"] == "clear"
    for title in ("Underwriting Agreement", "Placement Agency Agreement", "At-The-Market Sales Agreement",
                  "Company Announces $5 Million Registered Direct Offering"):
        assert SD.dilutive_title(title)
    assert SD.dilutive_title("Employment Agreement") is None


@pytest.mark.parametrize("accepted, status", [
    ("2026-09-18T14:00:00.000Z", "clear"),     # after 09:00 ET on D under either reading
    ("2026-09-16T22:00:00.000Z", "clear"),     # before D-1 00:00 ET under either reading
    ("2026-09-17T00:30:00.000Z", "hit"),       # D-1 00:30 ET
    ("2026-09-18T08:59:00.000Z", "hit"),       # D 08:59 ET
    ("2026-09-18T09:45:00.000Z", "hit"),       # 09:45 ET is late, but read as UTC it is 05:45 ET: counted
])
def test_the_8k_window_is_d1_midnight_to_0900_on_d(accepted, status):
    late = {"DIL": [eight_k("0000000009-26-000002", accepted, "3.02")]}
    old = dict(FILINGS)
    FILINGS.clear()
    FILINGS.update(late)
    try:
        sec = SD.SecFilings(Market(), sleep=lambda s: None, pace_s=0)
        assert SD.dilution_8k_filter(sec, cik=int(CIK["DIL"]), d1=D1, day=D)["status"] == status
    finally:
        FILINGS.clear()
        FILINGS.update(old)


def test_unavailable_sec_is_recorded_and_fails_the_gate_never_silently_dropped():
    store, get, out = run(Market(sec_status=403))
    assert out["status"] == "complete"
    full = store.get(SD.TABLE, SD.VERSION)
    assert full["dilution_filter"] == "unavailable"
    by = trades(store)
    assert set(by) == {"DIL", "DILX", "RUN", "OLD8K", "CTL"}          # kept, flagged -- not silently passed
    assert all(t["dilution"]["status"] == "unchecked" for t in by.values())
    g = full["windows"]["full"]["gate"]
    assert g["verdict"] == "FAIL" and any("dilution_filter: unavailable" in r for r in g["reasons"])
    sec_calls = [x for x in get.log if x[0] == "sec"]
    assert len(sec_calls) <= 6            # three failures before any success stop the calls


def test_top_five_by_dollar_volume():
    uni = {f"R{c}": (10.0, 12.8, dict(h=13.0, l=10.5, v=1_000_000 * (i + 1))) for i, c in enumerate("ABCDEFG")}
    global CIK
    saved = CIK
    CIK = {t: str(i + 100).zfill(10) for i, t in enumerate(sorted(uni))}
    try:
        store, _get, _out = run(Market(universe=uni, minutes=lambda d: {}))
    finally:
        CIK = saved
    by = trades(store)
    assert set(by) == {"RG", "RF", "RE", "RD", "RC"}
    assert [by[s]["rank"] for s in ("RG", "RF", "RE", "RD", "RC")] == [1, 2, 3, 4, 5]
    assert all(t["base"]["simulated"] == SD.NO_DATA for t in by.values())


# ------------------------------------------------------------------------------ point in time --
def test_selection_uses_only_data_from_before_d():
    store, get, _ = run(start=D1, end=D)
    calls = [x for x in get.log if x[0] in ("grouped", "minute")]
    assert ("grouped", D) not in calls                  # D's own daily bar is never read
    # For D-1's own decision, its daily bar is fetched only AFTER its minute bars (execution).
    if ("minute", D1) in calls:
        assert calls.index(("minute", D1)) < calls.index(("grouped", D1))
    assert calls.index(("grouped", D1)) < calls.index(("minute", D))
    # D's selection is unchanged whatever D's own daily bar would have said.
    store2, _, _ = run(Market(daily_override={D: []}), start=D1, end=D)
    assert set(trades(store)) == set(trades(store2))


def test_close_d2_restated_only_for_a_split_executed_on_d1():
    # FWD did a 2-for-1 on D-1: D-2 traded at $20.00 (raw), $10.00 on D-1's basis -> +28%, not -36%.
    uni = {"FWD": (20.0, 12.8, dict(h=13.0, l=10.5))}
    global CIK
    saved = CIK
    CIK = {"FWD": "0000000500"}
    try:
        store, _, _ = run(Market(universe=uni, minutes=lambda d: {},
                                 splits=[{"ticker": "FWD", "execution_date": "2026-09-17",
                                          "split_from": 1, "split_to": 2}]))
    finally:
        CIK = saved
    t = trades(store)["FWD"]
    assert t["prior_day_pct"] == 28.0 and t["split_restated"]["d2_share_factor"] == 2.0
    assert t["limit"] == pytest.approx(12.8 * 1.15)


# ------------------------------------------------------------------------------- resolution --
def test_fill_target_stop_time_exit_and_no_fill():
    lim = round(12.8 * 1.15, 6)                                                    # 14.72
    path = {
        "RUN": [(13.0, 13.1, 12.95, 13.05), (13.05, 13.7, 13.0, 13.6)],            # +5% -> WIN
        "OLD8K": [(15.0, 15.2, 14.9, 15.1)],                                       # opens above 14.72 -> NO_FILL
        "CTL": [(11.6, 11.65, 11.5, 11.5), (11.5, 11.5, 11.2, 11.25)],             # -3% -> LOSS
    }
    store, _, _ = run(Market(minutes=lambda d: {s: minute_rows(d, p) for s, p in path.items()}))
    by = trades(store)
    assert by["RUN"]["base"]["simulated"] == "WIN" and by["RUN"]["base"]["entry"] == 13.0
    assert by["RUN"]["shares"] == int(1000 // lim) == 67
    assert by["OLD8K"]["base"]["simulated"] == "NO_FILL" and by["OLD8K"]["base"]["pnl_usd"] is None
    assert by["CTL"]["base"]["simulated"] == "LOSS"
    # costs: the same outcome, a smaller P&L at 25 bps
    assert by["RUN"]["cost_25bps"]["simulated"] == "WIN"
    assert by["RUN"]["cost_25bps"]["pnl_usd"] < by["RUN"]["base"]["pnl_usd"]


def test_time_exit_one_bar_both_levels_and_the_entry_delay():
    from edge.backtest import _bars
    b = _bars(minute_rows(D, [(13.0, 13.05, 12.95, 13.0)]))
    kw = dict(symbol="X", arm=SD.MAIN, ref=12.8, limit=14.72, shares=67, complete=True)
    x = SD.simulate(b, D, **kw)
    assert x["simulated"] == "TIME_EXIT" and x["exit"] == 13.0
    both = _bars(minute_rows(D, [(13.0, 13.05, 12.95, 13.0), (13.0, 13.8, 12.5, 13.0)]))
    assert SD.simulate(both, D, **kw)["simulated"] == "LOSS"                       # same bar: stop
    # One-minute delay: the 09:31 open is the fill, the same limit rule.
    gap = _bars(minute_rows(D, [(13.0, 13.05, 12.95, 13.0), (14.9, 15.0, 14.8, 14.9)]))
    assert SD.simulate(gap, D, **kw)["simulated"] == "WIN"                         # filled 09:30, ran
    assert SD.simulate(gap, D, delay_min=1, **kw)["simulated"] == "NO_FILL"
    ok = _bars(minute_rows(D, [(13.0, 13.05, 12.95, 13.0), (13.2, 13.3, 13.1, 13.2)]))
    d = SD.simulate(ok, D, delay_min=1, **kw)
    assert d["entry"] == 13.2
    assert SD.simulate([], D, **kw)["simulated"] == SD.NO_DATA


# ---------------------------------------------------------------------- arms, report and gate --
def test_control_arm_is_reported_separately_with_all_fields():
    store, _, out = run()
    full = store.get(SD.TABLE, SD.VERSION)
    for w in ("preregistered", "full"):
        assert set(full["windows"][w]["arms"]) == {SD.MAIN, SD.CONTROL}
    arms = full["windows"]["full"]["arms"]
    main, ctl = arms[SD.MAIN], arms[SD.CONTROL]
    for k in ("fills", "wins", "losses", "time_exits", "no_fills", "win_rate", "wilson_95",
              "expectancy_usd_10bps", "expectancy_usd_25bps", "total_usd_10bps", "total_usd_25bps",
              "delay_1min_10bps"):
        assert k in main and k in ctl
    assert main["wins"] == 2 and main["fills"] == 2 and ctl["losses"] == 1 and ctl["wins"] == 0
    assert full["break_even"] == pytest.approx(0.375)
    assert full["gap_baseline"]["status"] == "skipped"
    assert full["hypothesis"][SD.MAIN]["experiment_id"] == "second_day_open@v1"
    assert out["windows"]["full"]["gate"]["verdict"] in ("PASS", "FAIL")


def arm(fills, wins, e10, e25):
    from edge import stats
    lo, hi = stats.wilson(wins, fills)
    return {"fills": fills, "wins": wins, "win_rate": wins / fills if fills else None,
            "wilson_95": [lo, hi] if fills else None, "expectancy_usd_10bps": e10, "expectancy_usd_25bps": e25}


def test_gate_verdict():
    good, ctl = arm(100, 50, 12.0, 8.0), arm(100, 40, 3.0, -1.0)
    assert SD.gate(good, ctl, "applied")["verdict"] == "PASS"
    weak = arm(10, 4, 12.0, 8.0)                                     # Wilson low ~17%
    g = SD.gate(weak, ctl, "applied")
    assert g["verdict"] == "FAIL" and any("lower bound" in r for r in g["reasons"])
    assert SD.gate(arm(100, 50, 2.0, -0.5), arm(100, 40, 1.0, -2.0), "applied")["verdict"] == "FAIL"
    loses = SD.gate(good, arm(100, 55, 13.0, 9.0), "applied")
    assert loses["verdict"] == "FAIL" and len([r for r in loses["reasons"] if "control" in r]) == 3
    assert SD.gate(good, arm(0, 0, None, None), "applied")["verdict"] == "FAIL"
    assert SD.gate(good, ctl, "partial")["verdict"] == "FAIL"
    assert SD.gate(arm(0, 0, None, None), ctl, "applied")["reasons"][0] == "no filled trades"


# --------------------------------------------------------------- windows, resume, availability --
def test_resumes_across_ticks_and_never_refetches_a_stored_day():
    store, get = MemoryStore(), Market()
    clock = iter(range(0, 10_000, 100))
    kw = dict(start_day=date(2026, 9, 14), end_day=D, pace_s=0, sec_pace_s=0, sleep=lambda s: None)
    first = SD.run(get, store, budget_s=150, clock=lambda: next(clock), **kw)
    assert first["status"] == "in_progress" and first["done_through"]
    second = SD.run(get, store, **kw)
    assert second["status"] == "complete"
    grouped = [d for k, d in get.log if k == "grouped"]
    assert len(grouped) == len(set(grouped))                          # one fetch per session, ever
    assert SD.run(get, store, **kw)["status"] == "already_run"
    old = store.get(SD.DAILY_TABLE, f"{SD.VERSION}|2026-09-10")
    assert old and old.get("compacted") and "closes" not in old       # compacted once no longer needed


def test_transient_failure_ends_the_tick_and_the_next_one_retries():
    class Flaky(Market):
        fail = True

        def __call__(self, url, params=None, headers=None, timeout=None):
            if "/v2/aggs/grouped/" in url and Flaky.fail:
                Flaky.fail = False
                return Resp(None, status=502)
            return super().__call__(url, params, headers, timeout)

    store, get = MemoryStore(), Flaky()
    kw = dict(start_day=D, end_day=D, pace_s=0, sec_pace_s=0, sleep=lambda s: None)
    assert SD.run(get, store, **kw)["status"] == "in_progress"
    assert SD.run(get, store, **kw)["status"] == "complete"


def test_data_availability_boundary_is_reported_not_a_failure():
    store, _, out = run(Market(denied_before=D2), start=date(2026, 9, 14), end=D)
    assert out["status"] == "complete"
    av = store.get(SD.TABLE, SD.VERSION)["data_availability"]
    assert av["polygon_grouped_first_session"] == D2.isoformat()
    assert av["polygon_grouped_unavailable"]["count"] > 0
    assert av["first_session_with_both"] == D.isoformat()
    assert day_rec(store, date(2026, 9, 14))["status"] == "no_daily"


def test_results_split_into_preregistered_and_full_windows():
    store, _, _ = run(start=D, end=D)
    full = store.get(SD.TABLE, SD.VERSION)
    assert full["windows"]["preregistered"]["window"] == ["2026-07-09", "2026-10-02"]
    assert full["windows"]["full"]["window"] == [D.isoformat(), D.isoformat()]
    assert full["windows"]["preregistered"]["arms"][SD.MAIN]["fills"] == \
        full["windows"]["full"]["arms"][SD.MAIN]["fills"]


def test_start_comes_from_the_env(monkeypatch):
    monkeypatch.delenv(SD.START_ENV, raising=False)
    assert SD.start_day_from_env() == date(2024, 10, 7)
    monkeypatch.setenv(SD.START_ENV, "2025-03-03")
    assert SD.start_day_from_env() == date(2025, 3, 3)


def test_no_split_list_no_run():
    class NoSplits(Market):
        def __call__(self, url, params=None, headers=None, timeout=None):
            if "/v3/reference/splits" in url:
                raise RuntimeError("HTTP 500")
            return super().__call__(url, params, headers, timeout)

    store, _, out = run(NoSplits())
    assert out["status"] == "error" and store.get(SD.TABLE, SD.VERSION) is None


# ------------------------------------------------------------------------------- wiring --
def test_report_reaches_the_backtest_view():
    store, _, _ = run()
    store.put("edge_backtest", "v", {"version": "v", "completed_at": 1})
    view = R.view(store, "backtest")
    assert view["second_day_open"]["version"] == SD.VERSION and "trades" not in view["second_day_open"]


def test_the_job_chain_runs_it_after_the_post_split_backtest():
    src = (ROOT / "wolf_app.py").read_text()
    at = src.index("def _edge_backtest_job")
    job = src[at:src.index('"edge_backtest",\n', at)]
    assert job.index("_ps.run(") < job.index("_sd.run(")
    assert "store.get(_sd.TABLE, _sd.VERSION)" in job


PINNED_FROZEN_HASHES = {
    "catalyst_breakout@v1": "e2f4657bada0a994d115a4f355c89ac096466ec50b9371633400ccf289dcc372",
    "crowded_short_ignition@v1": "42240af1bb9b8f4a510b84f5d911b800d423eda0daea25789a9e0065b61c1299",
    "gap_and_go@v1": "6b99e5306855af2f6dbd40bd8df069bbf7d2131f4c05d8435c7eb2b080b11cc0",
    "gap_and_go_auto@v1": "b2e4106e868a14e5651b96b6bd890a97aabbd328425b87ba0214a5032d76ae20",
    "gap_and_go_verified@v1": "01ce0bde8477be8b2044c724626de71378b0b3a65434a8592ba8f12fd328682f",
    "gap_baseline@v1": "038b5e8aca95950e33bbd7ecc5a01eb1cfa797db687889c5de677aef6f20c014",
    "intraday_continuation@v1": "de33cf2976757efc0275f80d029f8b453f4af886c0058c33f38f504c11a7a527",
    "intraday_continuation@v2": "c109c89824413df2ed9cdba804978ac264bfc64e07b990d1951a8157b760a118",
    "short_interest_ignition@v1": "adf8c21524fd43be9b7cc5c99d3439d5aecbed0d36943d095a092ff7475f25f9",
}


def test_frozen_spec_hashes_are_untouched():
    tree = ast.parse((ROOT / "tests" / "test_edge_pipeline.py").read_text())
    node = next(n for n in tree.body if isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "FROZEN_HASHES" for t in n.targets))
    assert ast.literal_eval(node.value) == PINNED_FROZEN_HASHES
    from edge import intraday as I, pipeline as P
    from edge.contracts import ExperimentSpec
    live = {v.experiment_id: v.spec_hash() for m in (P, I) for v in vars(m).values() if isinstance(v, ExperimentSpec)}
    for eid, h in PINNED_FROZEN_HASHES.items():
        assert live.get(eid) == h, eid
    # this hypothesis's specs live in their own module, never among the frozen live experiments
    assert not {SD.SECOND_DAY_OPEN.experiment_id, SD.SECOND_DAY_CONTROL.experiment_id} & set(live)
