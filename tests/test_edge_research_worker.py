"""The research worker: cited claims before the card, a reviewer, a hard daily cap."""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

from edge import research as RS, research_worker as W
from edge.ledger import MemoryStore

ET = ZoneInfo("America/New_York")


def ts(hh, mm):
    return int(datetime(2026, 9, 23, hh, mm, tzinfo=ET).timestamp())


def reply(text, stop="end_turn", tokens=(20_000, 1_500), searches=3):
    return NS(stop_reason=stop, content=[NS(type="text", text=text)],
              usage=NS(input_tokens=tokens[0], output_tokens=tokens[1], cache_creation_input_tokens=0,
                       cache_read_input_tokens=0, server_tool_use=NS(web_search_requests=searches)))


AUTHOR_OK = json.dumps({"claims": [{
    "kind": "contract", "statement": "Shopify announced Meta's Muse agent can check out via Shop Pay.",
    "value": None, "unit": None,
    "citations": [{"url": "https://news.shopify.com/muse", "quote": "Muse can now check out...", "published_at": "2026-09-21T08:00:00-04:00"},
                  {"url": "https://www.reuters.com/x", "quote": "Shopify said...", "published_at": "2026-09-21T09:00:00-04:00"}],
    "unknowns": []}], "unknowns": []})
REVIEW_OK = json.dumps({"entity_ok": True, "contradictions": [], "dilution_found": False, "stale": False, "notes": ""})


class Client:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return self.replies.pop(0)


def test_a_reviewed_cited_claim_becomes_usable_and_costs_are_counted():
    store, c = MemoryStore(), Client([reply(AUTHOR_OK), reply(REVIEW_OK)])
    out = W.research_symbol(c, store, symbol="SHOP", day="2026-09-23", now=ts(8, 35))
    assert out["status"] == "researched" and out["usable"] == 1
    rec = store.get("edge_research", "2026-09-23|SHOP")
    assert rec["claims"][0]["status"] == RS.VERIFIED            # two independent domains
    assert "correlated" in rec["independence"]
    assert store.get("edge_research_budget", "2026-09-23")["spent_usd"] == pytest.approx(out["cost_usd"])
    kw = c.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default"
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["tools"][0]["type"] == "web_search_20260209"
    assert W.research_symbol(c, store, symbol="SHOP", day="2026-09-23", now=ts(8, 40))["status"] == "already_researched"


def test_a_probability_claim_is_quarantined():
    bad = json.loads(AUTHOR_OK)
    bad["claims"][0]["statement"] = "There is a 70% chance Shopify rises today."
    store = MemoryStore()
    W.research_symbol(Client([reply(json.dumps(bad)), reply(REVIEW_OK)]), store,
                      symbol="SHOP", day="2026-09-23", now=ts(8, 35))
    assert store.get("edge_research", "2026-09-23|SHOP")["claims"][0]["status"] == RS.QUARANTINED


def test_the_reviewer_catching_a_namesake_quarantines_the_claim():
    store = MemoryStore()
    review = json.dumps({"entity_ok": False, "contradictions": [], "dilution_found": False, "stale": False, "notes": "about Shop Apotheke"})
    W.research_symbol(Client([reply(AUTHOR_OK), reply(review)]), store, symbol="SHOP", day="2026-09-23", now=ts(8, 35))
    assert store.get("edge_research", "2026-09-23|SHOP")["claims"][0]["status"] == RS.QUARANTINED


def test_a_refusal_yields_no_claims_and_no_review_call():
    store, c = MemoryStore(), Client([reply("", stop="refusal")])
    out = W.research_symbol(c, store, symbol="SHOP", day="2026-09-23", now=ts(8, 35))
    assert out["claims"] == 0 and len(c.calls) == 1
    assert store.get("edge_research", "2026-09-23|SHOP")["author_stop"] == "refusal"


def test_a_server_tool_pause_is_continued():
    c = Client([reply("", stop="pause_turn"), reply(AUTHOR_OK), reply(REVIEW_OK)])
    out = W.research_symbol(c, MemoryStore(), symbol="SHOP", day="2026-09-23", now=ts(8, 35))
    assert out["usable"] == 1 and len(c.calls) == 3
    assert c.calls[1]["messages"][-1]["role"] == "assistant"


def test_the_daily_cap_stops_spending(monkeypatch):
    monkeypatch.setenv("EDGE_RESEARCH_DAILY_USD", "0.10")
    store = MemoryStore()
    W.research_symbol(Client([reply(AUTHOR_OK), reply(REVIEW_OK)]), store, symbol="SHOP", day="2026-09-23", now=ts(8, 35))
    c = Client([])
    assert W.research_symbol(c, store, symbol="USAR", day="2026-09-23", now=ts(8, 40))["status"] == "budget_exhausted"
    assert c.calls == []


def test_research_made_after_the_card_is_never_used():
    store = MemoryStore()
    W.research_symbol(Client([reply(AUTHOR_OK), reply(REVIEW_OK)]), store, symbol="SHOP", day="2026-09-23", now=ts(9, 20))
    assert W.verdict(store, day="2026-09-23", symbol="SHOP", issued_at=ts(9, 10)) == {"catalyst": None, "dilutive": None}
    assert W.verdict(store, day="2026-09-23", symbol="SHOP", issued_at=ts(9, 25))["catalyst"] is True


def test_off_by_default():
    assert W.enabled() is False


def test_the_card_runs_a_verified_experiment_when_research_is_on(monkeypatch):
    import sys
    sys.path.insert(0, "tests")
    from test_edge_pipeline import FakeAlpaca, ts as pts
    from edge import pipeline as P
    from edge.ledger import Ledger
    monkeypatch.setenv("EDGE_RESEARCH_ENABLED", "1")
    lg = Ledger(MemoryStore())
    W.research_symbol(Client([reply(AUTHOR_OK), reply(REVIEW_OK)]), lg.store, symbol="SHOP", day="2026-09-23", now=pts(8, 40))
    out = P.morning_card(FakeAlpaca(), lg, now=pts(9, 10))
    assert out["forecasts"] == ["SHOP"]
    card = lg.store.get("edge_cards", "2026-09-23")
    assert card["verified_forecasts"] == ["SHOP"]
    rows = {r["symbol"]: r for r in card["rows"]}
    assert rows["USAR"]["verified_missing"] == ["catalyst: not researched before the card",
                                                "not_dilutive: not researched"]


class OAIHttp:
    def __init__(self, verdict=None, models=("gpt-x", "gpt-y"), code=200):
        self.verdict, self.models, self.code, self.posts = verdict, models, code, []

    def get(self, url, headers=None, timeout=None):
        return NS(status_code=self.code, json=lambda: {"data": [{"id": m} for m in self.models]})

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append(json)
        return NS(status_code=200, json=lambda: {"choices": [{"message": {"content": self.verdict}}]})


def test_a_different_family_reviews_when_configured(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("EDGE_OPENAI_MODEL", "gpt-x")
    store, c = MemoryStore(), Client([reply(AUTHOR_OK)])        # no Claude review call needed
    W.research_symbol(c, store, symbol="SHOP", day="2026-09-23", now=ts(8, 35), http=OAIHttp(REVIEW_OK))
    rec = store.get("edge_research", "2026-09-23|SHOP")
    assert rec["reviewer"] == "openai:gpt-x" and len(c.calls) == 1
    assert "different model family" in rec["independence"] and "not independent evidence" in rec["independence"]


def test_without_a_model_name_it_falls_back_to_claude_and_the_probe_lists_choices(monkeypatch):
    from edge import research_openai as RO
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.delenv("EDGE_OPENAI_MODEL", raising=False)
    store, c = MemoryStore(), Client([reply(AUTHOR_OK), reply(REVIEW_OK)])
    W.research_symbol(c, store, symbol="SHOP", day="2026-09-23", now=ts(8, 35), http=OAIHttp(REVIEW_OK))
    assert store.get("edge_research", "2026-09-23|SHOP")["reviewer"] == W.REVIEWER
    p = RO.probe(OAIHttp())
    assert "key can use: gpt-x, gpt-y" in p.note


def test_the_probe_lists_every_chat_model_not_an_alphabetical_prefix(monkeypatch):
    from edge import research_openai as RO
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.delenv("EDGE_OPENAI_MODEL", raising=False)
    ids = ["babbage-002", "chat-latest", "chatgpt-image-latest", "dall-e-3", "davinci-002",
           "gpt-3.5-turbo", "gpt-3.5-turbo-0125", "gpt-3.5-turbo-instruct", "gpt-4.1",
           "gpt-4.1-2025-04-14", "gpt-4o-audio-preview", "gpt-4o-realtime-preview", "gpt-5",
           "gpt-5-codex", "gpt-5-mini", "gpt-5.1", "o3", "o4-mini", "omni-moderation-latest",
           "text-embedding-3-large", "tts-1", "whisper-1"] + [f"ft-{i}" for i in range(120)]
    note = RO.probe(OAIHttp(models=ids)).note
    listed = note.split("chat models this key can use: ")[1].split(", ")
    assert listed == ["chat-latest", "gpt-3.5-turbo", "gpt-4.1", "gpt-5", "gpt-5-mini", "gpt-5.1",
                      "o3", "o4-mini"]
    monkeypatch.setenv("EDGE_OPENAI_MODEL", "gpt-5")    # configured: still shows what else it could use
    p = RO.probe(OAIHttp(models=ids))
    assert p.status == "OK" and p.note.startswith("using gpt-5 (test call answered); ") and "gpt-5.1" in p.note


def test_an_unusable_openai_reply_falls_back_to_claude(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("EDGE_OPENAI_MODEL", "gpt-x")
    store, c = MemoryStore(), Client([reply(AUTHOR_OK), reply(REVIEW_OK)])
    W.research_symbol(c, store, symbol="SHOP", day="2026-09-23", now=ts(8, 35), http=OAIHttp("not json"))
    assert store.get("edge_research", "2026-09-23|SHOP")["reviewer"] == W.REVIEWER and len(c.calls) == 2


def test_the_daily_cap_is_counted_at_opus_5_5_list_prices():
    # 1M input $4, 1M cache read $0.20, 1M output $20, 3 searches $0.03
    usage = NS(input_tokens=1_000_000, cache_creation_input_tokens=0, cache_read_input_tokens=1_000_000,
               output_tokens=1_000_000, server_tool_use=NS(web_search_requests=3))
    assert W.MODEL == "claude-opus-5-5" and W.AUTHOR == "claude-opus-5-5/author"
    assert W.cost_usd(usage) == pytest.approx(4.00 + 0.20 + 20.00 + 0.03)


def test_a_listed_model_that_refuses_a_chat_completion_reads_error(monkeypatch):
    from edge import research_openai as RO
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("EDGE_OPENAI_MODEL", "gpt-x")

    class Refuses(OAIHttp):
        def post(self, url, json=None, headers=None, timeout=None):
            return NS(status_code=400, json=lambda: {"error": {"message": "only supported in v1/responses"}})

    p = RO.probe(Refuses())
    assert p.status == "ERROR" and "only supported in v1/responses" in p.note


def test_the_openai_review_is_bounded_and_counted_in_the_daily_cap(monkeypatch):
    from edge import research_openai as RO
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("EDGE_OPENAI_MODEL", "gpt-6-sol")

    class Billed(OAIHttp):
        def post(self, url, json=None, headers=None, timeout=None):
            self.posts.append(json)
            return NS(status_code=200, json=lambda: {"choices": [{"message": {"content": REVIEW_OK}}],
                                                     "usage": {"prompt_tokens": 100_000, "completion_tokens": 10_000}})

    h = Billed()
    verdict, cost = RO.review(h, symbol="SHOP", claims=[{"kind": "contract"}])
    assert verdict["entity_ok"] is True and cost == pytest.approx(0.2 + 0.1)     # $2 / $10 per 1M
    assert h.posts[0]["max_completion_tokens"] == RO.MAX_OUT
    assert RO.cost_usd("some-new-model", {"completion_tokens": 1_000_000}) == 50.0  # unknown: priciest rate
    store, c = MemoryStore(), Client([reply(AUTHOR_OK)])
    W.research_symbol(c, store, symbol="SHOP", day="2026-09-23", now=ts(8, 35), http=Billed())
    rec = store.get("edge_research", "2026-09-23|SHOP")
    assert rec["reviewer"] == "openai:gpt-6-sol" and rec["cost_usd"] >= 0.3


def test_the_claude_probe_makes_one_real_call_with_the_workers_own_settings(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert W.probe(Client([])).status == "NO_KEY"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("EDGE_RESEARCH_ENABLED", "1")
    ok = reply("OK")
    ok.model = "claude-opus-5-5"
    c = Client([ok])
    p = W.probe(c)
    assert p.status == "OK" and "using claude-opus-5-5 (test call answered, served by it)" in p.note
    assert "research ON" in p.note and "$3/day" in p.note
    kw = c.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default" and kw["max_tokens"] <= 300
    fb = reply("OK")
    fb.model = "claude-opus-5"
    assert "served by claude-opus-5" in W.probe(Client([fb])).note        # a fallback answered


def test_a_failing_claude_call_is_classified_not_hidden(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("EDGE_RESEARCH_ENABLED", raising=False)

    class Denied(Exception):
        status_code, message = 401, "invalid x-api-key"

    class Boom:
        beta = NS(messages=NS(create=lambda **kw: (_ for _ in ()).throw(Denied())))

    p = W.probe(Boom())
    assert p.status == "NOT_AUTHORIZED" and "invalid x-api-key" in p.note and "research OFF" in p.note
    assert W.probe(Client([reply("", stop="refusal")])).status == "ERROR"


# ---- a failed search is "not researched", never a rejection (audit 2026-09-25) ----

NOTHING = json.dumps({"claims": [], "unknowns": ["Server tool use limit exceeded; could not verify any news"]})


def searched_and_failed(text, code="max_uses_exceeded"):
    r = reply(text, searches=0)
    r.content = [NS(type="server_tool_use", name="web_search"),
                 NS(type="web_search_tool_result", content=NS(type="web_search_tool_result_error", error_code=code)),
                 NS(type="text", text=text)]
    return r


def test_a_web_search_error_block_records_not_researched_and_skips_the_review():
    store, c = MemoryStore(), Client([searched_and_failed(json.dumps({"claims": [], "unknowns": []}))])
    out = W.research_symbol(c, store, symbol="SHOP", day="2026-09-23", now=ts(8, 35))
    assert out["status"] == "not_researched" and "max_uses_exceeded" in out["reason"] and len(c.calls) == 1
    rec = store.get("edge_research", "2026-09-23|SHOP")
    assert rec["status"] == "not_researched" and rec["author_tool_errors"] == ["max_uses_exceeded"]
    assert store.get("edge_research_budget", "2026-09-23")["spent_usd"] == pytest.approx(out["cost_usd"])
    v = W.verdict(store, day="2026-09-23", symbol="SHOP", issued_at=ts(9, 10))
    assert v["catalyst"] is None and v["dilutive"] is None and "max_uses_exceeded" in v["not_researched"]


def test_a_tool_limit_reported_only_in_the_authors_words_is_not_researched_too():
    store = MemoryStore()
    out = W.research_symbol(Client([reply(NOTHING, searches=0)]), store, symbol="SHOP", day="2026-09-23",
                            now=ts(8, 35))
    assert out["status"] == "not_researched" and "Server tool use limit exceeded" in out["reason"]
    assert W.verdict(store, day="2026-09-23", symbol="SHOP", issued_at=ts(9, 10))["catalyst"] is None


def test_an_older_record_of_a_failed_search_is_read_as_not_researched():
    store = MemoryStore()      # written before the status existed: 0 claims, the failure only in unknowns
    store.put("edge_research", "2026-09-23|SHOP", {"day": "2026-09-23", "symbol": "SHOP", "made_at": ts(8, 35),
              "claims": [], "unknowns": ["Server tool use limit exceeded"], "review": {"dilution_found": None}})
    assert W.verdict(store, day="2026-09-23", symbol="SHOP", issued_at=ts(9, 10))["catalyst"] is None
    assert W.not_researched_reason(store.get("edge_research", "2026-09-23|SHOP"))


def test_a_search_that_ran_and_found_nothing_is_still_a_rejection():
    store = MemoryStore()
    empty = json.dumps({"claims": [], "unknowns": ["no company-specific news for SHOP in the 24 hours before the cutoff"]})
    out = W.research_symbol(Client([reply(empty)]), store, symbol="SHOP", day="2026-09-23", now=ts(8, 35))
    assert out["status"] == "researched" and out["claims"] == 0
    v = W.verdict(store, day="2026-09-23", symbol="SHOP", issued_at=ts(9, 10))
    assert v["catalyst"] is False and "not_researched" not in v


def test_claims_the_reviewer_never_checked_are_unknown_not_rejected():
    store = MemoryStore()
    W.research_symbol(Client([reply(AUTHOR_OK), reply("", stop="refusal")]), store,
                      symbol="SHOP", day="2026-09-23", now=ts(8, 35))
    rec = store.get("edge_research", "2026-09-23|SHOP")
    assert rec["claims"][0]["status"] == RS.QUARANTINED and rec["claims"][0]["problems"] == ["no usable review"]
    v = W.verdict(store, day="2026-09-23", symbol="SHOP", issued_at=ts(9, 10))
    assert v["catalyst"] is None and "review did not run" in v["not_researched"]


def test_the_card_books_a_failed_search_as_missing_data_not_a_rejection(monkeypatch):
    import sys
    sys.path.insert(0, "tests")
    from test_edge_pipeline import FakeAlpaca, ts as pts
    from edge import pipeline as P, setups as S
    from edge.ledger import Ledger
    monkeypatch.setenv("EDGE_RESEARCH_ENABLED", "1")
    lg = Ledger(MemoryStore())
    W.research_symbol(Client([searched_and_failed(NOTHING)]), lg.store, symbol="SHOP", day="2026-09-23",
                      now=pts(8, 40))
    P.morning_card(FakeAlpaca(), lg, now=pts(9, 10))
    card = lg.store.get("edge_cards", "2026-09-23")
    row = {r["symbol"]: r for r in card["rows"]}["SHOP"]
    assert row["verified_verdict"] == S.DATA_UNAVAILABLE and row["research_status"] == "not_researched"
    assert row["verified_missing"][0].startswith("catalyst: not researched (web search failed")
    assert card["verified_forecasts"] == []


def test_the_scorecard_counts_not_researched_rows_on_neither_side():
    from edge import scorecard as SC

    def rows(prefix, n, wins, **stamp):
        return [{"symbol": f"{prefix}{i}", "outcome": "WIN" if i < wins else "LOSS", "baseline": "ELIGIBLE",
                 **stamp} for i in range(n)]

    store = MemoryStore()
    store.put("edge_card_outcomes", "2026-09-23", {"day": "2026-09-23", "rows":
              rows("A", 10, 6, research="ELIGIBLE", research_status="researched")
              + rows("R", 10, 2, research="REJECTED", research_status="researched")
              + rows("N", 7, 5, research="REJECTED", research_status="not_researched")   # tool failed
              + rows("L", 3, 3, research="REJECTED")})                                   # legacy rows
    for i in range(3):     # the legacy rows' research records show the failure
        store.put("edge_research", f"2026-09-23|L{i}", {"day": "2026-09-23", "symbol": f"L{i}", "made_at": 1,
                  "claims": [], "unknowns": ["web_search failed: Server tool use limit exceeded"], "review": {}})
    ai = SC.scorecard(store)["ai_research"]
    assert ai["approved"]["candidates"] == 10 and ai["rejected"]["candidates"] == 10
    assert ai["rejected"]["wins"] == 2 and ai["not_researched"] == 10
    assert SC.research_quality(store)["by_reviewer"]["unknown"]["not_researched"] == 3
