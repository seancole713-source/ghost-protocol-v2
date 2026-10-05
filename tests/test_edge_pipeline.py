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
        if "/v2/aggs/grouped/" in url:      # the premarket scan's prior session: nothing liquid, no error
            return Resp({"results": []})
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


def test_a_card_tick_that_reaches_issuance_after_the_open_refuses_its_forecasts(ledger):
    """EDGE-01: the tick starts at 09:27, the slow fetches run past 09:30. The forecasts must not
    be recorded with the 09:27 tick time: the trusted clock is read again at issuance, and a
    forecast whose real issuance is at/after the window open is refused (late_refused)."""
    wall = iter([ts(9, 31)] * 50)
    out = P.morning_card(FakeAlpaca(), ledger, now=ts(9, 27), clock=lambda: next(wall))
    assert out["status"] == "issued" and out["forecasts"] == [] and out["baseline_forecasts"] == []
    assert out["late_refused"] and all("09:31:00 ET" in m for m in out["late_refused"])
    assert ledger.store.scan("forecasts") == []
    abst = {a["symbol"]: a["reasons"] for a in ledger.store.scan("abstentions", experiment_id=P.EID)}
    assert abst["SHOP"] == [P.LATE_REASON]            # the name it would have chosen, refused on record
    card = ledger.store.get("edge_cards", DAY.isoformat())
    assert card["late_refused"] == out["late_refused"] and card["data_as_of"] == ts(9, 27)


def test_a_card_forecast_carries_its_real_issuance_time_and_its_data_cutoff(ledger):
    """EDGE-01: issued_at / recorded_at are the trusted clock at issuance, not the tick start;
    the tick start is kept as the data cutoff (evidence.data_as_of)."""
    wall = iter(range(ts(9, 12), ts(9, 12) + 100))
    out = P.morning_card(FakeAlpaca(), ledger, now=ts(9, 10), clock=lambda: next(wall))
    assert out["forecasts"] == ["SHOP"] and out["late_refused"] == []
    f = ledger.store.scan("forecasts", experiment_id=P.EID)[0]
    assert ts(9, 12) <= f["issued_at"] <= f["recorded_at"] < ts(9, 30)
    assert f["evidence"]["data_as_of"] == ts(9, 10)


def test_a_record_that_slips_past_the_open_is_refused_even_after_a_timely_issue(ledger):
    reads = iter([ts(9, 29)] + [ts(9, 30)] * 50)     # issued 09:29, recorded 09:30
    out = P.morning_card(FakeAlpaca(), ledger, now=ts(9, 20), clock=lambda: next(reads))
    assert out["forecasts"] == [] and out["late_refused"]
    assert ledger.store.scan("forecasts") == []


def test_the_scheduler_tick_passes_its_clock_to_the_card(ledger):
    out = P.run(FakeAlpaca(), ledger, now=ts(9, 27), clock=lambda: ts(9, 31))
    assert out["card"]["forecasts"] == [] and out["card"]["late_refused"]


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


# ------------------------------------------- miss review v2: the radar counts --

def _radar_row(sym, *, detected, history, state, blocker=None, catalyst=None):
    return {"symbol": sym, "session_date": DAY.isoformat(), "detected_at": detected, "detected_move_pct": 9.0,
            "strategy": None, "state": state, "history": history, "blocker": blocker, "catalyst": catalyst}


def _radar_forecast(ledger, sym, *, sim, actual=None):
    from edge import intraday as I
    from edge.contracts import issue_intraday
    ledger.register(I.INTRADAY_CONTINUATION, now=ts(9, 45))
    fc = issue_intraday(I.INTRADAY_CONTINUATION, symbol=sym, session_date=DAY, entry_ref=5.1, issued_at=ts(10, 0))
    ledger.record(fc, now=ts(10, 0))
    ledger.store.put("outcomes", f"{fc.forecast_id}|simulated", {"forecast_id": fc.forecast_id, "outcome": sim})
    if actual:
        ledger.store.put("outcomes", f"{fc.forecast_id}|actual", {"forecast_id": fc.forecast_id, "outcome": actual})
    ledger.store.put("edge_radar", f"{DAY.isoformat()}|{sym}", _radar_row(
        sym, detected=ts(9, 50), state="ENTRY_ELIGIBLE", history=[
            {"ts": ts(9, 50), "from": "DETECTED", "to": "WATCHING", "reason": "", "evidence": {}},
            {"ts": ts(10, 0), "from": "WATCHING", "to": "SETUP_FORMING", "reason": "", "evidence": {}},
            {"ts": ts(10, 0), "from": "SETUP_FORMING", "to": "ENTRY_ELIGIBLE", "reason": "",
             "evidence": {"forecast_id": fc.forecast_id, "price": 5.1}}]))
    return fc


def _review_v2(ledger):
    ledger.store.put("edge_universe", "2026-09-23", {"day": "2026-09-23", "symbols": ["SHOP", "GAPR", "NEWX", "USAR"]})
    P.morning_card(FakeAlpaca(), ledger, now=ts(9, 10))
    return P.miss_review(FullMarket("evening"), ledger, day=DAY, now=ts(16, 25))


def test_a_radar_only_name_that_expired_is_a_strategy_rejection_not_a_detection_failure(ledger):
    ledger.store.put("edge_radar", f"{DAY.isoformat()}|NEWX", _radar_row(
        "NEWX", detected=ts(9, 50), state="EXPIRED",
        blocker={"strategy": "intraday_continuation", "verdict": "REJECTED",
                 "reasons": ["rvol_tod 1.4 fails >= 2x"], "at": ts(11, 5)},
        history=[{"ts": ts(9, 50), "from": "DETECTED", "to": "WATCHING", "reason": "", "evidence": {}},
                 {"ts": ts(16, 25), "from": "WATCHING", "to": "EXPIRED",
                  "reason": "session ended without an eligible setup; last blocker: "
                            "intraday_continuation: rvol_tod 1.4 fails >= 2x", "evidence": {}}]))
    out = _review_v2(ledger)
    review = ledger.store.get("edge_miss", "2026-09-23")
    row = {r["symbol"]: r for r in review["rows"]}["NEWX"]
    assert row["label"] == "STRATEGY_REJECTION"
    assert row["seen_by"] == ["radar"] and row["radar_state"] == "EXPIRED" and row["radar_detected_at"] == ts(9, 50)
    assert row["why"] == "intraday_continuation: rvol_tod 1.4 fails >= 2x"
    assert review["labels_version"] == out["labels_version"] == "miss_labels_v2"
    assert "09:30 open" in review["executable_basis"] and "upper bound" in review["executable_basis"]   # U13
    assert (review["radar_seen"], review["card_seen"], review["radar_names"]) == (1, 1, 1)
    from edge import notify as N
    assert "radar saw it, rule rejected: 1" in N.misses_text(review)


def test_a_radar_forecast_that_filled_is_caught_and_counted_as_radar_recall(ledger):
    _radar_forecast(ledger, "NEWX", sim="WIN", actual="WIN")
    out = _review_v2(ledger)
    review = ledger.store.get("edge_miss", "2026-09-23")
    rows = {r["symbol"]: r for r in review["rows"]}
    assert rows["NEWX"]["label"] == "CAUGHT" and rows["NEWX"]["caught_by"] == "radar"
    assert rows["SHOP"]["label"] == "CAUGHT" and rows["SHOP"]["caught_by"] == "card"   # not graded yet: no fill verdict
    assert (out["card_recall"], out["radar_recall"], out["recall"]) == (0.5, 0.5, 1.0)
    assert review["caught_by"] == {"card": 1, "radar": 1}
    from edge import notify as N
    assert "by the radar 1/2" in N.misses_text(review)


def test_a_forecast_that_never_filled_is_an_execution_failure_not_a_catch(ledger):
    """2026-09-24 GLND: simulated and paper both NO_FILL, yet v1 counted it CAUGHT."""
    fc = _radar_forecast(ledger, "NEWX", sim="NO_FILL", actual="NO_FILL")
    _review_v2(ledger)
    review = ledger.store.get("edge_miss", "2026-09-23")
    row = {r["symbol"]: r for r in review["rows"]}["NEWX"]
    assert row["label"] == "ALERT_EXECUTION_FAILURE" and "caught_by" not in row
    assert row["forecasts"] == [{"source": "radar", "forecast_id": fc.forecast_id, "filled": False,
                                 "simulated": "NO_FILL", "actual": "NO_FILL"}]
    assert review["labels"]["ALERT_EXECUTION_FAILURE"] == 1 and review["caught"] == 1


def test_the_paper_record_overrides_a_simulated_fill(ledger):
    _radar_forecast(ledger, "NEWX", sim="WIN", actual="NO_FILL")        # the broker never filled it
    _review_v2(ledger)
    row = {r["symbol"]: r for r in ledger.store.get("edge_miss", "2026-09-23")["rows"]}["NEWX"]
    assert row["label"] == "ALERT_EXECUTION_FAILURE"


def test_a_card_forecast_that_never_filled_is_an_execution_failure(ledger):
    ledger.store.put("edge_universe", "2026-09-23", {"day": "2026-09-23", "symbols": ["SHOP", "GAPR", "NEWX", "USAR"]})
    P.morning_card(FakeAlpaca(), ledger, now=ts(9, 10))
    fid = next(r["forecast_id"] for r in ledger.store.get("edge_cards", "2026-09-23")["rows"]
               if r["symbol"] == "SHOP")
    ledger.store.put("outcomes", f"{fid}|simulated", {"forecast_id": fid, "outcome": "NO_FILL"})
    P.miss_review(FullMarket("evening"), ledger, day=DAY, now=ts(16, 25))
    row = {r["symbol"]: r for r in ledger.store.get("edge_miss", "2026-09-23")["rows"]}["SHOP"]
    assert row["label"] == "ALERT_EXECUTION_FAILURE"


def test_a_miss_with_a_stored_company_event_and_no_forecast_is_a_catalyst_miss(ledger):
    ledger.store.put("edge_research", "2026-09-23|NEWX", {
        "day": "2026-09-23", "symbol": "NEWX", "made_at": ts(8, 40), "status": "researched",
        "claims": [{"kind": "contract", "statement": "NEWX wins a $40M Army contract", "status": "verified"}]})
    _review_v2(ledger)
    row = {r["symbol"]: r for r in ledger.store.get("edge_miss", "2026-09-23")["rows"]}["NEWX"]
    assert row["label"] == "CATALYST_MISSED"
    assert row["stored_catalyst"] == "NEWX wins a $40M Army contract"


def test_a_seen_name_judged_without_its_stored_catalyst_is_a_catalyst_miss(ledger):
    ledger.store.put("edge_research", "2026-09-23|NEWX", {
        "day": "2026-09-23", "symbol": "NEWX", "made_at": ts(8, 40), "status": "researched",
        "claims": [{"kind": "fda_regulatory", "statement": "FDA clears NEWX device", "status": "single_source"}]})
    ledger.store.put("edge_radar", f"{DAY.isoformat()}|NEWX", _radar_row(
        "NEWX", detected=ts(9, 50), state="EXPIRED",
        blocker={"strategy": "catalyst_breakout", "verdict": "REJECTED",
                 "reasons": ["no dated company-specific catalyst (sector sympathy does not count)"], "at": ts(10, 0)},
        history=[{"ts": ts(9, 50), "from": "DETECTED", "to": "WATCHING", "reason": "", "evidence": {}}]))
    _review_v2(ledger)
    row = {r["symbol"]: r for r in ledger.store.get("edge_miss", "2026-09-23")["rows"]}["NEWX"]
    assert row["label"] == "CATALYST_MISSED"
    # ...but a quarantined claim is not a catalyst the store "held".
    ledger2 = Ledger(MemoryStore())
    ledger2.store.put("edge_research", "2026-09-23|NEWX", {
        "day": "2026-09-23", "symbol": "NEWX", "made_at": ts(8, 40), "status": "researched",
        "claims": [{"kind": "fda_regulatory", "statement": "FDA clears NEWX device", "status": "quarantined"}]})
    _review_v2(ledger2)
    assert {r["symbol"]: r for r in ledger2.store.get("edge_miss", "2026-09-23")["rows"]}["NEWX"]["label"] \
        == "DETECTION_FAILURE"


def test_after_the_close_every_priced_candidate_is_graded_as_a_labelled_counterfactual(ledger):
    from edge import scorecard as SC
    P.morning_card(FakeAlpaca(), ledger, now=ts(9, 10))
    assert SC.grade_card(FakeAlpaca("evening"), ledger.store, day=DAY, now=ts(16, 0))["status"] == "too_early"
    out = SC.grade_card(FakeAlpaca("evening"), ledger.store, day=DAY, now=ts(16, 25))
    assert out["status"] == "graded" and out["rows"] == 4          # STALE had no fresh price
    rec = ledger.store.get("edge_card_outcomes", DAY.isoformat())
    shop = next(r for r in rec["rows"] if r["symbol"] == "SHOP")
    assert shop["outcome"] == "WIN" and shop["auto"] == "ELIGIBLE" and "counterfactual" in rec["label"]
    # U12: every row also carries the order-as-written grade (stop-limit + 10 bps a side)
    assert shop["execution"] in ("WIN", "LOSS", "TIME_EXIT", "NO_FILL", "UNRESOLVED")
    assert "10 bps" in rec["basis"] and "execution" in out
    # never part of any experiment's record
    assert ledger.report(P.EID)["forecasts"] == 1
    # The fake's bars end at 10:20, so some rows are UNRESOLVED (data missing): a later tick
    # re-grades only those (EDGE-08); terminal rows are kept exactly as written.
    terminal = {r["symbol"]: r for r in rec["rows"] if "UNRESOLVED" not in (r["outcome"], r.get("execution"))}
    assert out["pending"] == len(rec["rows"]) - len(terminal) > 0
    again = SC.grade_card(FakeAlpaca("evening"), ledger.store, day=DAY, now=ts(16, 30))
    assert again["status"] == "graded" and again["regraded"] == again["pending"] == out["pending"]
    after = ledger.store.get("edge_card_outcomes", DAY.isoformat())
    assert {r["symbol"]: r for r in after["rows"] if r["symbol"] in terminal} == terminal
    assert after["graded_at"] == ts(16, 25) and after["updated_at"] == ts(16, 30)


def test_the_card_grade_retries_unresolved_rows_and_stops_once_all_are_terminal(ledger):
    """EDGE-08: a day row with UNRESOLVED rows is not 'already_graded'; once every row is
    terminal it is, and nothing is fetched again."""
    from edge import scorecard as SC
    P.morning_card(FakeAlpaca(), ledger, now=ts(9, 10))

    class Truncated(FakeAlpaca):          # minute bars missing entirely on the first pass
        def __call__(self, url, params=None, headers=None, timeout=None):
            if url.endswith("/v2/stocks/bars") and (params or {}).get("timeframe") == "1Min":
                return Resp({"bars": {}})
            return super().__call__(url, params, headers, timeout)

    first = SC.grade_card(Truncated("evening"), ledger.store, day=DAY, now=ts(16, 25))
    assert first["status"] == "graded" and first["pending"] == first["rows"] == 4

    class Whole(FakeAlpaca):              # the whole session, through the 15:30 time exit
        def __call__(self, url, params=None, headers=None, timeout=None):
            if url.endswith("/v2/stocks/bars") and (params or {}).get("timeframe") == "1Min":
                out = {}
                for s in params["symbols"].split(","):
                    px = PRE.get(s, 10.0)
                    out[s] = [{"t": iso(t), "o": px, "h": px, "l": px, "c": px, "v": 100}
                              for t in range(ts(9, 30), ts(15, 35), 300)]
                return Resp({"bars": out})
            return super().__call__(url, params, headers, timeout)

    second = SC.grade_card(Whole("evening"), ledger.store, day=DAY, now=ts(16, 30))
    assert second["status"] == "graded" and second["pending"] == 0 and second["regraded"] == 4
    rows = ledger.store.get("edge_card_outcomes", DAY.isoformat())["rows"]
    assert all(r["outcome"] != "UNRESOLVED" and r["execution"] != "UNRESOLVED" for r in rows)
    assert all(r["resolver_version"] == "resolver_v2" for r in rows)
    third = Whole("evening")
    assert SC.grade_card(third, ledger.store, day=DAY, now=ts(16, 35)) == {"status": "already_graded",
                                                                           "pending": 0}
    assert third.calls == []


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


def test_an_empty_research_queue_is_retried_not_cached_for_the_day(ledger, monkeypatch):
    monkeypatch.setenv("EDGE_RESEARCH_ENABLED", "1")
    calls = {"n": 0}

    class NoFreshPrice(FakeAlpaca):
        def __call__(self, url, params=None, headers=None, timeout=None):
            if "/v2/stocks/snapshots" in url:
                if (params or {}).get("feed") == "iex":      # not the once-a-day SIP entitlement check
                    calls["n"] += 1
                return Resp({})                      # 08:30 ET: IEX has printed nothing yet
            return super().__call__(url, params=params, headers=headers, timeout=timeout)

    out = P.research_step(NoFreshPrice(), ledger, day=DAY, now=ts(8, 30), client=object())
    assert out["status"] == "no_candidates_yet"
    assert ledger.store.get("edge_research_queue", DAY.isoformat()) is None
    P.research_step(NoFreshPrice(), ledger, day=DAY, now=ts(8, 35), client=object())
    assert calls["n"] == 2                            # recomputed, not frozen empty for the day


def test_the_card_stores_its_top_10_and_the_evening_grades_it(ledger):
    P.morning_card(FakeAlpaca(), ledger, now=ts(9, 10))
    card = ledger.store.get("edge_cards", "2026-03-18") or next(iter(ledger.store.scan("edge_cards")))
    t10 = ledger.store.get("edge_top10", card["day"])
    assert t10 and card["top10"] == [x["symbol"] for x in t10["list"]] and len(card["top10"]) <= 10
    P.run(FakeAlpaca("evening"), ledger, now=ts(16, 25))
    from edge import readout as R
    assert R.view(ledger.store, "top10", card["day"])["graded"] is True


# ------------------------------------ one bad data minute must not cost the day (audit 2026-09-25) --

class RateLimited(FakeAlpaca):
    """Alpaca answers 429 on the card's price snapshots while `failing` is set."""

    def __init__(self, failing=True, **kw):
        super().__init__(**kw)
        self.failing = failing

    def __call__(self, url, params=None, headers=None, timeout=None):
        if self.failing and "/v2/stocks/snapshots" in url and (params or {}).get("feed") != "sip":
            raise RuntimeError("429 Client Error: Too Many Requests")
        return super().__call__(url, params=params, headers=headers, timeout=timeout)


def test_a_failed_first_card_tick_stores_nothing_and_the_next_tick_issues_once(ledger):
    out = P.morning_card(RateLimited(), ledger, now=ts(9, 5))
    assert out["status"] == "retry_pending" and any("snapshots_iex" in f for f in out["failures"])
    assert ledger.store.get("edge_cards", DAY.isoformat()) is None
    assert ledger.store.scan("forecasts", experiment_id=P.EID) == []
    assert ledger.store.scan("abstentions", experiment_id=P.EID) == []
    out = P.morning_card(RateLimited(failing=False), ledger, now=ts(9, 10))
    assert out["status"] == "issued" and out["forecasts"] == ["SHOP"] and out["source_errors"] == {}
    card = ledger.store.get("edge_cards", DAY.isoformat())
    assert card["degraded"] is False and card["failed_attempts"] == 1
    assert P.morning_card(FakeAlpaca(), ledger, now=ts(9, 15))["status"] == "already_issued"
    assert len(ledger.store.scan("forecasts", experiment_id=P.EID)) == 1


def test_errors_until_the_last_tick_issue_a_degraded_card_that_names_the_failed_source(ledger):
    for hm in ((9, 5), (9, 10), (9, 15), (9, 20)):
        assert P.morning_card(RateLimited(), ledger, now=ts(*hm))["status"] == "retry_pending"
    out = P.morning_card(RateLimited(), ledger, now=ts(9, 25))
    assert out["status"] == "issued" and "snapshots_iex" in out["source_errors"]
    card = ledger.store.get("edge_cards", DAY.isoformat())
    assert card["degraded"] is True and card["failed_attempts"] == 4
    assert any("snapshots_iex" in f for f in card["data_failures"])
    assert card["forecasts"] == [] and card["priced"] == 0          # nothing priced: nothing guessed
    from edge import notify as N
    first = N.card_text(card).splitlines()[0]
    assert first.startswith("DATA WARNING:") and "snapshots_iex" in first and "last try" in first


def test_a_failed_daily_bars_call_is_named_and_retried_not_a_crash(ledger):
    class NoDaily(FakeAlpaca):
        def __call__(self, url, params=None, headers=None, timeout=None):
            if url.endswith("/v2/stocks/bars") and (params or {}).get("timeframe") == "1Day":
                raise RuntimeError("ReadTimeout")
            return super().__call__(url, params=params, headers=headers, timeout=timeout)

    out = P.morning_card(NoDaily(), ledger, now=ts(9, 5))
    assert out["status"] == "retry_pending" and "daily_bars" in out["source_errors"]
    assert ledger.store.get("edge_cards", DAY.isoformat()) is None


def test_no_price_for_any_candidate_is_retried_but_a_thin_valid_card_is_not(ledger):
    class NoPrints(FakeAlpaca):
        def __call__(self, url, params=None, headers=None, timeout=None):
            if "/v2/stocks/snapshots" in url and (params or {}).get("feed") != "sip":
                return Resp({})                                # a 200 with nothing in it
            return super().__call__(url, params=params, headers=headers, timeout=timeout)

    out = P.morning_card(NoPrints(), ledger, now=ts(9, 5))
    assert out["status"] == "retry_pending" and "no premarket price for any of 5 candidates" in out["failures"]
    # STALE's last print is yesterday's: 4/5 priced, no error -- issued on the first tick, as before
    other = Ledger(MemoryStore())
    out = P.morning_card(FakeAlpaca(), other, now=ts(9, 5))
    assert out["status"] == "issued" and out["priced"] == 4 and out["candidates"] == 5
    assert other.store.get("edge_cards", DAY.isoformat())["degraded"] is False


def test_a_retrying_card_tick_is_logged():
    assert P.noteworthy({"day": "2026-09-23", "card": {"status": "retry_pending"}})


def test_a_scan_with_failed_batches_is_not_reused_from_cache():
    from edge import premarket as PM
    store = MemoryStore()
    store.put("edge_pm_scan", DAY.isoformat(), {"day": DAY.isoformat(), "at": ts(9, 4), "batch_errors": 3,
                                                "gainers": [], "scanned": 300, "priced": 0})
    g = FakeAlpaca()
    PM.scan(g, store, day=DAY, now=ts(9, 5))
    assert any("/v2/aggs/grouped/" in u for u, _ in g.calls)       # recomputed, not the broken cache


# ---- research window: 08:30 up to the card deadline (09:28 ET) --------------------------------

@pytest.mark.parametrize("hhmm,runs", [((8, 25), False), ((8, 30), True), ((9, 0), True), ((9, 4), True),
                                       ((9, 10), True), ((9, 27), True), ((9, 28), False)])
def test_research_runs_until_the_card_deadline_not_9am(ledger, monkeypatch, hhmm, runs):
    """It used to stop at 09:00 while cards are built 09:05-09:28: a mover qualifying later got no review."""
    monkeypatch.setenv("EDGE_RESEARCH_ENABLED", "1")
    order = []
    monkeypatch.setattr(P, "research_step", lambda *a, **k: order.append("research") or {"status": "nothing"})
    real_card = P.morning_card
    monkeypatch.setattr(P, "morning_card", lambda *a, **k: order.append("card") or real_card(*a, **k))
    P.run(FakeAlpaca(), ledger, now=ts(*hhmm))
    assert ("research" in order) is runs
    if runs and "card" in order:
        assert order.index("card") < order.index("research")      # a slow call cannot crowd the card
    assert P.RESEARCH_WINDOW[1] == P.CARD_WINDOW[1] == (9, 28)


def test_research_is_stamped_with_the_real_clock_not_the_tick_start(ledger, monkeypatch):
    """Research runs after the card inside a tick: it must be timed on the trusted clock, or research that
    finished after a card was issued could look like it finished before (verdict() compares the two)."""
    monkeypatch.setenv("EDGE_RESEARCH_ENABLED", "1")
    seen = {}
    monkeypatch.setattr(P, "research_step", lambda *a, **k: seen.update(now=k["now"]) or {"status": "nothing"})
    P.run(FakeAlpaca(), ledger, now=ts(9, 10), clock=lambda: ts(9, 12))
    assert seen["now"] == ts(9, 12)


def test_the_research_queue_takes_in_movers_that_qualify_later(ledger, monkeypatch):
    rankings = {"now": ["SHOP"]}
    monkeypatch.setattr(P, "_rank_research_candidates", lambda *a, **k: list(rankings["now"]))
    assert P.research_candidates(None, ledger.store, day=DAY, now=ts(8, 30), n=5) == ["SHOP"]
    rankings["now"] = ["LATE", "SHOP"]
    # Inside the refresh interval the cached queue stands ...
    assert P.research_candidates(None, ledger.store, day=DAY, now=ts(8, 35), n=5) == ["SHOP"]
    # ... after it, a mover that qualified later joins (first), and queued names are kept.
    assert P.research_candidates(None, ledger.store, day=DAY, now=ts(9, 10), n=5) == ["LATE", "SHOP"]
    rankings["now"] = ["NEWER"]
    assert P.research_candidates(None, ledger.store, day=DAY, now=ts(9, 20), n=5) == ["NEWER", "LATE", "SHOP"]


def test_a_failed_queue_refresh_keeps_the_queue_it_had(ledger, monkeypatch):
    monkeypatch.setattr(P, "_rank_research_candidates", lambda *a, **k: ["SHOP"])
    P.research_candidates(None, ledger.store, day=DAY, now=ts(8, 30), n=5)

    def boom(*a, **k):
        raise RuntimeError("snapshots down")
    monkeypatch.setattr(P, "_rank_research_candidates", boom)
    assert P.research_candidates(None, ledger.store, day=DAY, now=ts(9, 0), n=5) == ["SHOP"]


def test_research_written_after_the_card_never_reaches_it_and_reads_not_researched(ledger, monkeypatch):
    monkeypatch.setenv("EDGE_RESEARCH_ENABLED", "1")
    claim = {"kind": "contract", "statement": "Shopify x Meta checkout", "status": "verified", "problems": []}
    review = {"entity_ok": True, "contradictions": [], "dilution_found": False, "stale": False, "notes": ""}
    # Started 09:04, before the 09:05 card -- but only WRITTEN at 09:07.
    ledger.store.put("edge_research", "2026-09-23|SHOP", {
        "day": "2026-09-23", "symbol": "SHOP", "made_at": ts(9, 4), "finished_at": ts(9, 7),
        "status": "researched", "claims": [claim], "review": review})
    P.morning_card(FakeAlpaca(), ledger, now=ts(9, 5))
    row = {r["symbol"]: r for r in ledger.store.get("edge_cards", DAY.isoformat())["rows"]}["SHOP"]
    assert row["research_status"] == "not_researched"
    assert row["verified_verdict"] != "ELIGIBLE"
    # The same record, finished before 09:05 ET (frozen v1: "made before 09:05 ET"), is used.
    ledger2 = Ledger(MemoryStore())
    ledger2.store.put("edge_research", "2026-09-23|SHOP", {
        "day": "2026-09-23", "symbol": "SHOP", "made_at": ts(9, 3), "finished_at": ts(9, 4),
        "status": "researched", "claims": [claim], "review": review})
    P.morning_card(FakeAlpaca(), ledger2, now=ts(9, 10))
    row2 = {r["symbol"]: r for r in ledger2.store.get("edge_cards", DAY.isoformat())["rows"]}["SHOP"]
    assert row2["research_status"] == "researched"


# ---- frozen specs: the fingerprints never move; a refused spec never takes the card down ----------

FROZEN_HASHES = {   # spec_hash() of every frozen experiment as registered in production (7a1edea)
    "catalyst_breakout@v1": "e2f4657bada0a994d115a4f355c89ac096466ec50b9371633400ccf289dcc372",
    "crowded_short_ignition@v1": "42240af1bb9b8f4a510b84f5d911b800d423eda0daea25789a9e0065b61c1299",
    "gap_and_go@v1": "6b99e5306855af2f6dbd40bd8df069bbf7d2131f4c05d8435c7eb2b080b11cc0",
    "gap_and_go_auto@v1": "b2e4106e868a14e5651b96b6bd890a97aabbd328425b87ba0214a5032d76ae20",
    "gap_and_go_sipref@v1": "3c495b065ceeb7f68a5b18438b7315ef4445e0aeba48a1041fcb41ba339744fd",
    "gap_and_go_verified@v1": "01ce0bde8477be8b2044c724626de71378b0b3a65434a8592ba8f12fd328682f",
    "gap_baseline@v1": "038b5e8aca95950e33bbd7ecc5a01eb1cfa797db687889c5de677aef6f20c014",
    "intraday_continuation@v1": "de33cf2976757efc0275f80d029f8b453f4af886c0058c33f38f504c11a7a527",
    "intraday_continuation@v2": "c109c89824413df2ed9cdba804978ac264bfc64e07b990d1951a8157b760a118",
    "short_interest_ignition@v1": "adf8c21524fd43be9b7cc5c99d3439d5aecbed0d36943d095a092ff7475f25f9",
}


def test_no_frozen_experiment_spec_changes_its_fingerprint():
    """2026-10-02: an edited eligibility sentence changed gap_and_go_verified@v1's hash; the
    ledger refused it in production and the whole morning card failed. Any edit to a frozen spec
    is a new version (@v2), never an in-place change."""
    from edge import intraday as I
    from edge.contracts import ExperimentSpec
    specs = {v.experiment_id: v.spec_hash() for m in (P, I) for v in vars(m).values()
             if isinstance(v, ExperimentSpec)}
    for eid, h in FROZEN_HASHES.items():
        assert specs.get(eid) == h, f"{eid} changed: register the change as a new version"


def test_a_refused_frozen_spec_skips_only_its_own_experiment(ledger, monkeypatch, caplog):
    from dataclasses import replace
    monkeypatch.setenv("EDGE_RESEARCH_ENABLED", "1")
    monkeypatch.setattr(P._research(), "verdict", lambda *a, **k: {"catalyst": None, "dilutive": None})
    # The verified experiment is already registered under a different hash (as if its spec moved).
    ledger.register(replace(P.GAP_VERIFIED, description="an older wording"), now=ts(8, 0))
    out = P.morning_card(FakeAlpaca(), ledger, now=ts(9, 10))
    assert out["status"] == "issued" and out["forecasts"] == ["SHOP"]     # the card still issues
    card = ledger.store.get("edge_cards", DAY.isoformat())
    assert P.VERIFIED_EID in card["refused_specs"]
    assert not ledger.store.scan("forecasts", experiment_id=P.VERIFIED_EID)


def test_v1_verified_uses_only_research_made_before_0905(ledger, monkeypatch):
    """Research now runs to the card deadline, but gap_and_go_verified@v1 is frozen as
    'research made before 09:05 ET': the verdict cutoff is capped at 09:05."""
    monkeypatch.setenv("EDGE_RESEARCH_ENABLED", "1")
    seen = []
    monkeypatch.setattr(P._research(), "verdict",
                        lambda store, **k: seen.append(k["issued_at"]) or {"catalyst": None, "dilutive": None})
    P.morning_card(FakeAlpaca(), ledger, now=ts(9, 20), clock=lambda: ts(9, 21))
    assert seen and max(seen) == ts(9, 5)


def test_upgrade_over_production_registrations_issues_the_card(ledger, monkeypatch):
    """Audit NEW-01: fresh-store tests never exercised an upgrade. Seed the store with the
    experiment rows exactly as production holds them (hashes registered before this release);
    the new code must register over them without a FrozenSpecError and issue the card."""
    monkeypatch.setenv("EDGE_RESEARCH_ENABLED", "1")
    monkeypatch.setattr(P._research(), "verdict", lambda *a, **k: {"catalyst": None, "dilutive": None})
    for spec in P.EXPERIMENTS:
        ledger.store.put("experiments", spec.experiment_id, {
            "experiment_id": spec.experiment_id, "spec_hash": FROZEN_HASHES[spec.experiment_id],
            "spec": {}, "registered_at": ts(8, 0, date(2026, 9, 22)), "status": "active"})
    out = P.morning_card(FakeAlpaca(), ledger, now=ts(9, 10))
    assert out["status"] == "issued" and out["forecasts"] == ["SHOP"]
    assert ledger.store.get("edge_cards", DAY.isoformat())["refused_specs"] == {}


class CappedNews(FakeAlpaca):
    """Task #59: every news page answers with a next_page_token -- the page cap is always hit."""

    def __call__(self, url, params=None, headers=None, timeout=None):
        r = super().__call__(url, params=params, headers=headers, timeout=timeout)
        if "/v1beta1/news" in url:
            r._p = {**r._p, "next_page_token": "more"}
        return r


def test_a_page_capped_news_answer_leaves_the_card_catalyst_unknown_never_none(ledger):
    """A capped answer lost the oldest releases; "no dated company-specific catalyst" would be a
    guess. Those names read catalyst UNKNOWN (missing), never FAIL."""
    out = P.morning_card(CappedNews(), ledger, now=ts(9, 10))
    assert out["status"] == "issued" and out["forecasts"] == []
    abst = {a["symbol"]: a for a in ledger.store.scan("abstentions", experiment_id=P.EID)}
    assert not any("sector sympathy" in r for r in abst["USAR"]["reasons"])
    assert "catalyst: catalyst feed" in abst["USAR"]["reasons"]          # unknown, not "none"


# ---- gap_and_go_sipref@v1: IEX blind spot, priced from 15-min-delayed SIP (operator 2026-10-05) ----

class SipFake(FakeAlpaca):
    """STALE has a dated catalyst but its last IEX print is from the prior session; the full-market
    feed, 15 minutes delayed, has it at $22.00 (+10%) in the 08:50-08:55 ET bar."""

    def __init__(self, sip_ts=None):
        super().__init__("morning")
        self.sip_ts = sip_ts if sip_ts is not None else ts(8, 50)

    def __call__(self, url, params=None, headers=None, timeout=None):
        params = params or {}
        if "/v2/aggs/grouped/" in url:
            self.calls.append((url, dict(params)))
            return Resp({"results": [{"T": "STALE", "c": 20.0, "v": 2_000_000}]})
        if url.endswith("/v2/stocks/bars") and params.get("timeframe") == "5Min":
            self.calls.append((url, dict(params)))
            assert params["feed"] == "sip"
            return Resp({"bars": {"STALE": [{"t": iso(self.sip_ts), "c": 22.0, "v": 40_000}]}})
        return super().__call__(url, params, headers, timeout)


def test_sipref_forecasts_a_gapper_iex_cannot_price_and_auto_still_does_not(ledger):
    P.morning_card(SipFake(), ledger, now=ts(9, 10))
    card = ledger.store.get("edge_cards", DAY.isoformat())
    assert card["forecasts"] == ["SHOP"]                          # gap_and_go_auto@v1: unchanged, IEX only
    assert sorted(card["sipref_forecasts"]) == ["SHOP", "STALE"]  # the new version also sees STALE
    f = {x["symbol"]: x for x in ledger.store.scan("forecasts", experiment_id=P.SIPREF_EID)}
    assert f["STALE"]["entry_ref"] == 22.0 and f["SHOP"]["entry_ref"] == PRE["SHOP"]
    assert f["STALE"]["evidence"]["sipref_source"] == "sip_delayed"
    assert f["STALE"]["evidence"]["ref_ts"] == ts(8, 54)          # bar end 08:55, capped at the readable end (now-16 min)
    assert f["SHOP"]["evidence"]["sipref_source"] == "iex"
    row = {r["symbol"]: r for r in card["rows"]}["STALE"]
    assert row["verdict"] != "ELIGIBLE" and row["sipref_verdict"] == "ELIGIBLE"


def test_sipref_never_uses_a_delayed_price_older_than_30_minutes(ledger):
    P.morning_card(SipFake(sip_ts=ts(8, 30)), ledger, now=ts(9, 10))   # bar ends 08:35: 35 min old
    card = ledger.store.get("edge_cards", DAY.isoformat())
    assert card["sipref_forecasts"] == ["SHOP"]
    abst = {a["symbol"]: a["reasons"] for a in ledger.store.scan("abstentions", experiment_id=P.SIPREF_EID)}
    assert "STALE" in abst
