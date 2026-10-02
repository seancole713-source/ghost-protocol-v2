"""Short interest + borrow: free sources, verified by the probe, never defaulted."""
from __future__ import annotations

from datetime import date

import pytest

from edge import detectors as D
from edge.intraday import _crowded
from edge.providers import base as B, shortdata as SD


class R:
    def __init__(self, code=200, body=None):
        self.status_code, self._b = code, body

    def json(self):
        return self._b

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class Http:
    def __init__(self, post=None, get=None):
        self._post, self._get, self.posts = post, get, []

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append(json)
        return self._post

    def get(self, url, headers=None, timeout=None):
        return self._get


FINRA_ROWS = [
    {"symbolCode": "GME", "currentShortPositionQuantity": 60_000_000, "settlementDate": "2026-09-15",
     "averageDailyVolumeQuantity": 8_000_000, "daysToCoverQuantity": 7.5},
    {"symbolCode": "GME", "currentShortPositionQuantity": 55_000_000, "settlementDate": "2026-08-29",
     "averageDailyVolumeQuantity": 9_000_000, "daysToCoverQuantity": 6.1},
]


def test_the_latest_settlement_wins():
    out = SD.fetch_finra_si_for(Http(post=R(200, FINRA_ROWS)), ["gme"])
    assert out["GME"]["settlement_date"] == "2026-09-15" and out["GME"]["days_to_cover"] == 7.5


def test_a_finra_credential_wall_reads_as_not_authorized():
    p = SD.probe_finra_si(Http(post=R(403, {"message": "unauthorized"})))
    assert p.status == B.NOT_AUTHORIZED and "credentials" in p.note


def test_unknown_field_names_are_reported_not_guessed():
    p = SD.probe_finra_si(Http(post=R(200, [{"weirdSymbol": "X", "weirdQty": 1}])))
    assert p.status == B.ERROR and "fields seen: weirdQty, weirdSymbol" in p.note


def test_borrow_from_iborrowdesk():
    body = {"real_time": [{"fee": 48.2, "available": 15000, "time": "2026-09-23T13:00:00"}], "daily": []}
    point = SD.fetch_borrow(Http(get=R(200, body)), "gme")
    assert point["borrow_fee_pct"] == 48.2 and "unofficial" in point["source"]
    assert SD.probe_borrow(Http(get=R(200, body))).status == B.OK


def test_crowded_short_on_days_to_cover_when_float_is_unknown():
    rec = {"si": {"days_to_cover": 7.5, "settlement_date": "2026-09-15"},
           "borrow": {"borrow_fee_pct": 48.2, "available_shares": 15000}}
    sig = _crowded(rec, date(2026, 9, 23))
    assert sig.state == D.PASS and sig.evidence["basis"] == "days to cover >= 5"


def test_missing_short_data_is_unknown_never_zero():
    assert _crowded({}, date(2026, 9, 23)).state == D.UNKNOWN
    stale = {"si": {"days_to_cover": 9, "settlement_date": "2026-08-01"}, "borrow": {"borrow_fee_pct": 60}}
    assert _crowded(stale, date(2026, 9, 23)).state == D.UNKNOWN          # 53 days old


def test_the_probe_includes_both_when_it_has_an_http_client():
    from edge import probe as P
    probes = P.collect(lambda *a, **k: R(404, {}), ibkr_fetch=lambda: "", http=Http(post=R(403, {}), get=R(200, {"daily": []})))
    caps = {(p.capability, p.provider) for p in probes}
    assert (B.SHORT_INTEREST, "finra_api") in caps and (B.BORROW, "iborrowdesk") in caps


def test_row_order_cannot_resurrect_an_older_report():
    assert SD.parse_finra_si(list(reversed(FINRA_ROWS)))["GME"]["settlement_date"] == "2026-09-15"
    assert SD.parse_finra_si(FINRA_ROWS)["GME"]["settlement_date"] == "2026-09-15"


def test_short_interest_alone_is_measured_honestly():
    from edge.intraday import _short_interest
    rec = {"si": {"days_to_cover": 7.5, "settlement_date": "2026-09-15"}}
    s = _short_interest(rec, date(2026, 9, 23))
    assert s.name == "short_interest" and s.state == D.PASS
    assert _short_interest({}, date(2026, 9, 23)).state == D.UNKNOWN


def test_short_interest_ignition_runs_without_borrow_data_but_crowded_short_does_not():
    from edge import setups as S
    sig = {"short_interest": D.Signal("short_interest", D.PASS, 7.5), "liquidity": D.Signal("liquidity", D.PASS),
           "rvol_tod": D.Signal("rvol_tod", D.PASS), "acceleration": D.Signal("acceleration", D.PASS),
           "crowded_short": D.Signal("crowded_short", D.UNKNOWN, evidence={"missing": "borrow fee"})}
    assert S.decide("short_interest_ignition", sig).verdict == S.ELIGIBLE
    assert S.decide("crowded_short_ignition", sig).verdict == S.DATA_UNAVAILABLE


def test_the_probe_marks_it_runnable_on_verified_free_data():
    from edge import probe as P
    caps = {B.SHORT_INTEREST: {"status": B.OK}, B.QUOTE_IEX: {"status": B.OK}, B.MINUTE_BARS: {"status": B.OK}}
    assert P.assess(caps)["short_interest_ignition"]["state"] == "DEGRADED"      # IEX quotes only


class FlakyHttp:
    """FINRA / iBorrowDesk that fail (timeout) until `up` is set, then answer."""

    def __init__(self):
        self.up, self.posts, self.gets = False, 0, 0

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts += 1
        if not self.up:
            raise TimeoutError("finra read timed out")
        return R(200, FINRA_ROWS)

    def get(self, url, headers=None, timeout=None):
        self.gets += 1
        if not self.up:
            raise ConnectionError("reset")
        if "NOREC" in url:
            return R(404, {})
        return R(200, {"real_time": [{"fee": 48.2, "available": 15000, "time": "t"}], "daily": []})


def test_a_transient_short_data_failure_is_retried_after_the_backoff_not_cached_all_day():
    """EDGE-11: a timeout at 10:00 used to cache si=None / borrow=None for the whole session."""
    from edge.intraday import SHORT_RETRY_S, _short_data
    from edge.ledger import MemoryStore
    store, http, day = MemoryStore(), FlakyHttp(), date(2026, 9, 23)
    t0 = 1_790_000_000
    first = _short_data(http, store, ["GME", "NOREC"], day, now=t0)
    assert first["GME"]["si"] is None and first["GME"]["si_status"] == "error"
    assert first["GME"]["borrow"] is None and first["GME"]["borrow_status"] == "error"
    assert _crowded(first["GME"], day).state == D.UNKNOWN              # unknown, never zero
    http.up = True
    _short_data(http, store, ["GME", "NOREC"], day, now=t0 + 60)        # inside the backoff: no call
    assert (http.posts, http.gets) == (1, 2)
    later = _short_data(http, store, ["GME", "NOREC"], day, now=t0 + SHORT_RETRY_S)
    assert later["GME"]["si"]["days_to_cover"] == 7.5 and later["GME"]["si_status"] == "ok"
    assert later["GME"]["borrow"]["borrow_fee_pct"] == 48.2 and later["GME"]["borrow_status"] == "ok"
    # the source's definite "no record" is terminal for the day: never asked again
    assert later["NOREC"]["si"] is None and later["NOREC"]["si_status"] == "none"
    assert later["NOREC"]["borrow"] is None and later["NOREC"]["borrow_status"] == "none"
    calls = (http.posts, http.gets)
    _short_data(http, store, ["GME", "NOREC"], day, now=t0 + 5 * SHORT_RETRY_S)
    assert (http.posts, http.gets) == calls


def test_a_cached_row_from_before_statuses_is_kept_as_it_was():
    from edge.intraday import _short_data
    from edge.ledger import MemoryStore
    store, http = MemoryStore(), FlakyHttp()
    store.put("edge_short", "2026-09-23", {"GME": {"si": None, "borrow": None}})
    _short_data(http, store, ["GME"], date(2026, 9, 23), now=1_790_000_000)
    assert (http.posts, http.gets) == (0, 0)
