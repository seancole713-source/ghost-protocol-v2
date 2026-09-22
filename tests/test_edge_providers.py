"""edge providers and the readiness probe.

The build machine cannot reach any market-data host, so these tests use
payloads shaped like each vendor's documentation. They pin the CLASSIFICATION
logic -- that a plan refusal reads as a plan refusal, not "flaky"; that IEX is
a degraded fallback, not real-time coverage; that short VOLUME is never passed
off as short INTEREST. Whether the vendors answer as documented is what the
probe verifies in production.
"""
from __future__ import annotations

from datetime import date

import pytest

from edge import probe as P
from edge.providers import alpaca, base as B, polygon, public


class Resp:
    def __init__(self, code=200, payload=None, text=""):
        self.status_code, self._p, self.text = code, payload, text

    def json(self):
        if self._p is None:
            raise ValueError("no json")
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def router(routes):
    """routes: [(substring, Resp)] -- first match wins."""
    def get(url, params=None, headers=None, timeout=None):
        full = url + "?" + "&".join(f"{k}={v}" for k, v in (params or {}).items())
        for sub, resp in routes:
            if sub in full:
                return resp
        return Resp(404, {"message": "not routed"})
    return get


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("EDGE_PROBE_POLYGON_PACE_S", "0")
    monkeypatch.setenv("POLYGON_API_KEY", "k")
    monkeypatch.setenv("ALPACA_KEY_ID", "i")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")


TODAY = date(2026, 9, 23)

# The account as observed: reference data works, grouped daily refused.
POLYGON_AS_OBSERVED = [
    ("/v3/reference/tickers", Resp(200, {"results": [{"ticker": "AAPL"}, {"ticker": "CRML"}]})),
    ("/v2/aggs/grouped", Resp(403, {"status": "NOT_AUTHORIZED", "message": "You are not entitled to this data."})),
    ("/v2/snapshot/locale", Resp(403, {"message": "not entitled"})),
    ("/range/1/minute", Resp(200, {"results": [{"t": 1790083800000, "o": 1, "h": 1, "l": 1, "c": 1, "v": 5}]})),
    ("/v3/reference/splits", Resp(200, {"results": [{"ticker": "X"}]})),
    ("/v3/reference/dividends", Resp(200, {"results": [{"ticker": "FRO"}]})),
    ("/v2/reference/news", Resp(200, {"results": [{"id": "1"}]})),
]
ALPACA_FREE = [
    ("trades/latest?feed=sip", Resp(403, {"message": "subscription does not permit querying recent SIP data"})),
    ("trades/latest?feed=iex", Resp(200, {"trade": {"t": "2026-09-23T13:10:00Z", "p": 1.0}})),
    ("/bars", Resp(200, {"bars": [{"t": "2026-09-22T13:30:00Z"}]})),
    ("/screener/stocks/movers", Resp(200, {"gainers": [{"symbol": "GRML"}], "losers": [], "last_updated": "2026-09-23T13:09:00Z"})),
    ("/v1beta1/news", Resp(200, {"news": [{"created_at": "2026-09-23T12:00:00Z"}]})),
]
PUBLIC = [
    ("cdn.finra.org", Resp(200, None, "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n20260922|AAPL|100|0|400|B,Q,N\n")),
    ("sec.gov", Resp(200, None, "<feed><entry><updated>2026-09-23T08:01:00-04:00</updated></entry></feed>")),
]
IBKR_TEXT = "#BOF|2026.09.23|09:00:00\n#SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|\nCRML|USD|CRITICAL METALS|1|X|-38.2|42.5|15000|\nAAPL|USD|APPLE|2|Y|3.5|0.25|>10000000|\n#EOF\n"


def test_a_plan_refusal_reads_as_not_authorized():
    got = {p.capability: p for p in polygon.probe(router(POLYGON_AS_OBSERVED), today=TODAY, pace_s=0)}
    assert got[B.DAILY_ALL].status == B.NOT_AUTHORIZED
    assert "not entitled" in got[B.DAILY_ALL].note
    assert got[B.UNIVERSE].status == B.OK and got[B.UNIVERSE].rows == 2


def test_missing_key_is_no_key_not_error(monkeypatch):
    monkeypatch.delenv("POLYGON_API_KEY")
    assert {p.status for p in polygon.probe(router([]), today=TODAY, pace_s=0)} == {B.NO_KEY}


def test_alpaca_free_plan_refuses_sip_and_serves_iex():
    got = {p.capability: p for p in alpaca.probe(router(ALPACA_FREE), today=TODAY)}
    assert got[B.QUOTE_SIP].status == B.NOT_AUTHORIZED
    assert got[B.QUOTE_IEX].status == B.OK
    assert got[B.MOVERS].status == B.OK


def test_a_timeout_is_an_error_with_its_type():
    def boom(*a, **k):
        raise TimeoutError()
    assert {p.status for p in polygon.probe(boom, today=TODAY, pace_s=0)} == {B.ERROR}


def test_finra_short_volume_parses_and_labels_itself():
    rows = public.parse_finra_short_volume(PUBLIC[0][1].text)
    assert rows["AAPL"]["short_ratio"] == pytest.approx(0.25)
    p = public.probe_finra(router(PUBLIC), today=TODAY)
    assert p.status == B.OK and "not short interest" in p.note


def test_ibkr_file_parses_floors_and_fees():
    rows = public.parse_ibkr_shortstock(IBKR_TEXT)
    assert rows["CRML"]["fee_rate_pct"] == pytest.approx(42.5)
    assert rows["AAPL"]["available_is_floor"] is True
    assert public.probe_ibkr(lambda: IBKR_TEXT).rows == 2


def test_edgar_asks_for_a_real_user_agent(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    p = public.probe_edgar(router(PUBLIC))
    assert p.status == B.OK and "SEC_USER_AGENT" in p.note


def full_router(extra=()):
    return router(list(extra) + POLYGON_AS_OBSERVED + ALPACA_FREE + PUBLIC)


def test_probe_answers_per_strategy_with_unlock_hints():
    rep = P.run(full_router(), ibkr_fetch=lambda: IBKR_TEXT)
    s = rep["strategies"]
    # Movers via Alpaca, news, minute bars: runs -- but on IEX quotes, so DEGRADED.
    assert s["premarket_continuation"]["state"] == "DEGRADED"
    assert s["premarket_continuation"]["degraded"] == ["real-time quotes"]
    # Short interest has no adapter yet: honestly BLOCKED, not assumed.
    assert s["crowded_short_ignition"]["state"] == "BLOCKED"
    assert s["crowded_short_ignition"]["missing"][0]["need"].startswith("short interest")
    # Grouped daily refused, snapshot refused: the miss audit can't see the market.
    assert s["daily_miss_audit"]["state"] == "BLOCKED"
    assert "grouped daily" in s["daily_miss_audit"]["missing"][0]["unlock"][0]


def test_short_volume_never_satisfies_short_interest():
    rep = P.run(full_router(), ibkr_fetch=lambda: IBKR_TEXT)
    assert rep["capabilities"][B.SHORT_VOLUME]["status"] == B.OK
    assert rep["strategies"]["crowded_short_ignition"]["state"] == "BLOCKED"


def test_summary_is_one_line_per_strategy():
    lines = P.summary_lines(P.run(full_router(), ibkr_fetch=lambda: IBKR_TEXT))
    assert len(lines) == len(P.STRATEGIES)
    assert any(l.startswith("supported_universe: READY") for l in lines)


def test_polygon_calls_are_paced_so_ghost_is_not_rate_limited():
    waits = []
    polygon.probe(router(POLYGON_AS_OBSERVED), today=TODAY, pace_s=13, sleep=waits.append)
    assert waits == [13] * 6
