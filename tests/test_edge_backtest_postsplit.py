"""Post-reverse-split momentum: a new hypothesis tested on history before any forward trade."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

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
