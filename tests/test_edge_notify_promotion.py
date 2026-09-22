"""Phone messages and the promotion gate."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from edge import notify as N, pipeline as P, promotion as PR
from edge.ledger import Ledger, MemoryStore

ET = ZoneInfo("America/New_York")


def ts(hh, mm, d=23):
    return int(datetime(2026, 9, d, hh, mm, tzinfo=ET).timestamp())


class TG:
    def __init__(self, code=200):
        self.sent, self.code = [], code

    def post(self, url, json=None, timeout=None):
        self.sent.append((url, json))

        class R:
            status_code = self.code
        return R()


@pytest.fixture(autouse=True)
def tg_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "SECRET")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")


def test_each_message_is_sent_once_per_day():
    store, tg = MemoryStore(), TG()
    assert N.once(tg, store, day="2026-09-23", kind="duty_1030", text="x")["status"] == "sent"
    assert N.once(tg, store, day="2026-09-23", kind="duty_1030", text="x")["status"] == "already_sent"
    assert len(tg.sent) == 1


def test_a_failed_send_is_retried_next_tick_not_marked_done():
    store = MemoryStore()
    assert N.once(TG(code=502), store, day="2026-09-23", kind="card", text="x")["status"] == "error"
    assert N.once(TG(), store, day="2026-09-23", kind="card", text="x")["status"] == "sent"


def test_no_config_sends_nothing(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    assert N.send(TG(), "x")["status"] == "no_telegram_config"


def test_the_duty_reminders_fire_at_their_times():
    lg, tg = Ledger(MemoryStore()), TG()
    P.run(None, lg, now=ts(10, 26), notifier=tg)
    P.run(None, lg, now=ts(15, 27), notifier=tg)
    texts = [j["text"] for _, j in tg.sent]
    assert any("10:30" in t for t in texts) and any("3:30" in t for t in texts)
    P.run(None, lg, now=ts(10, 28), notifier=tg)
    assert len(tg.sent) == 2


def test_no_messages_on_a_weekend():
    tg = TG()
    P.run(None, Ledger(MemoryStore()), now=ts(10, 26, d=26), notifier=tg)
    assert tg.sent == []


def test_graded_text_says_what_it_is():
    t = N.graded_text("2026-09-23", {"gap_and_go_auto@v1:SHOP": {"simulated": "WIN", "pnl_usd": 48.0}})
    assert "SHOP [gap_and_go_auto] WIN +48.00" in t and "simulated" in t


# ---------------------------------------------------------------- promotion --

def rep(filled, wins, *, paper_filled=0, paper_mean=None, win_rate=None):
    return {"break_even": 0.375, "records": {
        "simulated": {"filled": filled, "wins": wins, "win_rate": win_rate if win_rate is not None else (wins / filled if filled else None)},
        "actual": {"filled": paper_filled, "expectancy_usd": {"mean": paper_mean}},
    }}


def test_a_young_experiment_lists_everything_it_still_needs():
    out = PR.evaluate(rep(3, 2), baseline=rep(5, 2), sessions=2, by_day={"d": [True, True, False]}, n_candidates=1)
    assert out["stage"] == "shadow" and len(out["unmet"]) >= 4
    assert out["live"].startswith("never automatic")


def test_a_strong_record_that_does_not_beat_the_baseline_is_not_proposable():
    days = {f"d{i}": [True, True, False] for i in range(60)}        # 66.7% over 180 trades, 60 days
    strong = rep(180, 120, paper_filled=40, paper_mean=12.0)
    base = rep(180, 125)                                            # baseline wins MORE
    out = PR.evaluate(strong, baseline=base, sessions=60, by_day=days, n_candidates=1)
    assert out["unmet"] == ["does not beat the no-catalyst baseline"]


def test_a_record_that_clears_every_bar_is_only_proposable_never_live():
    days = {f"d{i}": [True, True, False] for i in range(60)}
    out = PR.evaluate(rep(180, 120, paper_filled=40, paper_mean=12.0), baseline=rep(180, 90),
                      sessions=60, by_day=days, n_candidates=2)
    assert out["stage"] == "proposable" and out["unmet"] == []
    assert "operator" in out["live"]


def test_criteria_are_hashed_so_a_lowered_bar_is_visible():
    assert len(PR.CRITERIA_HASH) == 16
