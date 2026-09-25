"""Intraday radar: strategies that act during the session, in shadow."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from edge import intraday as I, radar as R
from edge.contracts import ContractError, issue_intraday
from edge.ledger import Ledger, MemoryStore

ET = ZoneInfo("America/New_York")
DAY = date(2026, 9, 23)


def ts(hh, mm, d=DAY):
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


def today_bars(sym, until):
    out, t, i = [], ts(9, 30), 0
    while t < until:
        if sym == "CATX":
            c = 10 + 0.0004 * i * i            # steady climb through the opening range
            v = 400
        elif sym == "CONT":                    # genuinely accelerating into 10:40
            c = 10 + 0.002 * i + (0.05 * (i - 60) ** 2 if i > 60 else 0)
            v = 400
        elif sym == "QUIET":
            c, v = 10 + 0.001 * i, 100
        else:                                  # STALE: prints stop at 10:00
            if t >= ts(10, 0):
                break
            c, v = 10.0, 400
        o = out[-1]["c"] if out else c
        out.append({"t": iso(t), "o": o, "h": max(o, c) + 0.005, "l": min(o, c) - 0.005, "c": c, "v": v})
        t, i = t + 60, i + 1
    return out


def history_bars(sym):
    out, d = [], DAY - timedelta(days=16)
    while d < DAY:
        if d.weekday() < 5:
            t = ts(9, 30, d)
            for _ in range(390):
                out.append({"t": iso(t), "o": 10, "h": 10, "l": 10, "c": 10, "v": 100})
                t += 60
        d += timedelta(days=1)
    return out


class Market:
    def __init__(self, now):
        self.now = now

    def __call__(self, url, params=None, headers=None, timeout=None):
        params = params or {}
        if "screener/stocks/movers" in url:
            return Resp({"gainers": [{"symbol": s, "percent_change": 12.0} for s in ("CATX", "CONT", "QUIET", "STALE")]})
        if url.endswith("/v2/stocks/bars") and params.get("timeframe") == "1Day":
            bars = {}
            for s in params["symbols"].split(","):
                d, rows = DAY - timedelta(days=30), []
                while d < DAY:
                    if d.weekday() < 5:
                        rows.append({"t": iso(ts(4, 0, d)), "o": 10, "h": 10, "l": 10, "c": 10, "v": 1_000_000})
                    d += timedelta(days=1)
                bars[s] = rows
            return Resp({"bars": bars})
        if url.endswith("/v2/stocks/bars") and params.get("timeframe") == "1Min":
            syms = params["symbols"].split(",")
            if params["start"].startswith(DAY.isoformat()):
                return Resp({"bars": {s: today_bars(s, self.now) for s in syms}})
            return Resp({"bars": {s: history_bars(s) for s in syms}})
        if "/v1beta1/news" in url:
            return Resp({"news": [{"headline": "CATX wins contract award from Navy", "created_at": iso(ts(8, 0)),
                                   "symbols": ["CATX"], "source": "x", "url": "u"}]})
        raise AssertionError(url)


@pytest.fixture
def ledger():
    return Ledger(MemoryStore())


def test_each_strategy_fires_on_its_own_evidence(ledger):
    out = I.tick(Market(ts(10, 40)), ledger, now=ts(10, 40))
    assert set(out["issued"]) == {"catalyst_breakout@v1:CATX", "intraday_continuation@v1:CONT",
                                  "intraday_continuation@v2:CONT"}
    f = ledger.store.scan("forecasts", experiment_id="catalyst_breakout@v1")[0]
    assert f["evidence"]["feed"] == "iex" and f["evidence"]["rvol"] == pytest.approx(4.0)
    assert f["window_start"] == ts(10, 41) and f["entry_expiry"] == ts(11, 1)


def test_the_radar_remembers_every_name_and_why(ledger):
    I.tick(Market(ts(10, 40)), ledger, now=ts(10, 40))
    radar = {r["symbol"]: r for r in ledger.store.scan("edge_radar", session_date="2026-09-23")}
    assert radar["CATX"]["state"] == R.ENTRY_ELIGIBLE
    assert radar["STALE"]["state"] == R.DATA_UNAVAILABLE
    assert radar["QUIET"]["state"] in (R.WATCHING, R.SETUP_FORMING)
    I.close_day(ledger, day=DAY, now=ts(16, 25))
    radar = {r["symbol"]: r for r in ledger.store.scan("edge_radar", session_date="2026-09-23")}
    assert radar["QUIET"]["state"] == R.EXPIRED
    # The expiry carries the name's own last blocker, not one line for every name.
    reason = radar["QUIET"]["history"][-1]["reason"]
    assert reason.startswith("session ended without an eligible setup; last blocker: ")
    assert "rvol_tod 1.0 fails" in reason
    assert radar["STALE"]["history"][-1]["reason"] == (
        "session ended without an eligible setup; last blocker: data unavailable "
        "(no IEX print in the last 5 minutes)")


def test_the_radar_keeps_why_a_name_was_not_eligible_and_when(ledger):
    I.tick(Market(ts(10, 40)), ledger, now=ts(10, 40))
    q = ledger.store.get("edge_radar", "2026-09-23|QUIET")
    b = q["blocker"]
    assert b["at"] == ts(10, 40) and b["verdict"] == "REJECTED" and b["reasons"]
    assert any("rvol_tod" in r for r in b["reasons"])
    I.tick(Market(ts(10, 45)), ledger, now=ts(10, 45))
    q2 = ledger.store.get("edge_radar", "2026-09-23|QUIET")
    if q2["blocker"]["reasons"] == b["reasons"]:
        assert q2["blocker"]["at"] == ts(10, 40)          # unchanged reasons keep the time they were set
    # A forecast name carries no blocker, and the radar saw CATX's catalyst.
    catx = ledger.store.get("edge_radar", "2026-09-23|CATX")
    assert catx["blocker"] is None and catx["catalyst"]["kind"] == "contract"
    from edge import readout as RO
    items = {i["symbol"]: i for i in RO.radar(ledger.store, "2026-09-23")["items"]}
    assert items["QUIET"]["detected_at"] == ts(10, 40)
    assert (items["QUIET"]["detected_at_et"], items["QUIET"]["detected_at_ct"]) == ("10:40 ET", "09:40 CT")
    assert items["QUIET"]["last_reasons"] == q2["blocker"]["reasons"]
    assert items["QUIET"]["last_reasons_at_et"] and items["QUIET"]["last_reasons_at_ct"]


def test_a_blocker_keeps_the_time_it_was_set_until_it_changes():
    it = R.RadarItem("X", "2026-09-23", ts(10, 0), 8.0)
    assert it.set_blocker(strategy="intraday_continuation", verdict="REJECTED", reasons=["a"], ts=ts(10, 0))
    assert not it.set_blocker(strategy="intraday_continuation", verdict="REJECTED", reasons=["a"], ts=ts(10, 5))
    assert it.blocker["at"] == ts(10, 0)
    assert it.set_blocker(strategy="intraday_continuation", verdict="REJECTED", reasons=["b"], ts=ts(10, 10))
    assert it.blocker == {"strategy": "intraday_continuation", "verdict": "REJECTED", "reasons": ["b"],
                          "at": ts(10, 10)}
    assert it.blocker_text() == "intraday_continuation: b"
    back = R.RadarItem.from_row(__import__("dataclasses").asdict(it))
    assert back.blocker == it.blocker and back.state == R.DETECTED


def test_a_second_tick_does_not_double_count(ledger):
    I.tick(Market(ts(10, 40)), ledger, now=ts(10, 40))
    out = I.tick(Market(ts(10, 45)), ledger, now=ts(10, 45))
    assert out["issued"] == []
    assert len(ledger.store.scan("forecasts", experiment_id="catalyst_breakout@v1")) == 1


def test_outside_the_window_nothing_happens(ledger):
    assert I.tick(Market(ts(9, 40)), ledger, now=ts(9, 40))["status"] == "outside_intraday_window"
    assert I.tick(Market(ts(14, 35)), ledger, now=ts(14, 35))["status"] == "outside_intraday_window"


def test_too_late_for_an_entry_window_is_refused():
    with pytest.raises(ContractError):
        issue_intraday(I.CATALYST_BREAKOUT, symbol="X", session_date=DAY, entry_ref=10.0, issued_at=ts(15, 15))


def test_both_intraday_strategies_are_labelled_hypotheses():
    for spec in I.INTRADAY_SPECS:
        assert spec.eligibility["status"] == "UNVALIDATED v0 hypothesis"
        assert spec.eligibility["feed"].startswith("IEX")


def test_continuation_v2_vetoes_a_stock_that_just_sold_shares():
    """2026-09-24: v1 bought GRML the morning after its $12 registered direct offering and was
    stopped out. v2 is v1 plus the E5 dilution veto; v1 keeps running unchanged beside it."""
    from edge import catalysts as C, detectors as D, setups as S
    ok = {k: D.Signal(k, D.PASS) for k in ("liquidity", "vwap_hold", "rvol_tod", "acceleration")}
    offer = [C.make("GRML", "Greenland Mines Completes $12-Per-Share Equity Financing", source="x", url="u",
                    published_at=1, first_seen_at=1)]
    sig = {**ok, "not_dilutive": S.dilution_signal(offer)}
    assert S.decide("intraday_continuation", sig).verdict == S.ELIGIBLE
    assert S.decide("intraday_continuation_v2", sig).verdict == S.REJECTED
    assert S.decide("intraday_continuation_v2", {**ok, "not_dilutive": S.dilution_signal([])}).verdict == S.ELIGIBLE
    assert I.INTRADAY_CONTINUATION_V2.experiment_id == "intraday_continuation@v2"


def test_the_quality_lane_adds_liquid_movers_the_gainers_list_never_shows():
    """2026-09-24: the top 50 by % were +20-200% sub-$5 names; NBIS (+9.5% at $248) and
    TWST (+8.4%) never reached the radar. Most-actives by volume, priced from snapshots."""
    from edge.ledger import MemoryStore as MS

    def get(url, params=None, headers=None, timeout=None):
        if "most-actives" in url:
            return Resp({"most_actives": [{"symbol": s} for s in ("NBIS", "TWST", "PENY", "FLAT", "KNOWN", "ABCDW")]})
        if "/v2/stocks/snapshots" in url:
            if params.get("feed") == "sip":
                raise RuntimeError("403 subscription does not permit querying recent SIP data")
            px = {"NBIS": (248.14, 226.6), "TWST": (41.0, 37.8), "PENY": (1.10, 0.90), "FLAT": (50.0, 49.5)}
            return Resp({s: {"latestTrade": {"p": p}, "prevDailyBar": {"c": c}}
                         for s, (p, c) in px.items() if s in params["symbols"].split(",")})
        raise AssertionError(url)

    lane = I._quality_lane(get, MS(), now=ts(10, 40), known={"KNOWN"}, universe=None)
    assert set(lane) == {"NBIS", "TWST"}            # PENY under $2, FLAT +1%, KNOWN already listed, ABCDW a warrant
    assert lane["NBIS"] == pytest.approx(9.5, abs=0.1)
