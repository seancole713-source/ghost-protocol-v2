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
    shop = out["settled"]["gap_and_go_auto@v1:SHOP"]
    assert (shop["forecast"], shop["simulated"]) == ("WIN", "WIN")
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
    assert out["resolve"]["status"] == "error"            # recorded, not swallowed
    # The miss review is not tied to grading: it runs the NEXT morning on the
    # completed session, when full-market bars are entitled.
    nxt = P.run(FakeAlpaca("evening"), ledger, now=ts(8, 0, DAY + timedelta(days=1)))
    assert nxt["miss_review"]["status"] == "reviewed"
    assert ledger.store.get("edge_miss", "2026-09-23") is not None


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


class FullMarket(FakeAlpaca):
    """Evening fake with Polygon grouped daily -- the probe-verified path."""

    def __call__(self, url, params=None, headers=None, timeout=None):
        if "/v2/aggs/grouped/" in url:
            day = url.rstrip("/").split("/")[-1]
            if day == DAY.isoformat():
                rows = [
                    {"T": "SHOP", "o": 146.0, "h": 158.0, "l": 145.5, "c": 156.0, "v": 9_000_000},
                    {"T": "GAPR", "o": 13.0, "h": 13.1, "l": 11.8, "c": 12.0, "v": 900_000},
                    {"T": "NEWX", "o": 5.0, "h": 5.6, "l": 4.99, "c": 5.5, "v": 2_000_000},   # never seen at 9am
                    {"T": "PNNY", "o": 0.40, "h": 0.60, "l": 0.39, "c": 0.55, "v": 90_000_000},  # < $1
                    {"T": "THNX", "o": 3.0, "h": 3.4, "l": 2.99, "c": 3.3, "v": 100_000},     # < $1M traded
                    {"T": "ABCD.WS", "o": 1.0, "h": 2.0, "l": 1.0, "c": 2.0, "v": 5_000_000},  # warrant
                ]
            else:
                rows = [{"T": t, "c": c} for t, c in
                        (("SHOP", 137.92), ("GAPR", 10.0), ("NEWX", 5.0), ("PNNY", 0.40), ("THNX", 3.0), ("ABCD.WS", 1.0))]
            return Resp({"results": rows})
        if (params or {}).get("timeframe") == "1Min" and "NEWX" in (params or {}).get("symbols", ""):
            out = super().__call__(url, params, headers, timeout)._p
            t, rows = ts(9, 30), []
            for c in (5.0, 5.1, 5.3, 5.5, 5.6):
                rows.append({"t": iso(t), "o": c - 0.1, "h": c + 0.02, "l": c - 0.1, "c": c, "v": 1000})
                t += 600
            out["bars"]["NEWX"] = rows
            return Resp(out)
        return super().__call__(url, params, headers, timeout)


def test_the_miss_review_sees_the_whole_market_when_polygon_answers(ledger):
    P.morning_card(FakeAlpaca(), ledger, now=ts(9, 10))
    out = P.miss_review(FullMarket("evening"), ledger, day=DAY, now=ts(16, 25))
    assert out["coverage_note"].startswith("full market: 3 liquid US stocks")
    rows = {r["symbol"]: r for r in ledger.store.get("edge_miss", "2026-09-23")["rows"]}
    assert set(rows) == {"SHOP", "GAPR", "NEWX"}                 # penny, illiquid, warrant filtered out
    assert rows["NEWX"]["label"] == "UNIVERSE_COVERAGE"          # moved +12% in RTH, never on the 9am radar
    assert rows["SHOP"]["label"] == "CAUGHT"
    assert out["recall"] == 0.5


def test_monday_morning_reviews_friday():
    assert P.previous_trading_day(date(2026, 9, 28)) == date(2026, 9, 25)



def test_the_baseline_trades_the_same_gaps_without_asking_why(ledger):
    """No catalyst check: it takes USAR (sympathy) and would take an offering.
    If the catalyst filter can't beat this over time, it isn't adding edge."""
    out = P.morning_card(FakeAlpaca(), ledger, now=ts(9, 10))
    assert out["forecasts"] == ["SHOP"]
    assert out["baseline_forecasts"] == ["SHOP", "USAR"]        # top 2 by dollar volume
    rep = ledger.report(P.BASE_EID)
    assert rep["forecasts"] == 2
    assert P.GAP_BASELINE.spec_hash() != P.SPEC.spec_hash()


def test_the_card_carries_what_a_live_release_would_have_said(ledger):
    P.morning_card(FakeAlpaca(), ledger, now=ts(9, 10))
    card = ledger.store.get("edge_cards", "2026-09-23")
    assert card["health"]["quotes_iex"]["coverage"] == 0.8
    assert card["health_banner"] == "1 setup. Coverage healthy."
    assert "shadow records regardless" in card["health_note"]


# ---------------------------------------------------------------- universe --

from edge import universe as U


class RefTickers:
    """Polygon reference tickers, two pages, types as Polygon labels them."""

    def __init__(self):
        self.calls = 0

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls += 1
        if "cursor=p2" in url:
            return Resp({"results": [{"ticker": "NEWX", "type": "CS"}, {"ticker": "SPY", "type": "ETF"}]})
        return Resp({"results": [{"ticker": t, "type": "CS"} for t in ("SHOP", "GAPR", "USAR")] +
                                [{"ticker": "ABCD.WS", "type": "WARRANT"}],
                     "next_url": "https://api.polygon.io/v3/reference/tickers?cursor=p2"})


def test_universe_snapshot_keeps_common_stock_and_diffs_the_day(monkeypatch, ledger):
    monkeypatch.setenv("POLYGON_API_KEY", "k")
    store = ledger.store
    store.put("edge_universe", "2026-09-22", {"day": "2026-09-22", "symbols": ["SHOP", "GONE"]})
    out = U.step(RefTickers(), store, day="2026-09-23", now=ts(6, 5), sleep=lambda s: None)
    snap = store.get("edge_universe", "2026-09-23")
    assert out["status"] == "complete" and snap["symbols"] == ["GAPR", "NEWX", "SHOP", "USAR"]
    assert snap["added"] == ["GAPR", "NEWX", "USAR"] and snap["removed"] == ["GONE"]
    assert U.step(RefTickers(), store, day="2026-09-23", now=ts(6, 10))["status"] == "already_complete"


def test_universe_resumes_across_ticks(monkeypatch, ledger):
    monkeypatch.setenv("POLYGON_API_KEY", "k")
    first = U.step(RefTickers(), ledger.store, day="2026-09-23", now=ts(6, 5), pages_per_tick=1)
    assert first["status"] == "in_progress" and first["so_far"] == 3
    second = U.step(RefTickers(), ledger.store, day="2026-09-23", now=ts(6, 10), pages_per_tick=1)
    assert second["status"] == "complete" and second["count"] == 4


def test_with_a_universe_an_unsurfaced_listed_stock_is_a_detection_failure(monkeypatch, ledger):
    """NEWX is a listed common stock; the 9am screen never showed it. That is the
    radar failing to see, not the system being out of scope."""
    ledger.store.put("edge_universe", "2026-09-23", {"day": "2026-09-23", "symbols": ["SHOP", "GAPR", "NEWX", "USAR"]})
    P.morning_card(FakeAlpaca(), ledger, now=ts(9, 10))
    P.miss_review(FullMarket("evening"), ledger, day=DAY, now=ts(16, 25))
    rows = {r["symbol"]: r for r in ledger.store.get("edge_miss", "2026-09-23")["rows"]}
    assert rows["NEWX"]["label"] == "DETECTION_FAILURE"


def test_after_the_close_every_priced_candidate_is_graded_as_a_labelled_counterfactual(ledger):
    from edge import scorecard as SC
    P.morning_card(FakeAlpaca(), ledger, now=ts(9, 10))
    assert SC.grade_card(FakeAlpaca("evening"), ledger.store, day=DAY, now=ts(16, 0))["status"] == "too_early"
    out = SC.grade_card(FakeAlpaca("evening"), ledger.store, day=DAY, now=ts(16, 25))
    assert out["status"] == "graded" and out["rows"] == 4          # STALE had no fresh price
    rec = ledger.store.get("edge_card_outcomes", DAY.isoformat())
    shop = next(r for r in rec["rows"] if r["symbol"] == "SHOP")
    assert shop["outcome"] == "WIN" and shop["auto"] == "ELIGIBLE" and "counterfactual" in rec["label"]
    # never part of any experiment's record
    assert ledger.report(P.EID)["forecasts"] == 1
    again = SC.grade_card(FakeAlpaca("evening"), ledger.store, day=DAY, now=ts(16, 30))
    assert again["status"] == "already_graded"


def test_the_evening_tick_grades_the_card(ledger):
    P.morning_card(FakeAlpaca(), ledger, now=ts(9, 10))
    out = P.run(FakeAlpaca("evening"), ledger, now=ts(16, 25))
    assert out["card_graded"]["status"] == "graded" and P.noteworthy(out)


def test_a_card_tick_that_died_after_recording_forecasts_is_recovered_not_blocked(ledger):
    P.morning_card(FakeAlpaca(), ledger, now=ts(9, 10))
    card = ledger.store.get("edge_cards", DAY.isoformat())
    ledger.store._t["edge_cards"].pop(DAY.isoformat())          # the card write never happened
    out = P.morning_card(FakeAlpaca(), ledger, now=ts(9, 15))   # a later tick, different prices/time
    assert out["status"] == "issued" and out["forecasts"] == card["forecasts"]
    assert len(ledger.store.scan("forecasts", experiment_id=P.EID)) == 1
