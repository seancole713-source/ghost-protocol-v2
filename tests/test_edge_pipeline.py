"""edge shadow pipeline: one simulated market day, end to end.

Morning card -> forecasts and abstentions recorded before the open -> graded
from minute bars after the close -> miss review against the day's movers.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from edge import pipeline as P
from edge.ledger import Ledger, MemoryStore

ET = ZoneInfo("America/New_York")
DAY = date(2026, 9, 23)


def ts(hh, mm, d=DAY):
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET).timestamp())


def iso(t):
    return datetime.fromtimestamp(t, tz=ET).isoformat()


def daily_hist(close, vol, vw=None):
    out, d = [], DAY - timedelta(days=30)
    while d < DAY:
        if d.weekday() < 5:
            out.append({"t": iso(ts(4, 0, d)), "o": close, "h": close, "l": close, "c": close,
                        "v": vol, "vw": vw or close})
        d += timedelta(days=1)
    return out


MORNING_GAINERS = ["SHOP", "USAR", "THIN", "STALE", "DILU"]
PREV = {"SHOP": 137.92, "USAR": 18.0, "THIN": 4.0, "STALE": 20.0, "DILU": 6.0}
VOL = {"SHOP": 9_000_000, "USAR": 6_000_000, "THIN": 80_000, "STALE": 2_000_000, "DILU": 3_000_000}
PRE = {"SHOP": 146.71, "USAR": 19.6, "THIN": 4.5, "STALE": 21.5, "DILU": 6.6}


class Resp:
    def __init__(self, payload):
        self._p, self.status_code = payload, 200

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


class FakeAlpaca:
    def __init__(self, phase="morning"):
        self.phase, self.calls = phase, []

    def __call__(self, url, params=None, headers=None, timeout=None):
        params = params or {}
        self.calls.append((url, dict(params)))
        if "screener/stocks/movers" in url:
            if self.phase == "morning":
                g = [{"symbol": s, "percent_change": 8.0} for s in MORNING_GAINERS]
                return Resp({"gainers": g, "losers": [], "last_updated": iso(ts(9, 4))})
            return Resp({"gainers": [{"symbol": s} for s in ("SHOP", "USAR", "GAPR")], "losers": [],
                         "last_updated": iso(ts(16, 15))})
        if url.endswith("/v2/stocks/bars") and params.get("timeframe") == "1Day":
            syms = params["symbols"].split(",")
            bars = {s: daily_hist(PREV.get(s, 10.0), VOL.get(s, 5_000_000)) for s in syms}
            if self.phase == "evening":
                today = {"SHOP": (146.0, 158.0, 145.5, 156.0), "USAR": (19.5, 20.1, 18.9, 19.0),
                         "GAPR": (13.0, 13.1, 11.8, 12.0)}
                for s in syms:
                    o, h, l, c = today.get(s, (10, 10, 10, 10))
                    bars[s] = bars[s] + [{"t": iso(ts(4, 0)), "o": o, "h": h, "l": l, "c": c, "v": 1, "vw": c}]
            return Resp({"bars": bars})
        if url.endswith("/v2/stocks/bars") and params.get("timeframe") == "1Min":
            syms = params["symbols"].split(",")
            out = {}
            for s in syms:
                if s == "SHOP":   # up through the 148.18 trigger, then to the 155.59 target
                    path = [146.5, 148.5, 150.0, 152.0, 154.0, 156.0]
                elif s == "GAPR":
                    path = [13.0, 12.8, 12.5, 12.2, 12.0, 11.9]
                else:
                    path = [19.5, 19.8, 20.0, 19.6, 19.2, 19.0]
                rows, t, prev = [], ts(9, 30), path[0]
                for c in path:
                    rows.append({"t": iso(t), "o": prev, "h": max(prev, c) + 0.05, "l": min(prev, c) - 0.05, "c": c, "v": 1000})
                    prev, t = c, t + 600
                out[s] = rows
            return Resp({"bars": out})
        if "/v2/stocks/snapshots" in url:
            snaps = {}
            for s in params["symbols"].split(","):
                t = ts(16, 0, DAY - timedelta(days=1)) if s == "STALE" else ts(9, 2)
                snaps[s] = {"latestTrade": {"t": iso(t), "p": PRE[s]}}
            return Resp(snaps)
        if "/v1beta1/news" in url:
            return Resp({"news": [
                {"headline": "Shopify announces partnership with Meta for agent checkout",
                 "created_at": iso(ts(7, 0)), "symbols": ["SHOP", "META"], "source": "benzinga", "url": "https://x/1"},
                {"headline": "Trump announces Greenland security deal", "created_at": iso(ts(6, 0)),
                 "symbols": ["USAR", "CRML"], "source": "benzinga", "url": "https://x/2"},
                {"headline": "Dilu Corp prices $20M registered direct offering", "created_at": iso(ts(8, 0)),
                 "symbols": ["DILU"], "source": "benzinga", "url": "https://x/3"},
                {"headline": "Thin Inc wins contract award from Navy", "created_at": iso(ts(8, 30)),
                 "symbols": ["THIN"], "source": "benzinga", "url": "https://x/4"},
                {"headline": "Stale Co raises full-year guidance", "created_at": iso(ts(7, 30)),
                 "symbols": ["STALE"], "source": "benzinga", "url": "https://x/5"},
            ]})
        raise AssertionError(f"unrouted {url}")


@pytest.fixture
def ledger():
    return Ledger(MemoryStore())


def test_the_morning_card_forecasts_only_what_passes_and_says_why_for_the_rest(ledger):
    out = P.morning_card(FakeAlpaca(), ledger, now=ts(9, 10))
    assert out["status"] == "issued" and out["forecasts"] == ["SHOP"]
    assert out["coverage_note"] == "4/5 candidates had a fresh IEX premarket price"
    abst = {a["symbol"]: a["reasons"] for a in ledger.store.scan("abstentions", experiment_id=P.EID)}
    assert "sector sympathy" in abst["USAR"][0]
    assert any("avg volume" in r for r in abst["THIN"])
    assert "prior session" in abst["STALE"][0]
    # Both reasons stand: an offering is not a bullish company catalyst, AND it dilutes.
    assert any("dilution" in r for r in abst["DILU"]) and any("catalyst" in r for r in abst["DILU"])


def test_the_card_is_written_once_and_only_before_the_open(ledger):
    P.morning_card(FakeAlpaca(), ledger, now=ts(9, 10))
    assert P.morning_card(FakeAlpaca(), ledger, now=ts(9, 15))["status"] == "already_issued"
    other = Ledger(MemoryStore())
    assert P.morning_card(FakeAlpaca(), other, now=ts(9, 31))["status"] == "outside_card_window"


def test_weekends_and_holidays_do_nothing(ledger, monkeypatch):
    sat = int(datetime(2026, 9, 26, 9, 10, tzinfo=ET).timestamp())
    assert P.run(FakeAlpaca(), ledger, now=sat)["status"] == "market_closed"
    monkeypatch.setenv("EDGE_HOLIDAYS", "2026-09-23")
    assert P.morning_card(FakeAlpaca(), ledger, now=ts(9, 10))["status"] == "market_closed"


def test_after_the_close_forecasts_are_graded_in_two_separate_records(ledger):
    P.morning_card(FakeAlpaca(), ledger, now=ts(9, 10))
    out = P.resolve_day(FakeAlpaca("evening"), ledger, day=DAY, now=ts(16, 25))
    assert out["settled"]["SHOP"] == {"forecast": "WIN", "simulated": "WIN",
                                      "pnl_usd": out["settled"]["SHOP"]["pnl_usd"]}
    rep = ledger.report(P.EID)
    assert rep["records"]["forecast"]["wins"] == 1
    assert rep["records"]["actual"]["by_outcome"] == {"PENDING": 1}     # no broker yet
    assert P.resolve_day(FakeAlpaca("evening"), ledger, day=DAY, now=ts(16, 30))["status"] == "nothing_pending"


def test_the_miss_review_separates_coverage_from_gap_only(ledger):
    P.morning_card(FakeAlpaca(), ledger, now=ts(9, 10))
    out = P.miss_review(FakeAlpaca("evening"), ledger, day=DAY, now=ts(16, 25))
    rows = {r["symbol"]: r for r in ledger.store.get("edge_miss", "2026-09-23")["rows"]}
    assert rows["SHOP"]["label"] == "CAUGHT"
    assert rows["GAPR"]["opportunity"] == "GAP_ONLY"            # +30% gap, nothing after the open
    # USAR peaked +11.7% on the day, but only +3% after its 19.50 open:
    # the move happened before anyone could buy, so it is not a miss.
    assert rows["USAR"]["opportunity"] == "GAP_ONLY" and "label" not in rows["USAR"]
    assert (out["executable"], out["caught"], out["recall"]) == (1, 1, 1.0)


def test_a_failing_grade_does_not_cost_the_miss_review(ledger):
    P.morning_card(FakeAlpaca(), ledger, now=ts(9, 10))

    class Broken(FakeAlpaca):
        def __call__(self, url, params=None, headers=None, timeout=None):
            if (params or {}).get("timeframe") == "1Min" and "SHOP" in (params or {}).get("symbols", "") \
                    and "GAPR" not in params.get("symbols", ""):
                raise ConnectionError("reset")
            return super().__call__(url, params, headers, timeout)

    out = P.run(Broken("evening"), ledger, now=ts(16, 25))
    assert out["resolve"]["status"] == "error"
    assert out["miss_review"]["status"] == "reviewed"


def test_the_auto_experiment_is_not_the_operators_rule():
    from edge.contracts import GAP_AND_GO_V1
    assert P.EID == "gap_and_go_auto@v1"
    assert P.SPEC.spec_hash() != GAP_AND_GO_V1.spec_hash()
    assert (P.SPEC.target_mult, P.SPEC.stop_mult) == (GAP_AND_GO_V1.target_mult, GAP_AND_GO_V1.stop_mult)


def test_only_ticks_that_did_something_are_logged(ledger):
    first = P.run(FakeAlpaca(), ledger, now=ts(9, 10))
    again = P.run(FakeAlpaca(), ledger, now=ts(9, 15))
    assert P.noteworthy(first) and not P.noteworthy(again)
    assert not P.noteworthy(P.run(FakeAlpaca(), ledger, now=ts(12, 0)))
