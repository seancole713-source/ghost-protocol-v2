"""Today's premarket gainers from the market itself -- not a stale screener."""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

from edge import catalysts as C
from edge import pipeline as P
from edge import premarket as PM
from edge.ledger import MemoryStore

ET = ZoneInfo("America/New_York")
DAY = date(2026, 9, 23)


def ts(hh, mm):
    return int(datetime(2026, 9, 23, hh, mm, tzinfo=ET).timestamp())


def iso(t):
    return datetime.fromtimestamp(t, tz=ET).isoformat()


class Market:
    """Polygon grouped daily for the prior session + Alpaca IEX snapshots + a STALE screener."""

    def __init__(self):
        self.snap_calls = []

    def __call__(self, url, params=None, headers=None, timeout=None):
        params = params or {}
        if "/v2/aggs/grouped/" in url:
            assert "2026-09-22" in url                        # the PRIOR session
            return NS(status_code=200, json=lambda: {"results": [
                {"T": "WOR", "c": 58.95, "v": 900_000},        # $53M: liquid
                {"T": "IONQ", "c": 40.73, "v": 30_000_000},
                {"T": "FLAT", "c": 20.0, "v": 1_000_000},
                {"T": "PENY", "c": 0.80, "v": 90_000_000},     # under $2: pre-filtered out
                {"T": "THIN", "c": 10.0, "v": 10_000},         # $100k: pre-filtered out
                {"T": "ABCDW", "c": 3.0, "v": 5_000_000},      # a warrant
                {"T": "OLD", "c": 10.0, "v": 1_000_000},
            ]}, raise_for_status=lambda: None, headers={})
        if "/v2/stocks/snapshots" in url:
            syms = params["symbols"].split(",")
            self.snap_calls.append(syms)
            prints = {"WOR": (68.33, ts(8, 50)), "IONQ": (45.60, ts(8, 55)), "FLAT": (20.1, ts(8, 58)),
                      "OLD": (15.0, ts(16, 0) - 86_400)}      # yesterday's print: never a gap
            return NS(status_code=200, json=lambda: {s: {"latestTrade": {"p": p, "t": iso(t)}}
                                                     for s, (p, t) in prints.items() if s in syms},
                      raise_for_status=lambda: None)
        if "screener/stocks/movers" in url:
            return NS(status_code=200, json=lambda: {"gainers": [{"symbol": "JAGX"}, {"symbol": "BEATW"}],
                                                     "losers": [], "last_updated": iso(ts(16, 0) - 86_400)},
                      raise_for_status=lambda: None)
        raise AssertionError(url)


def test_the_scan_finds_todays_gappers_from_current_session_prints_only():
    store = MemoryStore()
    out = PM.scan(Market(), store, day=DAY, now=ts(9, 5))
    assert [(g["symbol"], round(g["gap_pct"])) for g in out["gainers"]] == [("WOR", 16), ("IONQ", 12)]
    assert out["scanned"] == 4 and out["priced"] == 3          # WOR, IONQ, FLAT printed today; OLD did not
    assert store.get("edge_pm_base", "2026-09-23")["prior_session"] == "2026-09-22"


def test_the_scan_is_cached_four_minutes_and_batched():
    m, store = Market(), MemoryStore()
    PM.scan(m, store, day=DAY, now=ts(9, 5))
    PM.scan(m, store, day=DAY, now=ts(9, 8))
    assert len(m.snap_calls) == 1
    PM.scan(m, store, day=DAY, now=ts(9, 10))
    assert len(m.snap_calls) == 2 and all(len(c) <= PM.BATCH for c in m.snap_calls)


def test_candidates_put_todays_gappers_first_and_drop_warrants_and_say_the_screener_is_stale():
    out = PM.candidates(Market(), MemoryStore(), day=DAY, now=ts(9, 5))
    assert out["symbols"][:2] == ["WOR", "IONQ"] and "BEATW" not in out["symbols"]
    assert out["movers_stale"] is True and "BEATW" in out["dropped_non_common"]


def test_common_stock_filter():
    assert PM.common_stock("WOR") and PM.common_stock("IONQ") and PM.common_stock("GOOGL")
    assert not PM.common_stock("BEATW") and not PM.common_stock("AACPR")
    assert not PM.common_stock("SLND.WS") and not PM.common_stock("NMCO.RT")
    assert PM.common_stock("ABCDE", {"ABCDE"}) and not PM.common_stock("WOR", {"IONQ"})


def test_a_market_wrap_tagged_to_many_tickers_is_not_a_company_catalyst():
    items = [{"headline": "Crude Oil Down Over 1%; Thor Industries Shares Gain After Q4 Results",
              "created_at": iso(ts(8, 0)), "symbols": ["DCOY", "VKTX", "QNME", "THO", "SPY"], "source": "benzinga"},
             {"headline": "Worthington Enterprises reports fiscal Q1 results, beats estimates",
              "created_at": iso(ts(8, 0)), "symbols": ["WOR"], "source": "benzinga"}]
    ev = P._events(items, {"DCOY", "WOR"}, ts(9, 5))
    assert ev["DCOY"] == [] and len(ev["WOR"]) == 1
    assert C.MAX_STORY_TICKERS == 3


def test_the_probe_reports_todays_top_gappers(monkeypatch):
    from edge.providers import base as B
    monkeypatch.setattr(B, "et_today", lambda: DAY)
    import time
    monkeypatch.setattr(time, "time", lambda: ts(9, 5))
    p = PM.probe(Market())
    assert p.status == "OK" and p.rows == 3 and "WOR +15.9%" in p.note


def test_the_today_view_shows_what_the_premarket_scan_saw():
    """2026-09-24: the card missed BB and GRAL, and the view could not say whether the
    scan never saw them or saw and dropped them. The card already stores the scan's
    counts and top gappers; the view must show them."""
    from edge import readout as R
    store = MemoryStore()
    store.put("edge_cards", "2026-09-24", {
        "day": "2026-09-24", "forecasts": ["GLND"], "rows": [],
        "premarket_scan": {"scanned": 5301, "priced": 212, "batch_errors": 0},
        "premarket_scan_top": [{"symbol": "PFSA", "gap_pct": 74.6}],
        "movers_stale": False, "source_errors": {}})
    out = R.today(store, "2026-09-24")
    assert out["premarket_scan"] == {"scanned": 5301, "priced": 212, "batch_errors": 0}
    assert out["premarket_scan_top"][0]["symbol"] == "PFSA"
    assert out["movers_stale"] is False and out["source_errors"] == {}
