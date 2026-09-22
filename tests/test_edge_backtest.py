"""Historical test of the frozen rule: point-in-time or it doesn't count."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from edge import backtest as BT
from edge.ledger import MemoryStore

ET = ZoneInfo("America/New_York")
END = date(2026, 9, 18)


def ts(d, hh, mm):
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET).timestamp())


def iso(t):
    return datetime.fromtimestamp(t, tz=ET).isoformat()


class Resp:
    def __init__(self, p, code=200):
        self._p, self.status_code = p, code

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def path_bars(d, pre_close, rth_path, *, pre_minute=(9, 5)):
    early = round(pre_close * 0.99, 2)          # an 08:30 print, as a real premarket has
    rows = [{"t": iso(ts(d, 8, 30)), "o": early, "h": early, "l": early, "c": early, "v": 3000},
            {"t": iso(ts(d, *pre_minute)), "o": pre_close, "h": pre_close, "l": pre_close, "c": pre_close, "v": 5000}]
    t, prev = ts(d, 9, 30), rth_path[0]
    for c in rth_path:
        rows.append({"t": iso(t), "o": prev, "h": max(prev, c) + 0.01, "l": min(prev, c) - 0.01, "c": c, "v": 1000})
        prev, t = c, t + 600
    return rows


class Market:
    """Every session: RUNR/FADE/LATE trade at $10 on $10M. On END they gap."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, params=None, headers=None, timeout=None):
        params = params or {}
        self.calls.append(url)
        if "/v2/aggs/grouped/" in url:
            d = date.fromisoformat(url.rstrip("/").split("/")[-1])
            base = [{"T": t, "o": 10.0, "h": 10.1, "l": 9.9, "c": 10.0, "v": 1_000_000, "vw": 10.0}
                    for t in ("RUNR", "FADE", "LATE")]
            if d == END:
                base = [{"T": "RUNR", "o": 10.6, "h": 11.3, "l": 10.5, "c": 11.2, "v": 3e6, "vw": 10.9},
                        {"T": "FADE", "o": 10.6, "h": 10.7, "l": 10.0, "c": 10.1, "v": 3e6, "vw": 10.3},
                        {"T": "LATE", "o": 10.6, "h": 10.7, "l": 10.5, "c": 10.6, "v": 3e6, "vw": 10.6}]
            return Resp({"results": base})
        if url.endswith("/v2/stocks/bars"):
            d = END
            out = {"RUNR": path_bars(d, 10.60, [10.6, 10.75, 10.9, 11.2, 11.25]),
                   "FADE": path_bars(d, 10.60, [10.6, 10.75, 10.4, 10.2, 10.1]),
                   # LATE's only premarket bar starts 09:10 -> closes after the card: unusable
                   "LATE": path_bars(d, 10.60, [10.6, 10.6], pre_minute=(9, 10))}
            return Resp({"bars": {s: out[s] for s in params["symbols"].split(",") if s in out}})
        if "/v1beta1/news" in url:
            return Resp({"news": [
                {"headline": "RUNR wins contract award from Navy", "created_at": iso(ts(END, 8, 0)),
                 "symbols": ["RUNR"], "source": "x", "url": "u1"},
                {"headline": "FADE announces FDA approval", "created_at": iso(ts(END, 9, 20)),   # after 09:10
                 "symbols": ["FADE"], "source": "x", "url": "u2"},
            ]})
        raise AssertionError(url)


def test_the_backtest_uses_only_what_was_knowable_at_910():
    store = MemoryStore()
    out = BT.run(Market(), store, end_day=END, days=1, warmup=12, pace_s=0, sleep=lambda s: None)
    assert out["status"] == "complete"
    full = store.get("edge_backtest", BT.BACKTEST_VERSION)
    (sess,) = full["sessions_detail"]
    res = {(r["experiment"], r["symbol"]): r for r in sess["results"]}
    # RUNR: dated catalyst before 09:10 -> both experiments take it, and it wins.
    assert res[("gap_and_go_auto@v1", "RUNR")]["simulated"] == "WIN"
    # FADE's "catalyst" was published at 09:20 -- after the card. Not usable:
    # the catalyst rule skips it; only the no-catalyst baseline takes it.
    assert ("gap_and_go_auto@v1", "FADE") not in res
    assert res[("gap_baseline@v1", "FADE")]["simulated"] in ("LOSS", "TIME_EXIT")
    # LATE's only premarket bar closes after 09:10: no reference price, no trade.
    assert not any(k[1] == "LATE" for k in res)
    assert sess["priced"] == 2


def test_the_report_states_its_limits_and_an_interval():
    store = MemoryStore()
    BT.run(Market(), store, end_day=END, days=1, warmup=12, pace_s=0, sleep=lambda s: None)
    full = store.get("edge_backtest", BT.BACKTEST_VERSION)
    assert any("NOT the forward record" in x for x in full["limits"])
    auto = full["experiments"]["gap_and_go_auto@v1"]
    assert auto["filled"] == 1 and auto["wilson_ci"][0] < 0.375 < auto["wilson_ci"][1]
    assert auto["verdict"].startswith("undecided")


def test_it_runs_once_and_paces_polygon():
    store, waits = MemoryStore(), []
    BT.run(Market(), store, end_day=END, days=1, warmup=12, pace_s=13, sleep=waits.append)
    assert waits and set(waits) == {13}
    assert BT.run(Market(), store, end_day=END, days=1, warmup=12)["status"] == "already_run"


def test_liquidity_uses_prior_sessions_only():
    r = BT.Rolling(n=3)
    for v in (100, 200, 300, 400):
        r.push([{"T": "X", "v": v, "vw": 1.0}])
    assert r.avg("X", min_days=3) == (300, 300)      # the last three PRIOR days
    assert BT.Rolling().avg("NEW") == (None, None)   # no history -> unknown, not zero


def test_the_backtest_builds_the_model_dataset_and_tries_to_qualify_a_model():
    store = MemoryStore()
    BT.run(Market(), store, end_day=END, days=1, warmup=12, pace_s=0, sleep=lambda s: None)
    ds = store.get("edge_dataset", BT.BACKTEST_VERSION)
    assert ds["features"] and ds["rows"]
    row = ds["rows"][0]
    assert set(row["features"]) >= {"gap_855", "pre_trend", "catalyst_company"}
    full = store.get("edge_backtest", BT.BACKTEST_VERSION)
    assert full["model"]["status"] == "not_qualified"       # one session can never qualify
    assert "dataset" not in full["sessions_detail"][0]
