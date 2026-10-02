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
        if "/v3/reference/splits" in url:
            return Resp({"results": []})
        if "/v2/aggs/grouped/" in url:
            assert params.get("adjusted") == "false", params      # one basis: as traded
            d = date.fromisoformat(url.rstrip("/").split("/")[-1])
            base = [{"T": t, "o": 10.0, "h": 10.1, "l": 9.9, "c": 10.0, "v": 1_000_000, "vw": 10.0}
                    for t in ("RUNR", "FADE", "LATE")]
            if d == END:
                base = [{"T": "RUNR", "o": 10.6, "h": 11.3, "l": 10.5, "c": 11.2, "v": 3e6, "vw": 10.9},
                        {"T": "FADE", "o": 10.6, "h": 10.7, "l": 10.0, "c": 10.1, "v": 3e6, "vw": 10.3},
                        {"T": "LATE", "o": 10.6, "h": 10.7, "l": 10.5, "c": 10.6, "v": 3e6, "vw": 10.6}]
            return Resp({"results": base})
        if url.endswith("/v2/stocks/bars"):
            assert params.get("adjustment") == "raw", params
            d = END
            out = {"RUNR": path_bars(d, 10.60, [10.6, 10.75, 10.9, 11.2, 11.25]),
                   "FADE": path_bars(d, 10.60, [10.6, 10.75, 10.4, 10.2, 10.1]),
                   # LATE's only premarket bar starts 09:10 -> closes after the card: unusable
                   "LATE": path_bars(d, 10.60, [10.6, 10.6], pre_minute=(9, 10))}
            return Resp({"bars": {s: out[s] for s in params["symbols"].split(",") if s in out}})
        if "/v1beta1/news" in url:
            # A past window must be bounded, or Alpaca pages back from NOW (v2's bug).
            assert params.get("end") == iso(ts(END, 9, 10)).replace("+00:00", "Z") or \
                params.get("end", "").startswith(str(END)), params
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
    assert auto["verdict"].startswith("too few trades (n=1)")


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


def test_the_report_stress_tests_costs_and_a_one_minute_entry_delay():
    """Phase 1: does the result survive real-world friction? 25 bps a side (the first paper
    fills averaged ~0.2% worse than the trigger) and an order that goes live a minute late."""
    store = MemoryStore()
    BT.run(Market(), store, end_day=END, days=1, warmup=12, pace_s=0, sleep=lambda s: None)
    full = store.get("edge_backtest", BT.BACKTEST_VERSION)
    auto = full["experiments"]["gap_and_go_auto@v1"]
    assert set(auto["stress"]) == set(BT.STRESS)
    (sess,) = full["sessions_detail"]
    runr = next(r for r in sess["results"] if r["symbol"] == "RUNR" and r["experiment"] == "gap_and_go_auto@v1")
    base, costly = runr["pnl_usd"], runr["stress"]["cost_25bps"]["pnl_usd"]
    assert runr["stress"]["cost_25bps"]["simulated"] == runr["simulated"] and costly < base   # same trade, less profit
    for name, v in auto["stress"].items():
        assert v["filled"] <= auto["filled"] + 1 and "expectancy_usd" in v, name


# ------------------------------------------------------------------ one price basis (EDGE-09)
class SplitMarket:
    """RUNR trades $10M a day at $10 on END's basis and gaps +6% on END. One split of `mult`
    shares per share (2.0 = 2-for-1 forward, 0.1 = 1-for-10 reverse) executes on `ex`.

    Raw grouped daily bars (adjusted=false) carry prices and volume AS TRADED: before a split
    that executed by END the price is `10 x mult` and the volume `1M / mult`. Adjusted bars
    (adjusted=true) restate every day before `ex` for the split, as known TODAY -- even when it
    executed after END. Minute bars are raw. Dollar volume is $10M a day on every basis."""

    def __init__(self, mult, ex):
        self.mult, self.ex, self.params = mult, ex, []

    def daily(self, d, adjusted):
        f = self.mult if d < self.ex <= END else 1.0               # as traded on day d
        o, c = (10.6, 11.2) if d == END else (10.0, 10.0)          # on END's basis
        o, c, v = o * f, c * f, 1_000_000 / f
        if adjusted and d < self.ex:
            o, c, v = o / self.mult, c / self.mult, v * self.mult
        return {"T": "RUNR", "o": o, "h": max(o, c), "l": min(o, c), "c": c, "v": v, "vw": c}

    def __call__(self, url, params=None, headers=None, timeout=None):
        params = params or {}
        if "/v3/reference/splits" in url:
            lo = date.fromisoformat(params["execution_date.gte"])
            hi = date.fromisoformat(params["execution_date.lte"])
            frm, to = (1, self.mult) if self.mult >= 1 else (round(1 / self.mult), 1)
            hit = [{"ticker": "RUNR", "execution_date": self.ex.isoformat(), "split_from": frm, "split_to": to}]
            return Resp({"results": hit if lo <= self.ex <= hi else []})
        if "/v2/aggs/grouped/" in url:
            self.params.append(("grouped", params.get("adjusted")))
            d = date.fromisoformat(url.rstrip("/").split("/")[-1])
            return Resp({"results": [self.daily(d, params.get("adjusted") == "true")]})
        if url.endswith("/v2/stocks/bars"):
            self.params.append(("minute", params.get("adjustment")))
            return Resp({"bars": {"RUNR": path_bars(END, 10.60, [10.6, 10.75, 10.9, 11.2, 11.25])}})
        if "/v1beta1/news" in url:
            return Resp({"news": [{"headline": "RUNR wins contract award from Navy",
                                   "created_at": iso(ts(END, 8, 0)), "symbols": ["RUNR"], "source": "x",
                                   "url": "u1"}]})
        raise AssertionError(url)


SPLIT_CASES = {
    "forward_2for1_on_the_day": (2.0, END),
    "reverse_1for10_on_the_day": (0.1, END),
    "forward_2for1_inside_the_volume_window": (2.0, date(2026, 9, 11)),
    "reverse_1for10_inside_the_volume_window": (0.1, date(2026, 9, 11)),
    "forward_2for1_after_the_session": (2.0, date(2026, 9, 30)),
    "reverse_1for10_after_the_session": (0.1, date(2026, 9, 30)),
}


@pytest.mark.parametrize("case", sorted(SPLIT_CASES))
def test_the_gap_and_the_volume_are_on_one_basis_across_splits(case):
    mult, ex = SPLIT_CASES[case]
    store, get = MemoryStore(), SplitMarket(mult, ex)
    out = BT.run(get, store, end_day=END, days=1, warmup=12, pace_s=0, sleep=lambda s: None)
    assert out["status"] == "complete" and out["price_basis"] == BT.PRICE_BASIS == "raw_as_traded"
    # Every daily series asked as traded, every minute series raw: never a mix.
    assert set(get.params) == {("grouped", "false"), ("minute", "raw")}
    full = store.get("edge_backtest", BT.BACKTEST_VERSION)
    assert full["version"] == "gap_and_go_backtest_v8" and full["price_basis_detail"]["basis"] == "raw_as_traded"
    assert store.get("edge_dataset", BT.BACKTEST_VERSION)["price_basis"] == "raw_as_traded"
    (sess,) = full["sessions_detail"]
    assert sess["price_basis"] == "raw_as_traded"
    runr = next(r for r in sess["results"] if r["experiment"] == "gap_and_go_auto@v1")
    # Whatever the split, the previous close is on END's basis ($10, +6% to the 09:05 print),
    # and the 20-day share volume is END-basis shares (1M), not a mix of two share counts.
    assert runr["prev_close"] == pytest.approx(10.0)
    assert runr["gap_pct"] == pytest.approx(6.0)
    assert runr["avg_shares"] == pytest.approx(1_000_000)
    assert runr["simulated"] == "WIN"
    if ex == END:     # the restatement is disclosed on the session
        assert sess["split_restated"]["RUNR"]["raw_prev_close"] == pytest.approx(10.0 * mult)
        assert sess["split_restated"]["RUNR"]["share_factor"] == pytest.approx(mult)
    else:
        assert sess["split_restated"] == {}


@pytest.mark.parametrize("mult", [2.0, 0.1])
def test_v7s_mixed_basis_turned_a_later_split_into_a_fake_gap(mult):
    """What v7 did: previous close from ADJUSTED daily bars, reference from RAW minute bars."""
    from edge import detectors as D
    get = SplitMarket(mult, date(2026, 9, 30))           # a split twelve days AFTER END
    prev = get(f"x/v2/aggs/grouped/locale/us/market/stocks/{date(2026, 9, 17)}", {"adjusted": "true"})
    adj_prev = prev.json()["results"][0]["c"]
    assert adj_prev == pytest.approx(10.0 / mult)        # restated for a split no one knew of on END
    mixed = D.gap(adj_prev, 10.60)
    assert mixed.state == D.FAIL and abs(mixed.value - 6.0) > 50     # +112% or -89%: a fake gap
    assert D.gap(10.0, 10.60).value == pytest.approx(6.0)            # one basis: the real +6%


def test_a_split_counts_only_once_it_has_executed():
    splits = BT.split_index([
        {"ticker": "X", "execution_date": date(2026, 9, 11), "split_from": 1.0, "split_to": 2.0},
        {"ticker": "X", "execution_date": date(2026, 9, 25), "split_from": 10.0, "split_to": 1.0}])
    f = BT.share_factor
    assert f(splits, "X", date(2026, 9, 10), date(2026, 9, 11)) == 2.0        # executed on the day
    assert f(splits, "X", date(2026, 9, 11), date(2026, 9, 18)) == 1.0        # already in the prior close
    assert f(splits, "X", date(2026, 9, 1), date(2026, 9, 18)) == 2.0         # the later one is not known yet
    assert f(splits, "X", date(2026, 9, 1), date(2026, 9, 25)) == pytest.approx(0.2)
    assert f(splits, "X", None, date(2026, 9, 11)) == 2.0 and f(splits, "Y", None, date(2026, 9, 11)) == 1.0
    r = BT.Rolling(n=3)
    r.push([{"T": "X", "v": 100, "vw": 20.0}], date(2026, 9, 10))           # before the 2-for-1
    r.push([{"T": "X", "v": 200, "vw": 10.0}], date(2026, 9, 11))
    r.push([{"T": "X", "v": 200, "vw": 10.0}], date(2026, 9, 14))
    assert r.avg("X", min_days=3, as_of=date(2026, 9, 15), splits=splits) == (200, 2000)
    assert r.avg("X", min_days=3) == (pytest.approx(500 / 3), 2000)            # as pushed: two share counts


def test_no_split_list_no_run():
    """A partial split list would leave some names on a mixed basis: the run stores nothing."""
    class NoSplits(Market):
        def __call__(self, url, params=None, headers=None, timeout=None):
            if "/v3/reference/splits" in url:
                return Resp({}, code=500)
            return super().__call__(url, params, headers, timeout)

    store = MemoryStore()
    out = BT.run(NoSplits(), store, end_day=END, days=1, warmup=12, pace_s=0, sleep=lambda s: None)
    assert out["status"] == "error" and "split list unavailable" in out["why"]
    assert store.get("edge_backtest", BT.BACKTEST_VERSION) is None
