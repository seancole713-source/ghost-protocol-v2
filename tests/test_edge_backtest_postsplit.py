"""Post-reverse-split momentum: a new hypothesis tested on history before any forward trade."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from edge import backtest_postsplit as PS
from edge import readout as R
from edge.ledger import MemoryStore

ET = ZoneInfo("America/New_York")
END = date(2026, 9, 18)
PRIOR = date(2026, 9, 17)


def ts(d, hh, mm):
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET).timestamp())


def iso(t):
    return datetime.fromtimestamp(t, tz=ET).isoformat()


class Resp:
    def __init__(self, p):
        self._p, self.status_code = p, 200

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


def minute_path(d, first_half, after):
    """30 one-minute bars 09:30-09:59 on heavy volume, then the rest of the day."""
    rows, t, prev = [], ts(d, 9, 30), first_half[0]
    for c in first_half + after:
        v = 20_000 if t < ts(d, 10, 0) else 5_000
        rows.append({"t": iso(t), "o": prev, "h": max(prev, c) + 0.005, "l": min(prev, c) - 0.005, "c": c, "v": v})
        prev, t = c, t + 60
    return rows


RISE = [round(6.50 + 0.01 * i, 2) for i in range(30)]           # 6.50 -> 6.79, closing near the high


class Market:
    def __call__(self, url, params=None, headers=None, timeout=None):
        params = params or {}
        if "/v3/reference/splits" in url:
            return Resp({"results": [{"ticker": "SPLT", "split_from": 10, "split_to": 1,
                                      "execution_date": (END - timedelta(days=60)).isoformat()},
                                     {"ticker": "FWD", "split_from": 1, "split_to": 4,          # forward split
                                      "execution_date": (END - timedelta(days=30)).isoformat()}]})
        if "/v2/aggs/grouped/" in url:
            d = date.fromisoformat(url.rstrip("/").split("/")[-1])
            px = 6.5 if d == PRIOR else 5.0                     # +30% the day before END
            v = 3_000_000 if d == PRIOR else 1_000_000
            return Resp({"results": [{"T": t, "o": px, "h": px, "l": px, "c": px, "v": v, "vw": px}
                                     for t in ("SPLT", "CTRL", "FWD")] +
                                    [{"T": "FLAT", "o": 5, "h": 5, "l": 5, "c": 5.0, "v": 1_000_000, "vw": 5}]})
        if url.endswith("/v2/stocks/bars"):
            win = minute_path(END, RISE, [6.85, 6.95, 7.10, 7.25, 7.30])          # through the +5% target
            lose = minute_path(END, RISE, [6.85, 6.70, 6.55, 6.40])               # through the -3% stop
            out = {"SPLT": win, "CTRL": lose, "FWD": lose}
            return Resp({"bars": {s: out[s] for s in params["symbols"].split(",") if s in out}})
        raise AssertionError(url)


def run():
    store = MemoryStore()
    out = PS.run(Market(), store, end_day=END, days=1, warmup=0, pace_s=0, sleep=lambda s: None)
    return store, out


def test_it_separates_post_split_runners_from_the_control():
    store, out = run()
    assert out["status"] == "complete"
    full = store.get("edge_backtest_postsplit", PS.VERSION)
    arms = {t["symbol"]: t["arm"] for t in full["trades"]}
    assert arms == {"SPLT": "post_split", "CTRL": "control_no_split", "FWD": "control_no_split"}
    by = {t["symbol"]: t for t in full["trades"]}
    assert by["SPLT"]["cost_10bps"]["simulated"] == "WIN" and by["CTRL"]["cost_10bps"]["simulated"] == "LOSS"
    assert by["SPLT"]["rvol_proxy"] >= PS.RVOL_MIN and "FLAT" not in by       # FLAT never ran: not a candidate


def test_costs_are_stated_and_the_report_reaches_the_backtest_view():
    store, _ = run()
    full = store.get("edge_backtest_postsplit", PS.VERSION)
    ps = full["arms"]["post_split"]
    assert set(ps) == {"signals", "cost_10bps", "cost_25bps", "cost_50bps"}
    assert ps["cost_50bps"]["expectancy_usd"]["mean"] < ps["cost_10bps"]["expectancy_usd"]["mean"]
    assert any("NOT the forward record" in x for x in full["limits"])
    store.put("edge_backtest", "v", {"version": "v", "completed_at": 1})
    view = R.view(store, "backtest")
    assert view["post_split_momentum"]["version"] == PS.VERSION and "trades" not in view["post_split_momentum"]


def test_it_runs_once_per_version():
    store, _ = run()
    assert PS.run(Market(), store, end_day=END, days=1, warmup=0, pace_s=0, sleep=lambda s: None)["status"] == "already_run"


# ------------------------------------------------- one price basis (v2, same fix as EDGE-09)
class SplitMarket:
    """RUNR and its unsplit twin CTRL both close $5.00, then $6.50 (+30%) the day before END, on
    END's basis, with 1M shares a day (3M the day before), and rise on heavy volume into 10:00
    on END. RUNR alone has one split of `mult` shares per share (2.0 = 2-for-1 forward, 0.1 =
    1-for-10 reverse) executed on `ex`. `flat` makes RUNR a flat $5.00 name with no momentum.

    Grouped daily bars are served AS TRADED only (adjusted=false is asserted): on a day before
    a split executed by END the price is `px x mult` and the volume `v / mult` (a 1-for-10
    reverse split traded at $0.50 before it, a 2-for-1 at $10). Minute bars are raw."""

    def __init__(self, mult, ex, *, flat=False):
        self.mult, self.ex, self.flat, self.params = mult, ex, flat, []

    def daily(self, t, d, *, split, flat=False):
        base_px = 6.5 if (d == PRIOR and not flat) else 5.0               # on END's basis
        base_v = 3_000_000 if (d == PRIOR and not flat) else 1_000_000
        f = self.mult if (split and d < self.ex <= END) else 1.0
        px, v = base_px * f, base_v / f                                   # as traded on day d
        return {"T": t, "o": px, "h": px, "l": px, "c": px, "v": v, "vw": px}

    def __call__(self, url, params=None, headers=None, timeout=None):
        params = params or {}
        if "/v3/reference/splits" in url:
            lo = date.fromisoformat(params["execution_date.gte"])
            hi = date.fromisoformat(params["execution_date.lte"])
            frm, to = (1, self.mult) if self.mult >= 1 else (round(1 / self.mult), 1)
            hit = [{"ticker": "RUNR", "execution_date": self.ex.isoformat(), "split_from": frm, "split_to": to}]
            return Resp({"results": hit if lo <= self.ex <= hi else []})
        if "/v2/aggs/grouped/" in url:
            assert params.get("adjusted") == "false", params              # one basis: as traded
            self.params.append(("grouped", params.get("adjusted")))
            d = date.fromisoformat(url.rstrip("/").split("/")[-1])
            return Resp({"results": [self.daily("RUNR", d, split=True, flat=self.flat),
                                     self.daily("CTRL", d, split=False)]})
        if url.endswith("/v2/stocks/bars"):
            assert params.get("adjustment") == "raw", params
            self.params.append(("minute", params.get("adjustment")))
            win = minute_path(END, RISE, [6.85, 6.95, 7.10, 7.25, 7.30])
            return Resp({"bars": {s: win for s in params["symbols"].split(",") if s in ("RUNR", "CTRL")}})
        raise AssertionError(url)


SPLIT_CASES = {
    "forward_2for1_on_the_day": (2.0, END),
    "reverse_1for10_on_the_day": (0.1, END),
    "forward_2for1_inside_the_5day_window": (2.0, date(2026, 9, 14)),
    "reverse_1for10_inside_the_5day_window": (0.1, date(2026, 9, 14)),
    "forward_2for1_inside_the_volume_window": (2.0, date(2026, 8, 27)),
    "reverse_1for10_inside_the_volume_window": (0.1, date(2026, 8, 27)),
    "forward_2for1_after_the_session": (2.0, date(2026, 9, 30)),
    "reverse_1for10_after_the_session": (0.1, date(2026, 9, 30)),
}


def run_split(mult, ex, **kw):
    store, get = MemoryStore(), SplitMarket(mult, ex, **kw)
    out = PS.run(get, store, end_day=END, days=1, warmup=0, pace_s=0, sleep=lambda s: None)
    return store, get, out


@pytest.mark.parametrize("case", sorted(SPLIT_CASES))
def test_returns_volume_and_fills_are_on_one_basis_across_splits(case):
    mult, ex = SPLIT_CASES[case]
    store, get, out = run_split(mult, ex)
    assert out["status"] == "complete" and out["price_basis"] == "raw_as_traded"
    assert set(get.params) == {("grouped", "false"), ("minute", "raw")}      # never a mix
    full = store.get("edge_backtest_postsplit", PS.VERSION)
    assert full["version"] == "post_split_momentum_backtest_v2"
    assert full["price_basis_detail"]["basis"] == "raw_as_traded" and full["resolver_version"]
    by = {t["symbol"]: t for t in full["trades"]}
    assert set(by) == {"RUNR", "CTRL"}
    # Whatever the split, RUNR reads exactly like its unsplit twin: the same +30% prior day and
    # 5-day move, the same RVOL against a 20-day share volume on END's basis, the same fill.
    for k in ("prior_day_pct", "five_day_pct", "rvol_proxy", "vwap", "hod", "cost_10bps"):
        assert by["RUNR"][k] == by["CTRL"][k], k
    assert by["RUNR"]["prior_day_pct"] == 30.0 and by["RUNR"]["five_day_pct"] == 30.0
    # The universe rule is unchanged: only a reverse split executed BEFORE the session counts.
    known_reverse = mult < 1 and ex < END
    assert by["RUNR"]["arm"] == ("post_split" if known_reverse else "control_no_split")
    assert by["CTRL"]["arm"] == "control_no_split" and "split_restated" not in by["CTRL"]
    if date(2026, 9, 10) <= ex <= END:     # restated within the 6-session close history: disclosed
        assert "split_restated" in by["RUNR"]
        if ex == END:
            assert by["RUNR"]["split_restated"]["prior_share_factor"] == pytest.approx(mult)
            assert by["RUNR"]["split_restated"]["raw_prior_close"] == pytest.approx(6.5 * mult)
    if ex > END:       # a split no one knew of on END restates nothing
        assert "split_restated" not in by["RUNR"]


def test_a_reverse_split_inside_the_lookback_is_not_fake_momentum():
    """Raw closes across a 1-for-10 reverse split jump 10x ($0.50 -> $5.00). Unrestated, a flat
    name reads +900% over five days and enters the universe; restated it is flat and does not."""
    store, _get, out = run_split(0.1, date(2026, 9, 14), flat=True)
    assert out["status"] == "complete"
    syms = {t["symbol"] for t in store.get("edge_backtest_postsplit", PS.VERSION)["trades"]}
    assert syms == {"CTRL"}


def test_no_split_list_no_run():
    class NoSplits(SplitMarket):
        def __call__(self, url, params=None, headers=None, timeout=None):
            if "/v3/reference/splits" in url:
                raise RuntimeError("HTTP 500")
            return super().__call__(url, params, headers, timeout)

    store = MemoryStore()
    out = PS.run(NoSplits(2.0, END), store, end_day=END, days=1, warmup=0, pace_s=0, sleep=lambda s: None)
    assert out["status"] == "error" and "split list unavailable" in out["why"]
    assert store.get("edge_backtest_postsplit", PS.VERSION) is None


def test_reverse_splits_come_from_the_one_split_list():
    from edge import backtest as BT
    idx = BT.split_index([
        {"ticker": "R", "execution_date": date(2026, 9, 1), "split_from": 10.0, "split_to": 1.0},
        {"ticker": "F", "execution_date": date(2026, 9, 1), "split_from": 1.0, "split_to": 4.0}])
    assert PS.reverse_splits(idx) == {"R": [date(2026, 9, 1)]}
