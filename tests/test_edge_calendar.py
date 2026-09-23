"""Market calendar: holidays and early closes, from Alpaca when stored, else the NYSE table."""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

from edge import calendar as CAL
from edge import pipeline as P
from edge.ledger import Ledger, MemoryStore

ET = ZoneInfo("America/New_York")


def ts(d, hh, mm):
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET).timestamp())


def test_builtin_table_knows_thanksgiving_christmas_and_the_half_days():
    assert CAL.session(date(2026, 11, 26))["trading"] is False          # Thanksgiving
    assert CAL.session(date(2026, 12, 25))["trading"] is False          # Christmas
    assert CAL.session(date(2027, 1, 1))["trading"] is False
    half = CAL.session(date(2026, 11, 27))
    assert half["trading"] and half["early_close"] and half["close"] == (13, 0)
    assert CAL.session(date(2026, 9, 23)) == {"trading": True, "open": (9, 30), "close": (16, 0),
                                               "early_close": False, "source": "builtin"}
    assert P.previous_trading_day(date(2026, 11, 27)) == date(2026, 11, 25)


def test_a_stored_alpaca_calendar_wins_and_goes_stale():
    store, now = MemoryStore(), ts(date(2026, 9, 23), 6, 0)
    store.put("edge_calendar", "current", {"start": "2026-09-23", "end": "2027-09-23", "fetched_at": now,
                                           "days": {"2026-09-23": {"open": "09:30", "close": "13:00"}}})
    s = CAL.session(date(2026, 9, 23), store, now)
    assert s["early_close"] and s["source"] == "alpaca"
    assert CAL.session(date(2026, 9, 24), store, now)["trading"] is False   # not in Alpaca's list
    stale = now + 9 * 86400
    assert CAL.session(date(2026, 9, 24), store, stale)["source"] == "builtin"


def test_refresh_parses_alpaca_and_keeps_the_old_calendar_on_failure(monkeypatch):
    monkeypatch.setenv("ALPACA_KEY_ID", "i")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    store, calls = MemoryStore(), []

    def get(url, headers=None, params=None, timeout=None):
        calls.append(url)
        return NS(status_code=200, json=lambda: [{"date": "2026-11-27", "open": "09:30", "close": "13:00"},
                                                 {"date": "2026-11-30", "open": "09:30", "close": "16:00"}])

    out = CAL.refresh(NS(get=get), store, today=date(2026, 9, 23), now=1)
    assert out == {"status": "ok", "trading_days": 2, "early_closes": ["2026-11-27"]}
    assert calls[0].startswith("https://paper-api.alpaca.markets/v2/calendar")
    bad = NS(get=lambda *a, **k: NS(status_code=401, json=lambda: {}))
    assert CAL.refresh(bad, store, today=date(2026, 9, 23), now=2)["status"] == "error"
    assert store.get("edge_calendar", "current")["fetched_at"] == 1          # kept


def test_an_early_close_day_issues_no_forecasts_and_says_why():
    day = date(2026, 11, 27)
    lg = Ledger(MemoryStore())
    out = P.morning_card(lambda *a, **k: (_ for _ in ()).throw(AssertionError("no data needed")), lg,
                         now=ts(day, 9, 10))
    assert out["status"] == "early_close" and "15:30" in out["note"]
    card = lg.store.get("edge_cards", day.isoformat())
    assert card["forecasts"] == [] and "early close" in card["health_banner"]
    run = P.run(lambda *a, **k: (_ for _ in ()).throw(AssertionError("no intraday")), lg,
                now=ts(day, 10, 0))
    assert "intraday" not in run and "notify_duty" not in run


def test_holidays_do_nothing_at_all():
    out = P.run(lambda *a, **k: (_ for _ in ()).throw(AssertionError("closed")), Ledger(MemoryStore()),
                now=ts(date(2026, 11, 26), 9, 10))
    assert out["status"] == "market_closed"


def test_the_paper_account_and_telegram_probes(monkeypatch):
    from edge import notify, paper
    monkeypatch.setenv("ALPACA_KEY_ID", "i")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    seen = []

    def acct(url, headers=None, timeout=None, params=None):
        seen.append(url)
        return NS(status_code=200, json=lambda: {"status": "ACTIVE", "account_number": "PA123SECRET",
                                                 "trading_blocked": False})

    p = paper.probe(NS(get=acct))
    assert p.status == "OK" and "PA123SECRET" not in p.note and seen[0].startswith("https://paper-api.")
    blocked = paper.probe(NS(get=lambda *a, **k: NS(status_code=200, json=lambda: {"status": "ACTIVE",
                                                                                   "trading_blocked": True})))
    assert blocked.status == "ERROR" and "trading_blocked" in blocked.note
    assert paper.probe(NS(get=lambda *a, **k: NS(status_code=401, json=lambda: {}))).status == "NOT_AUTHORIZED"

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert notify.probe(NS()).status == "NO_KEY"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    ok = notify.probe(NS(get=lambda url, params=None, timeout=None: NS(status_code=200, json=lambda: {"ok": True})))
    assert ok.status == "OK" and "nothing sent" in ok.note
    bad = notify.probe(NS(get=lambda *a, **k: NS(status_code=400, json=lambda: {"description": "chat not found"})))
    assert bad.status == "ERROR" and "chat not found" in bad.note and "SECRET" not in bad.note

    def boom(url, **k):
        raise ConnectionError(f"failed {url}")          # the token is in the URL
    assert "SECRET" not in notify.probe(NS(get=boom)).note
