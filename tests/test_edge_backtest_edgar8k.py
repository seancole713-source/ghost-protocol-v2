"""edgar_8k_breakout@v1: a preregistered hypothesis replayed on history (no network: a fake `get`)."""
from __future__ import annotations

import html
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import requests

from edge import backtest_edgar8k as E
from edge import readout as R
from edge.contracts import LOSS, NO_FILL, WIN
from edge.ledger import MemoryStore

ET = ZoneInfo("America/New_York")
D = date(2026, 9, 17)          # Thursday: the decision session
PREV = date(2026, 9, 16)


def ts(d, hh, mm):
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET).timestamp())


def iso(t):
    return datetime.fromtimestamp(t, tz=ET).isoformat()


class Resp:
    def __init__(self, payload=None, status=200, text=None):
        self._p, self.status_code, self.headers = payload, status, {}
        self.text = text if text is not None else json.dumps(payload or {})

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


# ------------------------------------------------------------------ the fake market --
# ticker: (cik, close, shares/day, high on D, minute path, in SEC company_tickers.json)
NAMES = {
    "EIGHT": (101, 10.0, 5_000_000, 11.0, "win", True),       # 2-for-1 split ON D: raw prior close $20
    "PRESS": (102, 10.0, 4_000_000, 11.0, "lose", True),
    "EDGEA": (108, 10.0, 3_000_000, 11.0, "gap_above", True),  # accepted exactly 16:00:00 D-1
    "EDGEB": (109, 10.0, 2_500_000, 11.0, "never", True),      # accepted exactly 09:00:00 D
    "CAPX": (110, 10.0, 1_000_000, 11.0, "win", True),         # 5th eligible: over the cap of 4
    "DILU": (103, 10.0, 2_000_000, 11.0, "win", True),         # 1.01 + securities purchase agreement
    "ITEMX": (104, 10.0, 2_000_000, 11.0, "win", True),        # 1.01 + 3.02
    "AMND": (105, 10.0, 2_000_000, 11.0, "win", True),         # 8-K/A in the window
    "NOEX": (111, 10.0, 2_000_000, 11.0, "win", True),         # 8.01 without an EX-99
    "EARLY": (106, 10.0, 1_200_000, 11.0, "lose", True),       # 8-K at 15:59:59 D-1: outside, a twin
    "LATE": (107, 10.0, 1_100_000, 11.0, "win", True),         # 8-K at 09:00:01 D: outside, a twin
    "TWINW": (201, 10.0, 2_000_000, 11.0, "win", True),
    "TWINL": (202, 10.0, 1_500_000, 11.0, "lose", True),
    "GAPUP": (203, 10.0, 900_000, 11.0, "gap_above", True),
    "RUNTH": (204, 10.0, 850_000, 11.0, "run_through", True),
    "NOTRIG": (205, 10.0, 3_000_000, 10.2, "win", True),       # day's high below the trigger
    "POLY": (401, 10.0, 800_000, 11.0, "never", False),        # mapped only through Polygon's cik
    "CHEAP": (112, 1.5, 9_000_000, 2.0, "win", True),          # under $2: never fetched
    "THIN": (113, 10.0, 300_000, 11.0, "win", True),           # $3M a day: never fetched
    "RSPLT": (114, 10.0, 2_000_000, 11.0, "win", True),        # reverse split 3 sessions ago
    "MULA": (302, 10.0, 2_000_000, 11.0, "win", True),         # one CIK, two CS tickers
    "MULB": (302, 10.0, 2_000_000, 11.0, "win", True),
}
SPLITS = [{"ticker": "EIGHT", "execution_date": D.isoformat(), "split_from": 1, "split_to": 2},
          {"ticker": "RSPLT", "execution_date": "2026-09-14", "split_from": 10, "split_to": 1}]

ITEM_TITLE = {"1.01": "Entry into a Material Definitive Agreement", "8.01": "Other Events",
              "9.01": "Financial Statements and Exhibits", "3.02": "Unregistered Sales of Equity Securities",
              "2.03": "Creation of a Direct Financial Obligation or an Obligation under an Off-Balance Sheet "
                      "Arrangement of a Registrant"}


def header_text(acc, form, accepted, items, exhibits, *, escaped=False):
    lines = [f"<SEC-DOCUMENT>{acc}.txt : 20260917", f"<SEC-HEADER>{acc}.hdr.sgml : 20260917",
             f"<ACCEPTANCE-DATETIME>{accepted}", f"ACCESSION NUMBER:\t\t{acc}",
             f"CONFORMED SUBMISSION TYPE:\t{form}", "PUBLIC DOCUMENT COUNT:\t\t2"]
    lines += [f"ITEM INFORMATION:\t\t{ITEM_TITLE[i]}" for i in items]
    lines += ["FILED AS OF DATE:\t\t20260917", "</SEC-HEADER>"]
    for n, (typ, desc) in enumerate([(form, form)] + exhibits, 1):
        lines += ["<DOCUMENT>", f"<TYPE>{typ}", f"<SEQUENCE>{n}", f"<FILENAME>doc{n}.htm",
                  f"<DESCRIPTION>{desc}", "<TEXT>", "</TEXT>", "</DOCUMENT>"]
    body = "\n".join(lines)
    if escaped:
        return f"<html><body><pre>{html.escape(body)}</pre></body></html>"
    return body


PR = [("EX-99.1", "PRESS RELEASE")]
# accession: (index date, form, cik, accepted, items, exhibits)
FILINGS = {
    "0000000101-26-000001": (D, "8-K", 101, "20260916183000", ["1.01", "9.01"], PR),
    "0000000102-26-000001": (D, "8-K", 102, "20260917070000", ["8.01", "9.01"], PR),
    "0000000108-26-000001": (PREV, "8-K", 108, "20260916160000", ["1.01"], []),
    "0000000109-26-000001": (D, "8-K", 109, "20260917090000", ["8.01", "9.01"], [("EX-99.1", "Press release")]),
    "0000000110-26-000001": (D, "8-K", 110, "20260917063000", ["1.01"], []),
    "0000000103-26-000001": (D, "8-K", 103, "20260917060000", ["1.01", "9.01"],
                             [("EX-10.1", "Form of Securities Purchase Agreement")]),
    "0000000104-26-000001": (D, "8-K", 104, "20260917061000", ["1.01", "3.02"], []),
    "0000000105-26-000001": (D, "8-K/A", 105, "20260917062000", ["1.01"], []),
    "0000000111-26-000001": (D, "8-K", 111, "20260917063000", ["8.01"], []),
    "0000000106-26-000001": (PREV, "8-K", 106, "20260916155959", ["1.01"], []),
    "0000000107-26-000001": (D, "8-K", 107, "20260917090001", ["1.01"], []),
    "0000000112-26-000001": (D, "8-K", 112, "20260917063000", ["1.01"], []),
    "0000000114-26-000001": (D, "8-K", 114, "20260917063000", ["1.01"], []),
    "0000000301-26-000001": (PREV, "8-K", 301, "20260916170000", ["1.01"], []),
    "0000000302-26-000001": (PREV, "8-K", 302, "20260916170000", ["1.01"], []),
}


def daily_index(d):
    rows = ["Description:           Daily Index of EDGAR Dissemination Feed by Form Type",
            f"Last Data Received:    {d:%B %d, %Y}", "", "",
            "Form Type   Company Name                                                  CIK         Date Filed  File Name",
            "-" * 140]
    for acc, (fd, form, cik, *_rest) in FILINGS.items():
        if fd == d:
            rows.append(f"{form:<12}{'Company ' + str(cik) + ' Inc':<62}{cik:<12}{d:%Y%m%d}    "
                        f"edgar/data/{cik}/{acc}.txt")
    rows.append(f"{'10-Q':<12}{'Other Co':<62}{'999':<12}{d:%Y%m%d}    edgar/data/999/0000000999-26-000001.txt")
    return "\n".join(rows)


def bar_rows(d, path):
    trig = 10.40
    steps = {
        "win": [(10.30, 10.45, 10.28, 10.42), (10.42, 10.60, 10.40, 10.58), (10.58, 10.95, 10.55, 10.90)],
        "lose": [(10.30, 10.45, 10.28, 10.42), (10.42, 10.43, 10.05, 10.06)],
        "gap_above": [(10.70, 10.80, 10.50, 10.75)],
        "never": [(10.00, 10.20, 9.95, 10.10)],
        "run_through": [(10.30, 10.70, 10.28, 10.65), (10.65, 10.70, 10.50, 10.60), (10.60, 11.00, 10.58, 10.95)],
    }[path]
    assert trig
    rows, t = [], ts(d, 9, 30)
    for o, h, l, c in steps:
        rows.append({"t": iso(t), "o": o, "h": h, "l": l, "c": c, "v": 10_000})
        t += 60
    last = steps[-1][3]
    while t <= ts(d, 15, 35):
        rows.append({"t": iso(t), "o": last, "h": last, "l": last, "c": last, "v": 1_000})
        t += 60
    return rows


class Market:
    def __init__(self, *, daily_403=(), alpaca_403=False):
        self.calls, self.daily_403, self.alpaca_403 = [], set(daily_403), alpaca_403

    def kind(self, url):
        for k in ("/v3/reference/splits", "/v3/reference/tickers", "/v2/aggs/grouped/", "company_tickers.json",
                  "/daily-index/", "-index-headers.html", "/v2/stocks/bars"):
            if k in url:
                return k
        return url

    def count(self, kind):
        return sum(1 for k, _u in self.calls if k == kind)

    def __call__(self, url, params=None, headers=None, timeout=None):
        params = params or {}
        k = self.kind(url)
        self.calls.append((k, url))
        if k == "/v3/reference/splits":
            return Resp({"results": SPLITS})
        if k == "/v3/reference/tickers":
            assert params.get("type") == "CS" and params.get("date")
            return Resp({"results": [{"ticker": t, "type": "CS", "cik": f"{v[0]:010d}"} for t, v in NAMES.items()]})
        if k == "/v2/aggs/grouped/":
            assert params.get("adjusted") == "false", params
            d = date.fromisoformat(url.rstrip("/").split("/")[-1])
            if d in self.daily_403:
                return Resp({"message": "plan does not include this timeframe"}, status=403)
            out = []
            for t, (_cik, c, v, hi, _p, _s) in NAMES.items():
                px, sh = c, v
                if t == "EIGHT" and d < D:
                    px, sh = c * 2, v / 2             # as traded before the 2-for-1 split on D
                out.append({"T": t, "o": px, "h": hi if d == D else px, "l": px, "c": px, "v": sh, "vw": px})
            return Resp({"results": out})
        if k == "company_tickers.json":
            assert headers and headers.get("User-Agent")
            rows = [{"cik_str": v[0], "ticker": t, "title": t} for t, v in NAMES.items() if v[5]]
            rows.append({"cik_str": 101, "ticker": "EIGHT-WT", "title": "warrant"})      # not CS: ignored
            return Resp(text=json.dumps({str(i): r for i, r in enumerate(rows)}))
        if k == "/daily-index/":
            assert headers and headers.get("User-Agent")
            d = datetime.strptime(url.rsplit(".", 2)[-2], "%Y%m%d").date()
            if d not in (PREV, D):
                return Resp(status=404, text="")
            return Resp(text=daily_index(d))
        if k == "-index-headers.html":
            acc = url.rsplit("/", 1)[-1].replace("-index-headers.html", "")
            _d, form, _cik, accepted, items, ex = FILINGS[acc]
            return Resp(text=header_text(acc, form, accepted, items, ex, escaped=acc.startswith("0000000102")))
        if k == "/v2/stocks/bars":
            assert params.get("adjustment") == "raw", params
            if self.alpaca_403:
                return Resp({"message": "forbidden"}, status=403)
            syms = params["symbols"].split(",")
            return Resp({"bars": {s: bar_rows(D, NAMES[s][4]) for s in syms if s in NAMES}})
        raise AssertionError(url)


def run(get=None, store=None, **kw):
    store = store if store is not None else MemoryStore()
    get = get or Market()
    kw.setdefault("budget_s", 10_000)
    out = E.run(get, store, start_day=D, end_day=D, pace_s=0, sec_interval_s=0, sleep=lambda s: None, **kw)
    return store, get, out


@pytest.fixture(scope="module")
def done():
    store, get, out = run()
    return store, get, out, store.get(E.TABLE, E.VERSION)


# ------------------------------------------------------------------------- EDGAR parsing --
def test_header_parsing_reads_acceptance_items_and_exhibits():
    h = E.parse_header(header_text("0000000101-26-000001", "8-K", "20260916183000", ["1.01", "9.01"], PR))
    assert h["ok"] and h["form"] == "8-K" and h["accepted"] == "2026-09-16T18:30:00"
    assert h["items"] == ["1.01", "9.01"] and h["items_unknown"] == []
    assert h["exhibits"] == [["8-K", "8-K"], ["EX-99.1", "PRESS RELEASE"]]
    esc = E.parse_header(header_text("x", "8-K", "20260917070000", ["8.01"], PR, escaped=True))
    assert esc["accepted"] == "2026-09-17T07:00:00" and esc["items"] == ["8.01"] and esc["exhibits"][1][0] == "EX-99.1"
    sgml = E.parse_header("<SEC-HEADER>\n<ACCEPTANCE-DATETIME>20260917070000\nCONFORMED SUBMISSION TYPE: 8-K\n"
                          "<ITEMS>2.03\n<ITEMS>8.01\n</SEC-HEADER>")
    assert sgml["items"] == ["2.03", "8.01"]
    assert E.item_number("Creation of a Direct Financial Obligation or an Obligation under an Off-Balance") == "2.03"
    assert E.item_number("Triggering Events That Accelerate or Increase a Direct Financial Obligation") == "2.04"
    assert E.item_number("Notice of Delisting or Failure to Satisfy a Continued Listing Rule or Standard; "
                         "Transfer of Listing") == "3.01"
    assert not E.parse_header("<html>404</html>")["ok"]


def test_amendments_never_qualify():
    h = E.parse_header(header_text("a", "8-K/A", "20260917070000", ["1.01"], PR))
    c = E.classify(h)
    assert c["amendment"] and c["item_rule"] and not c["eligible"]


def test_daily_index_parsing_keeps_only_8k_rows():
    rows = E.parse_daily_index(daily_index(D))
    assert ["8-K", 101, "0000000101-26-000001"] in rows and ["8-K/A", 105, "0000000105-26-000001"] in rows
    assert all(r[0] in ("8-K", "8-K/A") for r in rows) and not any(r[1] == 999 for r in rows)
    assert E.daily_index_url(D).endswith("/daily-index/2026/QTR3/form.20260917.idx")


def test_the_acceptance_window_is_prior_session_16_00_to_09_00_inclusive():
    friday, monday = date(2026, 9, 11), date(2026, 9, 14)
    assert E.in_window("2026-09-11T16:00:00", monday, friday)            # start inclusive
    assert E.in_window("2026-09-14T09:00:00", monday, friday)            # end inclusive
    assert E.in_window("2026-09-12T10:00:00", monday, friday)            # the weekend in between
    assert not E.in_window("2026-09-11T15:59:59", monday, friday)
    assert not E.in_window("2026-09-14T09:00:01", monday, friday)
    assert not E.in_window(None, monday, friday)


@pytest.mark.parametrize("items,exhibits,eligible,dilution", [
    (["1.01"], [], True, False),
    (["8.01"], PR, True, False),
    (["8.01"], [], False, False),                                       # 8.01 needs an EX-99
    (["7.01"], PR, False, False),
    (["1.01", "3.02"], [], False, True),
    (["1.01", "2.03"], [], False, True),
    (["1.01"], [("EX-1.1", "Underwriting Agreement")], False, True),
    (["1.01"], [("EX-10.1", "Securities Purchase Agreement")], False, True),
    (["8.01"], PR + [("EX-10.2", "Placement Agency Agreement")], False, True),
    (["1.01"], [("EX-10.1", "At-The-Market Sales Agreement")], False, True),
    (["1.01"], [("EX-10.3", "Registration Rights Agreement")], False, True),
    (["1.01"], [("EX-10.1", "Registered Direct Offering Letter")], False, True),
])
def test_items_and_dilution_exclusions(items, exhibits, eligible, dilution):
    h = E.parse_header(header_text("a", "8-K", "20260917070000", items, exhibits)) if all(
        i in ITEM_TITLE for i in items) else {"ok": True, "form": "8-K", "items": items,
                                              "exhibits": [list(x) for x in exhibits]}
    c = E.classify(h)
    assert c["eligible"] is eligible and bool(c["dilution"]) is dilution


# ------------------------------------------------------------------------- order levels --
def test_levels_are_the_registered_ones():
    lv = E.levels(10.0)
    assert lv == {"prev_close": 10.0, "trigger": 10.40, "limit": 10.56, "target": 10.92, "stop": 10.09,
                  "shares": 94}                                         # floor(1000 / 10.56)
    f = E.forecast("X", D, lv)
    assert (f.issued_at, f.window_start, f.entry_expiry, f.time_exit) == (
        ts(D, 9, 25), ts(D, 9, 30), ts(D, 10, 30), ts(D, 15, 30))
    assert E.SPEC.break_even_win_rate() == pytest.approx(0.375)


def bars(path):
    from edge.backtest import _bars
    return _bars(bar_rows(D, path))


def test_entry_fill_no_fill_and_resolution():
    f = E.forecast("X", D, E.levels(10.0))
    win = E.simulate(f, bars("win"), cost_bps=0, complete=True)
    assert win["simulated"] == WIN and win["entry"] == 10.40 and win["exit"] == 10.92
    assert win["pnl_usd"] == pytest.approx(94 * (10.92 - 10.40))
    assert E.simulate(f, bars("lose"), cost_bps=0)["simulated"] == LOSS
    gap = E.simulate(f, bars("gap_above"), cost_bps=0)
    assert gap["simulated"] == NO_FILL and gap["note"] == "opened above the limit"      # not the dip to the limit
    assert E.simulate(f, bars("never"), cost_bps=0, complete=True)["simulated"] == NO_FILL
    rt = E.simulate(f, bars("run_through"), cost_bps=0)
    assert rt["simulated"] == WIN and rt["entry"] == 10.56 and rt["resolver_stricter"]
    late = E.forecast("X", D, E.levels(10.0), delay_s=60)
    assert E.simulate(late, bars("win"), cost_bps=0)["entry"] == 10.42                # opened at 09:31 above trigger


def test_same_bar_target_and_stop_is_a_loss():
    rows = [(ts(D, 9, 30), 10.30, 10.45, 10.28, 10.42, 1), (ts(D, 9, 31), 10.42, 11.0, 10.0, 10.5, 1)]
    rows += [(ts(D, 9, 32) + 60 * i, 10.5, 10.5, 10.5, 10.5, 1) for i in range(400)]
    assert E.simulate(E.forecast("X", D, E.levels(10.0)), rows, cost_bps=10)["simulated"] == LOSS


# ------------------------------------------------------------------------- the full replay --
def test_picks_are_the_eligible_8ks_ranked_and_capped(done):
    store, _get, out, full = done
    assert out["status"] == "complete"
    day = store.get(E.T_DAY, f"{E.VERSION}:{D.isoformat()}")
    assert day["status"] == "ok"
    assert [t["symbol"] for t in day["picks"]] == ["EIGHT", "PRESS", "EDGEA", "EDGEB"]
    assert day["over_cap"] == ["CAPX"]
    by = {t["symbol"]: t for t in day["picks"]}
    assert by["EIGHT"]["cost_10bps"]["simulated"] == WIN and by["PRESS"]["cost_10bps"]["simulated"] == LOSS
    assert by["EDGEA"]["cost_10bps"]["simulated"] == NO_FILL and by["EDGEB"]["cost_10bps"]["simulated"] == NO_FILL
    # EIGHT split 2-for-1 on D: its $20 raw prior close is $10 on D's basis, so the trigger is $10.40
    assert by["EIGHT"]["trigger"] == 10.40 and by["EIGHT"]["split_restated"] == {"raw_prev_close": 20.0,
                                                                                "share_factor": 2.0}
    assert by["EIGHT"]["filings"][0]["accession"] == "0000000101-26-000001"
    assert set(by["EIGHT"]) >= set(E.VARIANTS)
    c = day["counts"]
    assert c["in_window_8k"] == 8 and c["in_window_8ka"] == 1
    assert c["item_rule_met"] == 7 and c["excluded_dilution"] == 2 and c["eligible_filings"] == 5
    assert c["reverse_split_excluded"] == 1 and c["index_8k"] == 10 and c["index_8ka"] == 1


def test_unmapped_multi_ticker_and_filtered_ciks_are_never_fetched(done):
    store, get, _out, full = done
    day = store.get(E.T_DAY, f"{E.VERSION}:{D.isoformat()}")
    assert day["unmapped_ciks"] == [301] and day["multi_ciks"] == [302]
    fetched = {u.rsplit("/", 1)[-1].replace("-index-headers.html", "") for k, u in get.calls
               if k == "-index-headers.html"}
    assert len(fetched) == get.count("-index-headers.html") == 11            # each header once
    for cik in (112, 114, 301, 302):                                         # CHEAP, RSPLT, unmapped, multi
        assert f"0000000{cik}-26-000001" not in fetched
    counts = full["windows"]["full"]["counts"]
    assert counts["unmapped_ciks"] == 1 and counts["multi_ticker_ciks"] == 1


def test_polygon_cik_maps_a_cik_the_sec_list_no_longer_has():
    r = E._Run(Market(), MemoryStore(), start=D, end=D, pace=0, sec_interval=0, budget_s=1e6,
               sleep=lambda s: None, clock=lambda: 0.0)
    r.phase_meta()
    by_cik, source, owner = r.mapping(D)
    assert by_cik[401] == ["POLY"] and source[401] == "polygon" and owner["POLY"] == 401
    assert by_cik[101] == ["EIGHT"] and source[101] == "sec"                 # the warrant is not CS
    assert by_cik[302] == ["MULA", "MULB"] and "MULA" not in owner


def test_twins_are_untouched_names_that_hit_the_trigger(done):
    store, *_ = done
    day = store.get(E.T_DAY, f"{E.VERSION}:{D.isoformat()}")
    tw = {t["symbol"]: t for t in day["twins"]}
    # EARLY and LATE filed just outside the window: twins. 8-K/A, excluded and ineligible 8-Ks: not.
    assert list(tw) == ["TWINW", "TWINL", "EARLY", "LATE", "GAPUP", "RUNTH"]
    assert all(t["arm"] == "no_8k_twin" for t in tw.values())
    assert tw["TWINW"]["cost_10bps"]["simulated"] == WIN and tw["TWINL"]["cost_10bps"]["simulated"] == LOSS
    assert tw["GAPUP"]["cost_10bps"]["simulated"] == NO_FILL
    assert tw["RUNTH"]["cost_10bps"]["resolver_stricter"]
    assert "NOTRIG" not in tw and "POLY" not in tw                           # never hit +4%


def test_the_twin_set_is_capped_by_dollar_volume(monkeypatch):
    monkeypatch.setattr(E, "TWIN_CAP", 2)
    store, _get, _out = run()
    day = store.get(E.T_DAY, f"{E.VERSION}:{D.isoformat()}")
    assert [t["symbol"] for t in day["twins"]] == ["TWINW", "TWINL"] and day["counts"]["twins_cap_hit"]


def test_report_has_both_windows_costs_and_a_gate(done):
    _store, _get, _out, full = done
    assert full["version"] == "edgar_8k_breakout_backtest_v1" and full["price_basis"] == "raw_as_traded"
    assert set(full["windows"]) == {"preregistered", "full"}
    arm = full["windows"]["full"]["arms"]["edgar_8k"]
    assert (arm["fills"], arm["wins"], arm["losses"], arm["no_fills"]) == (2, 1, 1, 2)
    assert arm["expectancy_usd_25bps"] < arm["expectancy_usd_10bps"]
    assert set(arm["variants"]) == {"cost_10bps", "cost_25bps", "delay_1min_10bps", "delay_1min_25bps"}
    g = full["windows"]["full"]["gate"]
    assert g["verdict"] == "FAIL" and any("Wilson" in r for r in g["reasons"])
    assert full["data_availability"]["polygon_daily_first_session"] and \
        full["data_availability"]["alpaca_minute_first_session"] == D.isoformat()
    assert any("NOT the forward record" in x for x in full["limits"])


def test_gate_verdict():
    def rows(n_win, n_loss, day="2026-09-17", win=40.0, loss=-30.0):
        out = []
        for i, pnl in enumerate([win] * n_win + [loss] * n_loss):
            res = {"simulated": WIN if pnl > 0 else LOSS, "pnl_usd": pnl}
            out.append({"day": f"2026-09-{1 + i % 28:02d}", **{k: dict(res) for k in E.VARIANTS}})
        return out
    good, twins = rows(60, 40), rows(40, 60)
    g = E.gate(good, twins)
    assert g["verdict"] == "PASS" and g["reasons"] == []
    lo, hi = g["expectancy_diff_usd_10bps"]["ci95_session_clustered"]
    assert lo <= g["expectancy_diff_usd_10bps"]["diff"] <= hi
    assert E.gate(good, rows(80, 20))["verdict"] == "FAIL"                          # twins better
    assert "does not beat" in " ".join(E.gate(good, rows(80, 20))["reasons"])
    weak = E.gate(rows(3, 7), twins)
    assert weak["verdict"] == "FAIL" and any("Wilson lower bound" in r for r in weak["reasons"])
    assert "no filled trades" in E.gate([], twins)["reasons"]
    assert "no twin fills" in " ".join(E.gate(good, [])["reasons"])


def test_it_resumes_across_ticks_without_refetching():
    get, store = Market(), MemoryStore()
    clock = {"t": 0.0}

    class Ticking(Market):
        def __call__(self, url, params=None, headers=None, timeout=None):
            clock["t"] += 1.0
            return get(url, params, headers, timeout)

    outs = []
    for _ in range(40):
        out = E.run(Ticking(), store, start_day=D, end_day=D, pace_s=0, sec_interval_s=0, budget_s=6,
                    sleep=lambda s: None, clock=lambda: clock["t"])
        outs.append(out["status"])
        if out["status"] != "in_progress":
            break
    assert outs[0] == "in_progress" and outs[-1] == "complete" and len(outs) > 3
    # every session, index and header was fetched exactly once across all the ticks
    grouped = [u for k, u in get.calls if k == "/v2/aggs/grouped/"]
    assert len(grouped) == len(set(grouped)) == 21
    heads = [u for k, u in get.calls if k == "-index-headers.html"]
    assert len(heads) == len(set(heads)) == 11
    idx = [u for k, u in get.calls if k == "/daily-index/"]
    assert len(idx) == len(set(idx))
    assert get.count("company_tickers.json") == 1 and get.count("/v3/reference/splits") == 1
    prog = store.get(E.T_META, E.VERSION + ":progress")
    assert prog["headers_needed"] == 11 and prog["headers_fetched"] == 11
    # once complete: once per version
    assert E.run(Market(), store, start_day=D, end_day=D, pace_s=0)["status"] == "already_run"


def test_a_second_call_continues_from_the_cache():
    store, get, out = run()
    assert out["status"] == "complete"
    store._t[E.TABLE].pop(E.VERSION)                         # as if the last tick died before the report
    get2 = Market()
    _s, _g, out2 = run(get=get2, store=store)
    assert out2["status"] == "complete" and get2.calls == []


def test_unavailable_history_is_reported_not_fatal():
    first = E.sessions(D, D)[0]
    store, _get, out = run(get=Market(daily_403={first}))
    assert out["status"] == "complete"
    av = out["data_availability"]
    assert av["polygon_daily_unavailable_sessions"] == 1 and av["no_data_sessions"] == 1
    assert av["first_decided_session"] is None
    assert store.get(E.T_DAY, f"{E.VERSION}:{D.isoformat()}")["status"] == "no_data"


def test_minute_data_refused_is_reported():
    store, _get, out = run(get=Market(alpaca_403=True))
    assert out["status"] == "complete"
    assert out["data_availability"]["alpaca_minute_unavailable_sessions"] == 1
    assert out["data_availability"]["alpaca_minute_first_session"] is None


def test_start_date_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv(E.START_ENV, raising=False)
    assert E._env_start() == date(2024, 10, 7)
    monkeypatch.setenv(E.START_ENV, "2025-03-03")
    assert E._env_start() == date(2025, 3, 3)
    monkeypatch.setenv(E.START_ENV, "garbage")
    assert E._env_start() == date(2024, 10, 7)


def test_sessions_cover_pre_2026_holidays():
    s = E.sessions(date(2025, 11, 24), date(2025, 12, 31), warm=0)
    assert date(2025, 11, 27) not in s and date(2025, 12, 25) not in s and date(2025, 11, 28) in s
    assert E.early_close(date(2025, 11, 28)) and not E.early_close(date(2025, 12, 1))


def test_the_report_reaches_the_backtest_view(done):
    store, *_ = done
    store.put("edge_backtest", "v", {"version": "v", "completed_at": 1})
    view = R.view(store, "backtest")
    e = view["edgar_8k_breakout"]
    assert e["version"] == E.VERSION and "trades_prereg" not in e and "picks_full" not in e
    assert e["windows"]["full"]["gate"]["verdict"] in ("PASS", "FAIL")
