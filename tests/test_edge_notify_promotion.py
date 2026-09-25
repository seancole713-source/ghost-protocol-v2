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
    lg.store.put("edge_cards", "2026-09-23", {"day": "2026-09-23", "forecasts": ["SHOP"]})
    P.run(None, lg, now=ts(10, 26), notifier=tg)
    P.run(None, lg, now=ts(15, 27), notifier=tg)
    texts = _duties(tg)
    assert any("9:30am CT" in t for t in texts) and any("2:30pm CT" in t for t in texts)
    P.run(None, lg, now=ts(10, 28), notifier=tg)
    assert len(_duties(tg)) == 2


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



def _duties(tg):
    return [j["text"] for _, j in tg.sent if j["text"].startswith("Gap-and-Go v1:")]


@pytest.mark.parametrize("card", [{"day": "2026-09-23", "forecasts": []}, None])
def test_duty_reminders_go_out_on_a_trading_day_with_no_ghost_forecast(card):
    """The operator's own card can hold a name when Ghost's shadow card has none (or no card
    was written): both duties go out every regular trading day, and each says when to skip."""
    lg, tg = Ledger(MemoryStore()), TG()
    if card:
        lg.store.put("edge_cards", "2026-09-23", card)
    P.run(None, lg, now=ts(10, 26), notifier=tg)
    P.run(None, lg, now=ts(15, 27), notifier=tg)
    duties = _duties(tg)
    assert duties == [N.DUTY_1030, N.DUTY_1530]
    assert "9:30am CT" in duties[0] and "2:30pm CT" in duties[1]
    assert all("Skip if you placed nothing today" in d for d in duties)


def test_no_duty_reminders_on_an_early_close_day():
    lg, tg = Ledger(MemoryStore()), TG()
    half = lambda hh, mm: int(datetime(2026, 11, 27, hh, mm, tzinfo=ET).timestamp())   # noqa: E731
    lg.store.put("edge_cards", "2026-11-27", {"day": "2026-11-27", "forecasts": [], "status": "early_close"})
    P.run(None, lg, now=half(10, 26), notifier=tg)
    P.run(None, lg, now=half(15, 27), notifier=tg)
    assert _duties(tg) == []


# ---------------------------------------------------------------- phone card --

CARD = {"day": "2026-09-23", "issued_at": ts(9, 10), "status": None,
        "forecasts": ["SHOP"], "baseline_forecasts": ["SHOP", "USAR"],
        "trades": [{"symbol": "SHOP", "ref_price": 146.71, "prev_close": 137.92,
                    "catalyst": "Shopify announces partnership with Meta for agent checkout in a multi-year "
                                "deal covering every merchant on the platform",
                    "entry_trigger": 148.18, "entry_limit": 149.64, "target": 155.59, "stop": 143.73,
                    "shares": 6, "entry_expiry": ts(10, 30), "time_exit": ts(15, 30)}],
        "top10_ranked": [{"rank": 1, "symbol": "SHOP", "score": 81.0}, {"rank": 2, "symbol": "USAR", "score": 40.2}],
        "health_banner": "1 setup. Coverage healthy.", "source_errors": {},
        "coverage_note": "4/5 candidates had a fresh IEX premarket price"}


def test_the_phone_card_is_a_trade_with_levels_size_risk_and_ct_times():
    t = N.card_text(CARD)
    assert "SHOP" in t and "ref $146.71" in t and "gap +6.4%" in t
    assert "BUY STOP $148.18, limit $149.64" in t and "target $155.59" in t and "stop $143.73" in t
    assert "6 shares" in t and "max loss ~$35 at stop" in t           # (149.64 - 143.73) * 6
    assert "Entry expires 9:30am CT" in t and "Exit everything by 2:30pm CT" in t
    assert "ET" not in t.replace("Ghost", "")                         # the operator reads CT only
    cat = next(line for line in t.splitlines() if "Catalyst:" in line)
    assert len(cat.split("Catalyst: ", 1)[1]) <= 80 and cat.endswith("...")
    assert "Baseline (no catalyst check): SHOP, USAR" in t and "1.SHOP 81, 2.USAR 40" in t
    assert "DATA WARNING" not in t and "No trade today" not in t
    assert len(t) <= 1500


def test_a_card_without_stored_trades_rebuilds_the_same_frozen_levels():
    card = {k: v for k, v in CARD.items() if k != "trades"}
    card["rows"] = [{"symbol": "SHOP", "ref_price": 146.71, "prev_close": 137.92, "catalyst": "x"}]
    t = N.card_text(card)
    assert "BUY STOP $148.18, limit $149.64" in t and "target $155.59" in t and "stop $143.73" in t


def test_a_no_trade_day_says_so_plainly():
    t = N.card_text({"day": "2026-09-23", "forecasts": [], "baseline_forecasts": []})
    assert "No trade today" in t and "Baseline (no catalyst check): none" in t
    early = N.card_text({"day": "2026-11-27", "forecasts": [], "status": "early_close"})
    assert "No trade today" in early and "early close" in early


def test_a_card_on_broken_data_leads_with_a_warning():
    card = dict(CARD, health_banner="Trading signals paused. Market-data coverage incomplete: news",
                source_errors={"news": "HTTPError: 500"})
    first = N.card_text(card).splitlines()[0]
    assert first.startswith("DATA WARNING:") and "paused" in first and "news" in first


def test_the_issued_card_carries_its_levels_to_the_phone():
    from test_edge_pipeline import FakeAlpaca, ts as pts
    lg, tg = Ledger(MemoryStore()), TG()
    P.run(FakeAlpaca(), lg, now=pts(9, 10), notifier=tg)
    card = lg.store.get("edge_cards", "2026-09-23")
    f = lg.store.get("forecasts", card["rows"][[r["symbol"] for r in card["rows"]].index("SHOP")]["forecast_id"])
    assert card["trades"][0]["entry_trigger"] == f["entry_trigger"] and card["top10_ranked"]
    text = next(j["text"] for _, j in tg.sent if "Ghost Gap-and-Go card" in j["text"])
    assert f"BUY STOP ${f['entry_trigger']:,.2f}" in text and "9:30am CT" in text


# ---------------------------------------------------------------- problems --

def _problems(tg):
    return [j["text"] for _, j in tg.sent if j["text"].startswith("PROBLEM")]


def test_no_card_by_928_sends_one_problem():
    lg, tg = Ledger(MemoryStore()), TG()
    P.run(None, lg, now=ts(9, 30), notifier=tg)
    P.run(None, lg, now=ts(9, 35), notifier=tg)
    assert _problems(tg) == [N.PROBLEM_NO_CARD]
    assert "9:28 ET/8:28 CT" in N.PROBLEM_NO_CARD


def test_a_failing_step_sends_one_problem_per_step_per_day(monkeypatch):
    from edge import intraday as I
    lg, tg = Ledger(MemoryStore()), TG()
    lg.store.put("edge_cards", "2026-09-23", {"day": "2026-09-23", "forecasts": []})

    def boom(*a, **k):
        raise ConnectionError("https://api.polygon.io/v2/x?apiKey=SUPERSECRET&x=1 reset")
    monkeypatch.setattr(I, "tick", boom)
    P.run(None, lg, now=ts(11, 0), notifier=tg)
    P.run(None, lg, now=ts(11, 5), notifier=tg)
    probs = _problems(tg)
    assert len(probs) == 1 and probs[0].startswith("PROBLEM: intraday failed (10:00am CT): ConnectionError")
    assert "SUPERSECRET" not in probs[0] and "apiKey=***" in probs[0]
    assert lg.store.get("edge_notify", "2026-09-23|problem_intraday")


def test_no_problem_messages_without_a_notifier(monkeypatch):
    from edge import intraday as I
    lg = Ledger(MemoryStore())
    monkeypatch.setattr(I, "tick", lambda *a, **k: 1 / 0)
    out = P.run(None, lg, now=ts(11, 0))
    assert out["intraday"]["status"] == "error" and not any(k.startswith("notify") for k in out)
    assert lg.store.scan("edge_notify") == []


def test_grading_not_done_by_1700_sends_one_problem(monkeypatch):
    lg, tg = Ledger(MemoryStore()), TG()
    P.run(None, lg, now=ts(17, 0), notifier=tg)      # nothing to grade: resolved, no card -- quiet
    assert _problems(tg) == []

    def boom(*a, **k):
        raise TimeoutError("minute bars")
    monkeypatch.setattr(P, "resolve_day", boom)
    P.run(None, lg, now=ts(16, 30), notifier=tg)     # before 17:00 ET: a retry may still fix it
    assert _problems(tg) == []
    P.run(None, lg, now=ts(17, 5), notifier=tg)
    P.run(None, lg, now=ts(17, 10), notifier=tg)
    probs = _problems(tg)
    assert len(probs) == 1 and "grading not done by 4:00pm CT" in probs[0] and "resolve=error" in probs[0]


def test_a_paper_stage_needs_the_rule_to_have_traded_too():
    from edge import promotion as PR
    rep = {"break_even": 0.375, "records": {
        "simulated": {"filled": 0, "wins": 0, "win_rate": None},
        "actual": {"filled": 1, "wins": 1, "expectancy_usd": {"mean": 62.2}}}}
    base = {"records": {"simulated": {"win_rate": 0.3}}}
    assert PR.evaluate(rep, baseline=base, sessions=1, by_day={}, n_candidates=1)["stage"] == "shadow"


def test_retirement_needs_enough_trades_and_the_whole_interval_below_break_even():
    """Audit F09: a rule far below break-even must not run "undecided" forever; retirement is
    written down beforehand (hashed) and needs >= 30 simulated trades, never a handful."""
    from edge import promotion as PR

    def rep(wins, n):
        from edge import stats
        lo, hi = stats.wilson(wins, n)
        return {"break_even": 0.375, "feed_regime": "iex",
                "records": {"simulated": {"filled": n, "wins": wins, "win_rate_ci": [lo, hi]},
                            "actual": {"expectancy_usd": {"mean": -6.3}}}}

    assert PR.retirement(rep(0, 5))["retire"] is False                     # 0/5: too few to judge
    r = PR.retirement(rep(16, 68))                                          # the backtest's 23.5%
    assert r["retire"] is True and "whole interval below break-even" in r["why"]
    assert PR.retirement(rep(25, 60))["retire"] is False                    # interval reaches 37.5%
    assert r["retirement_hash"] == PR.RETIREMENT_HASH and len(PR.RETIREMENT_HASH) == 16


def test_the_card_states_the_rules_own_backtest():
    from edge import notify as N
    card = {"day": "2026-09-28", "forecasts": [], "baseline_forecasts": [],
            "backtest_note": "Rule's own backtest 2026-07-01..2026-09-24: 16/68 wins (24%) vs 37.5% needed"}
    assert "Rule's own backtest" in N.card_text(card)
