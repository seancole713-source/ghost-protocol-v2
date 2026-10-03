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


def test_an_intraday_forecast_is_stamped_with_the_clock_at_issuance_not_the_tick_start(ledger):
    """EDGE-01: the tick starts 10:40, its fetches take until 10:44. The forecast is issued and
    recorded at 10:44 and its window opens 10:45 -- never a 10:41 window recorded at 10:44. A
    paired later version shares the same issuance moment (and so its v1 paper order)."""
    out = I.tick(Market(ts(10, 40)), ledger, now=ts(10, 40), clock=lambda: ts(10, 44))
    assert "catalyst_breakout@v1:CATX" in out["issued"] and out["late_refused"] == []
    f = ledger.store.scan("forecasts", experiment_id="catalyst_breakout@v1")[0]
    assert (f["issued_at"], f["recorded_at"], f["window_start"]) == (ts(10, 44), ts(10, 44), ts(10, 45))
    assert f["entry_expiry"] == ts(11, 5) and f["evidence"]["data_as_of"] == ts(10, 40)
    v1 = ledger.store.scan("forecasts", experiment_id="intraday_continuation@v1")[0]
    v2 = ledger.store.scan("forecasts", experiment_id="intraday_continuation@v2")[0]
    assert v1["issued_at"] == v2["issued_at"] == ts(10, 44)


def test_an_intraday_tick_that_reaches_issuance_after_the_window_refuses(ledger):
    """EDGE-01: a tick that started inside the 09:45-14:30 window but reached issuance after it
    records nothing; each refusal is counted (late_refused) and kept on the radar."""
    out = I.tick(Market(ts(10, 40)), ledger, now=ts(10, 40), clock=lambda: ts(14, 31))
    assert out["issued"] == [] and len(out["late_refused"]) >= 2
    assert all("14:31:00 ET" in m for m in out["late_refused"])
    assert ledger.store.scan("forecasts") == []
    catx = ledger.store.get("edge_radar", "2026-09-23|CATX")
    assert catx["state"] != R.ENTRY_ELIGIBLE and "refused" in catx["blocker"]["reasons"][0]


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


# ------------------------------- SIP history must never silently drop stocks (audit 2026-09-25) --

class Paged:
    """Alpaca's multi-symbol bars, paginated as Alpaca does: symbol by symbol, `page` bars a page,
    next_page_token until done. 12 prior sessions x 390 bars = 4,680 bars per symbol."""

    HIST = None

    def __init__(self, page):
        self.page, self.requests = page, []
        if Paged.HIST is None:
            Paged.HIST = history_bars("X")                         # the same tape for every symbol

    def __call__(self, url, params=None, headers=None, timeout=None):
        assert url.endswith("/v2/stocks/bars") and params["timeframe"] == "1Min"
        syms = sorted(params["symbols"].split(","))
        off = int(params.get("page_token") or 0)
        if not off:
            self.requests.append(syms)
        n = len(Paged.HIST)
        bars = {}
        for k in range(off, min(off + self.page, n * len(syms))):
            bars.setdefault(syms[k // n], []).append(Paged.HIST[k % n])
        nxt = off + self.page
        return Resp({"bars": bars, "next_page_token": str(nxt) if nxt < n * len(syms) else None})


SYMS = [f"S{i:02d}" for i in range(80)]


def test_the_old_single_request_would_drop_the_alphabetically_last_symbols(caplog):
    from edge.providers import alpaca as A
    rows, complete = A.bars_pages(Paged(2000), SYMS, timeframe="1Min", start="x", feed="sip")
    assert complete is False and not rows["S79"]                   # silently empty before the fix
    assert "page cap hit" in caplog.text


def test_history_is_requested_in_chunks_and_every_symbol_gets_its_sessions():
    store, g = MemoryStore(), Paged(2000)                           # 10 symbols: 24 pages, under the cap
    out = I._history(g, store, SYMS, DAY, feed="sip")
    assert all(len(r) <= I.HISTORY_CHUNK for r in g.requests) and len(g.requests) == 8
    assert set(out) == set(SYMS)
    for s in ("S00", "S79"):
        c = store.get("edge_rvol", f"{DAY.isoformat()}|{s}")
        assert c["sessions"] == 10 and c["complete"] is True and len(out[s][389]) == 10


def test_a_truncated_chunk_is_re_asked_one_symbol_at_a_time(caplog):
    store, g = MemoryStore(), Paged(1000)                           # 10 symbols: 47 pages, over the cap
    out = I._history(g, store, SYMS[:10], DAY, feed="sip")
    assert g.requests[0] == SYMS[:10] and g.requests[1:] == [[s] for s in SYMS[:10]]
    assert "truncated" in caplog.text
    assert all(store.get("edge_rvol", f"{DAY.isoformat()}|{s}")["sessions"] == 10 for s in SYMS[:10])
    assert all(len(out[s][389]) == 10 for s in SYMS[:10])


def test_a_symbol_still_truncated_is_left_out_and_retried_next_tick_never_cached_short(caplog):
    store, g = MemoryStore(), Paged(100)                            # even one symbol overflows 40 pages
    out = I._history(g, store, ["AAA", "BBB"], DAY, feed="sip")
    assert out == {} and store.get("edge_rvol", f"{DAY.isoformat()}|AAA") is None
    assert "still truncated" in caplog.text
    g2 = Paged(5000)                                                # the next tick: the answer fits
    out = I._history(g2, store, ["AAA", "BBB"], DAY, feed="sip")
    assert g2.requests == [["AAA", "BBB"]] and store.get("edge_rvol", f"{DAY.isoformat()}|BBB")["sessions"] == 10
    I._history(g2, store, ["AAA", "BBB"], DAY, feed="sip")
    assert len(g2.requests) == 1                                    # complete now: cached for the day


# ------------------------------------ Task #64: one 429 must not drop the tick --

class _R429:
    status_code = 429
    headers = {}

    def json(self):
        return {"message": "too many requests"}

    def raise_for_status(self):
        raise RuntimeError("429 Client Error: Too Many Requests")


class Throttled(Market):
    """The radar's 1-minute bars call answers 429 `times` times, then the normal market."""

    def __init__(self, now, times):
        super().__init__(now)
        self.left = times

    def __call__(self, url, params=None, headers=None, timeout=None):
        p = params or {}
        if url.endswith("/v2/stocks/bars") and p.get("timeframe") == "1Min" \
                and str(p.get("start", "")).startswith(DAY.isoformat()) and self.left > 0:
            self.left -= 1
            return _R429()
        return super().__call__(url, params=params, headers=headers, timeout=timeout)


def test_one_429_on_the_radar_bars_is_retried_and_the_tick_completes(ledger, monkeypatch):
    from edge.providers import alpaca as A
    waits = []
    monkeypatch.setattr(A, "_sleep", waits.append)
    out = I.tick(Throttled(ts(10, 40), times=1), ledger, now=ts(10, 40))
    assert out["status"] in ("issued", "watched") and out["movers"] == 4
    assert waits == [1.0]
    beat = ledger.store.get(I.TICK_TABLE, DAY.isoformat())
    assert beat["status"] == out["status"] and beat["last_ok_at"] == ts(10, 40)
    assert beat["source_errors"] == {}


def test_a_persistent_429_is_a_named_source_error_not_no_data(ledger, monkeypatch):
    from edge.providers import alpaca as A
    monkeypatch.setattr(A, "_sleep", lambda s: None)
    with pytest.raises(A.RateLimited):
        I.tick(Throttled(ts(10, 40), times=99), ledger, now=ts(10, 40))
    beat = ledger.store.get(I.TICK_TABLE, DAY.isoformat())
    assert beat["status"] == "error" and beat["errors"] == 1 and beat["last_ok_at"] is None
    assert beat["source_errors"]["alpaca"]["kind"] == "rate_limited"
    assert beat["source_errors"]["alpaca"]["path"] == "/v2/stocks/bars"
    assert "RateLimited" in beat["error"] and "no_movers" not in beat["error"]


# ---------------- Task #59: an ELIGIBLE name with no forecast says exactly why (VEEA 2026-10-01) --

def _prior_catalyst_forecasts(ledger, syms, at):
    for s in syms:
        f = issue_intraday(I.CATALYST_BREAKOUT, symbol=s, session_date=DAY, entry_ref=50.0, issued_at=at)
        ledger.record(f, now=at)


def test_an_eligible_name_over_the_daily_cap_names_the_cap(ledger):
    """2026-10-01: VEEA (+88%, catalyst "Veea Announces Agreement With TROLLEE Holdings ...") turned
    catalyst_breakout ELIGIBLE at 11:34 ET; ACN (09:48) and SNPS (09:53) had already used the
    2-a-day cap. The radar said only "daily cap, entry window or already recorded". The cap is the
    rule working as designed -- unchanged -- but the record now names it."""
    for spec in I.INTRADAY_SPECS:
        ledger.register(spec, now=ts(9, 45))
    _prior_catalyst_forecasts(ledger, ["ACN", "SNPS"], ts(9, 48))
    out = I.tick(Market(ts(10, 40)), ledger, now=ts(10, 40))
    assert "catalyst_breakout@v1:CATX" not in out["issued"]
    assert len(ledger.store.scan("forecasts", experiment_id="catalyst_breakout@v1")) == 2   # cap unchanged
    catx = ledger.store.get("edge_radar", "2026-09-23|CATX")
    assert catx["blocker"]["verdict"] == "ELIGIBLE"
    assert catx["blocker"]["reasons"] == [
        "eligible, but no forecast recorded: daily cap reached (2 of 2 catalyst_breakout@v1 forecasts today)"]
    assert any(m.startswith("catalyst_breakout@v1:CATX: daily cap reached") for m in out["not_recorded"])
    beat = ledger.store.get(I.TICK_TABLE, DAY.isoformat())
    assert beat["not_recorded"] == out["not_recorded"]


def test_a_refused_contract_is_named_never_swallowed(ledger, monkeypatch):
    """The old `except Exception: return None` hid WHY a contract was refused."""
    def refuse(*a, **k):
        raise ContractError("too late in the session for this setup's entry window")
    monkeypatch.setattr(I, "issue_intraday", refuse)
    out = I.tick(Market(ts(10, 40)), ledger, now=ts(10, 40))
    assert out["issued"] == []
    catx = ledger.store.get("edge_radar", "2026-09-23|CATX")
    assert catx["blocker"]["reasons"] == ["eligible, but no forecast recorded: contract refused: "
                                          "too late in the session for this setup's entry window"]


def test_an_unexpected_build_error_is_named_and_the_tick_goes_on(ledger, monkeypatch):
    def boom(*a, **k):
        raise KeyError("rvol")
    monkeypatch.setattr(I, "issue_intraday", boom)
    out = I.tick(Market(ts(10, 40)), ledger, now=ts(10, 40))
    assert out["status"] == "watched" and out["movers"] == 4
    catx = ledger.store.get("edge_radar", "2026-09-23|CATX")
    assert catx["blocker"]["reasons"][0].startswith("eligible, but no forecast recorded: forecast not built: ")


# ------------- Task #59: "no catalyst" must say what the feed carried, and a capped feed is unknown --

class NewsMarket(Market):
    """The market above, with its own news tape: `stories` [(symbols, headline, hh, mm)], and
    every news page answering with a next_page_token when `capped`."""

    def __init__(self, now, stories, capped=False):
        super().__init__(now)
        self.stories, self.capped, self.news_requests = stories, capped, []

    def __call__(self, url, params=None, headers=None, timeout=None):
        if "/v1beta1/news" in url:
            asked = set(params["symbols"].split(","))
            if "page_token" not in params:
                self.news_requests.append(sorted(asked))
            news = [{"headline": h, "created_at": iso(ts(hh, mm)), "symbols": list(syms), "source": "x", "url": "u"}
                    for syms, h, hh, mm in self.stories if asked & set(syms)]
            return Resp({"news": news, "next_page_token": "more" if self.capped else None})
        return super().__call__(url, params=params, headers=headers, timeout=timeout)


def test_a_name_with_no_catalyst_keeps_the_headlines_the_feed_carried(ledger):
    """2026-10-01 MEDS (+37%): "no dated company-specific catalyst" -- with nothing on record to
    tell whether the feed never carried its 06:00 release or the classifier read it as not
    company-specific. The release as published ("DataMeds AI's Corexa Pharmacy Surpasses $1
    Million In Monthly Revenue") is tagged "other" by the keyword classifier. The radar now keeps
    what the feed carried and how each story was read; the classifier itself is unchanged."""
    meds = "DataMeds AI's Corexa Pharmacy Surpasses $1 Million In Monthly Revenue"
    m = NewsMarket(ts(10, 40), [(["CATX"], "CATX wins contract award from Navy", 8, 0),
                                (["QUIET"], meds, 6, 0)])
    I.tick(m, ledger, now=ts(10, 40))
    q = ledger.store.get("edge_radar", "2026-09-23|QUIET")
    assert q["catalyst"] is None
    assert q["news_seen"] == {"items": 1, "latest": [{"kind": "other", "headline": meds}], "at": ts(10, 40)}
    cont = ledger.store.get("edge_radar", "2026-09-23|CONT")
    assert cont["news_seen"] == {"items": 0, "latest": [], "at": ts(10, 40)}      # the feed had no story
    catx = ledger.store.get("edge_radar", "2026-09-23|CATX")
    assert catx["catalyst"]["kind"] == "contract" and catx["news_seen"] is None
    from edge import readout as RO
    items = {i["symbol"]: i for i in RO.radar(ledger.store, "2026-09-23")["items"]}
    assert items["QUIET"]["news_seen"]["latest"][0]["kind"] == "other"


def test_a_page_capped_news_answer_is_unknown_never_no_catalyst(ledger, caplog):
    """The radar asked for every mover's 24h news in one request capped at 8 pages, newest first;
    a capped answer lost the oldest (pre-market) releases and read as "no catalyst"."""
    m = NewsMarket(ts(10, 40), [(["CATX"], "CATX wins contract award from Navy", 8, 0)], capped=True)
    out = I.tick(m, ledger, now=ts(10, 40))
    assert "page cap hit" in caplog.text
    assert m.news_requests[0] == ["CATX", "CONT", "QUIET", "STALE"]
    assert m.news_requests[1:] == [["CATX"], ["CONT"], ["QUIET"], ["STALE"]]   # re-asked one at a time
    assert not any(x.startswith("catalyst_breakout") for x in out["issued"])
    q = ledger.store.get("edge_radar", "2026-09-23|QUIET")
    assert not any("no dated company-specific catalyst" in r for r in q["blocker"]["reasons"])
    assert q["news_seen"] is None


def test_news_is_asked_in_chunks_and_a_complete_chunk_is_used_whole():
    from edge.providers import alpaca as A
    m = NewsMarket(ts(10, 40), [(["S05"], "S05 wins contract award from Navy", 8, 0)])
    syms = [f"S{i:02d}" for i in range(45)]
    items, unknown = A.news_complete(m, syms, start=iso(ts(10, 40) - 86_400))
    assert [len(r) for r in m.news_requests] == [20, 20, 5] and unknown == set()
    assert [n["headline"] for n in items] == ["S05 wins contract award from Navy"]
