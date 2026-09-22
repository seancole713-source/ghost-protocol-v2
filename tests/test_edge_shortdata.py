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
