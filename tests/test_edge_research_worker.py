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
        return [{"symbol": f"{prefix}{i}", "outcome": "WIN" if i < wins else "LOSS",
                 "execution": "WIN" if i < wins else "LOSS", "baseline": "ELIGIBLE", **stamp} for i in range(n)]

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


# ---- 2026-09-26: search limits yield partial results; failed calls are charged and back off (U70) ----

def limited(text, code="max_uses_exceeded", tokens=(30_000, 2_000)):
    """A response that searched, then hit the per-request search limit, then wrote `text`."""
    r = reply(text, tokens=tokens, searches=W.AUTHOR_SEARCHES)
    r.content = [NS(type="server_tool_use", name="web_search"),
                 NS(type="web_search_tool_result", content=[NS(type="web_search_result", url="https://x.com/a")]),
                 NS(type="server_tool_use", name="web_search"),
                 NS(type="web_search_tool_result", content=NS(type="web_search_tool_result_error", error_code=code)),
                 NS(type="text", text=text)]
    return r


class Raising(Client):
    """Replays `replies`; an exception instance in the list is raised instead of returned."""

    def _create(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


class Timeout(Exception):          # like anthropic.APITimeoutError: no status code, may have been billed
    pass


class RateLimited(Exception):      # like anthropic.RateLimitError: rejected before any work
    status_code, message = 429, "rate limited"


def test_a_search_limit_with_partial_claims_keeps_and_reviews_them():
    store, c = MemoryStore(), Client([limited(AUTHOR_OK), reply(REVIEW_OK)])
    out = W.research_symbol(c, store, symbol="SHOP", day="2026-09-23", now=ts(8, 35))
    assert out["status"] == "researched" and out["claims"] == 1 and out["usable"] == 1
    rec = store.get("edge_research", "2026-09-23|SHOP")
    assert rec["partial"] is True and rec["author_tool_errors"] == ["max_uses_exceeded"]
    assert W.not_researched_reason(rec) is None and len(c.calls) == 2           # reviewed, not dropped
    assert W.verdict(store, day="2026-09-23", symbol="SHOP", issued_at=ts(9, 10))["catalyst"] is True


def test_a_search_limit_that_left_no_json_gets_one_toolless_finish_call():
    prose = "The search tool hit its usage limit. From what I saw, Shopify announced a Muse deal."
    store, c = MemoryStore(), Client([limited(prose), reply(AUTHOR_OK, searches=0), reply(REVIEW_OK)])
    out = W.research_symbol(c, store, symbol="SHOP", day="2026-09-23", now=ts(8, 35))
    assert out["status"] == "researched" and out["usable"] == 1 and len(c.calls) == 3
    finish = c.calls[1]
    assert finish["tool_choice"] == {"type": "none"}
    assert finish["messages"][-2]["role"] == "assistant" and finish["messages"][-1]["content"] == W.FINISH_PROMPT
    rec = store.get("edge_research", "2026-09-23|SHOP")
    assert rec["author_stop"] == "finish:end_turn" and rec["partial"] is True
    # both author requests are on the cap, plus the review
    expect = sum(W.cost_usd(r.usage) for r in (limited(prose), reply(AUTHOR_OK, searches=0), reply(REVIEW_OK)))
    assert store.get("edge_research_budget", "2026-09-23")["spent_usd"] == pytest.approx(expect, abs=1e-6)


def test_no_finish_call_without_a_tool_error():
    store, c = MemoryStore(), Client([reply("no json here")])
    out = W.research_symbol(c, store, symbol="SHOP", day="2026-09-23", now=ts(8, 35))
    assert out["status"] == "not_researched" and "author output unusable" in out["reason"] and len(c.calls) == 1


def test_one_symbol_per_request_with_bounded_search_and_page_opens():
    store, c = MemoryStore(), Client([reply(AUTHOR_OK), reply(REVIEW_OK), reply(AUTHOR_OK), reply(REVIEW_OK)])
    W.research_symbol(c, store, symbol="SHOP", day="2026-09-23", now=ts(8, 35))
    W.research_symbol(c, store, symbol="USAR", day="2026-09-23", now=ts(8, 40))
    for kw, mine, other in ((c.calls[0], "SHOP", "USAR"), (c.calls[2], "USAR", "SHOP")):
        prompt = kw["messages"][0]["content"]
        assert f"Stock: {mine}." in prompt and other not in prompt
        assert f"at most {W.AUTHOR_SEARCHES} web searches" in prompt
        assert "Do NOT run one search per event type" in prompt
        search, fetch = kw["tools"]
        assert search == {"type": "web_search_20260209", "name": "web_search", "max_uses": W.AUTHOR_SEARCHES}
        assert fetch["type"] == "web_fetch_20260209" and fetch["max_uses"] == W.AUTHOR_FETCHES
        assert fetch["max_content_tokens"] == W.FETCH_MAX_TOKENS
    assert 5 <= W.AUTHOR_SEARCHES <= 8
    review = c.calls[1]
    assert review["tools"] == [{"type": "web_search_20260209", "name": "web_search",
                                "max_uses": W.REVIEWER_SEARCHES}]
    assert f"at most {W.REVIEWER_SEARCHES} web searches" in review["messages"][0]["content"]


def test_a_page_that_would_not_open_is_not_a_failed_search():
    text = json.dumps({"claims": [], "unknowns": ["no SHOP news in the window"]})
    r = reply(text)
    r.content = [NS(type="web_fetch_tool_result", content=NS(type="web_fetch_tool_error", error_code="url_not_accessible")),
                 NS(type="web_fetch_tool_result", content=NS(type="web_fetch_result", url="https://x.com")),
                 NS(type="text", text=text)]
    store = MemoryStore()
    out = W.research_symbol(Client([r]), store, symbol="SHOP", day="2026-09-23", now=ts(8, 35))
    rec = store.get("edge_research", "2026-09-23|SHOP")
    assert out["status"] == "researched" and rec["author_fetch_errors"] == ["url_not_accessible"]
    assert rec["author_tool_errors"] == []


def test_a_failed_call_is_charged_to_the_cap_and_recorded_not_researched():
    store, c = MemoryStore(), Raising([Timeout("read timed out")])
    out = W.research_symbol(c, store, symbol="SHOP", day="2026-09-23", now=ts(8, 35))
    assert out["status"] == "not_researched" and "Timeout" in out["reason"] and "attempt 1 of 2" in out["reason"]
    assert store.get("edge_research_budget", "2026-09-23")["spent_usd"] == pytest.approx(W.FAILED_CALL_USD)
    rec = store.get("edge_research", "2026-09-23|SHOP")
    assert rec["call_failed"] is True and rec["retry_after"] == ts(8, 35) + W.RETRY_AFTER_S
    v = W.verdict(store, day="2026-09-23", symbol="SHOP", issued_at=ts(9, 10))
    assert v["catalyst"] is None and "research call failed" in v["not_researched"]


def test_a_failure_after_a_billed_pause_counts_both():
    paused = reply("", stop="pause_turn")
    store = MemoryStore()
    W.research_symbol(Raising([paused, Timeout()]), store, symbol="SHOP", day="2026-09-23", now=ts(8, 35))
    assert store.get("edge_research_budget", "2026-09-23")["spent_usd"] == pytest.approx(
        W.cost_usd(paused.usage) + W.FAILED_CALL_USD)


def test_a_rejected_call_is_not_charged():
    store = MemoryStore()
    W.research_symbol(Raising([RateLimited()]), store, symbol="SHOP", day="2026-09-23", now=ts(8, 35))
    assert store.get("edge_research_budget", "2026-09-23")["spent_usd"] == 0.0
    assert store.get("edge_research", "2026-09-23|SHOP")["last_error"].startswith("RateLimited")


def test_a_failed_symbol_backs_off_then_retries_once_then_stops():
    store = MemoryStore()
    W.research_symbol(Raising([Timeout()]), store, symbol="SHOP", day="2026-09-23", now=ts(8, 30))
    idle = Raising([])
    for mm in (35, 40):                                           # every tick inside the backoff: no call
        out = W.research_symbol(idle, store, symbol="SHOP", day="2026-09-23", now=ts(8, mm))
        assert out["status"] == "backoff" and not W.due(store, day="2026-09-23", symbol="SHOP", now=ts(8, mm))
    assert idle.calls == []
    assert W.due(store, day="2026-09-23", symbol="SHOP", now=ts(8, 45))
    out = W.research_symbol(Raising([Timeout()]), store, symbol="SHOP", day="2026-09-23", now=ts(8, 45))
    assert "attempt 2 of 2" in out["reason"] and out["retry_after"] is None
    assert store.get("edge_research_budget", "2026-09-23")["spent_usd"] == pytest.approx(2 * W.FAILED_CALL_USD)
    assert store.get("edge_research", "2026-09-23|SHOP")["cost_usd"] == pytest.approx(2 * W.FAILED_CALL_USD)
    late = Raising([])
    assert W.research_symbol(late, store, symbol="SHOP", day="2026-09-23", now=ts(9, 30))["status"] == "backoff"
    assert late.calls == [] and not W.due(store, day="2026-09-23", symbol="SHOP", now=ts(9, 30))


def test_a_successful_retry_replaces_the_failure_and_keeps_its_cost():
    store = MemoryStore()
    W.research_symbol(Raising([Timeout()]), store, symbol="SHOP", day="2026-09-23", now=ts(8, 30))
    out = W.research_symbol(Client([reply(AUTHOR_OK), reply(REVIEW_OK)]), store, symbol="SHOP",
                            day="2026-09-23", now=ts(8, 45))
    rec = store.get("edge_research", "2026-09-23|SHOP")
    assert out["status"] == "researched" and rec["attempts"] == 2 and "call_failed" not in rec
    spent = store.get("edge_research_budget", "2026-09-23")["spent_usd"]
    assert spent == pytest.approx(W.FAILED_CALL_USD + out["cost_usd"], abs=1e-4)
    assert rec["cost_usd"] == pytest.approx(spent, abs=1e-4)
    assert W.research_symbol(Client([]), store, symbol="SHOP", day="2026-09-23",
                             now=ts(8, 50))["status"] == "already_researched"


def test_a_failed_claude_review_keeps_the_claims_unchecked_and_charged():
    store = MemoryStore()
    out = W.research_symbol(Raising([reply(AUTHOR_OK), Timeout()]), store, symbol="SHOP", day="2026-09-23",
                            now=ts(8, 35))
    rec = store.get("edge_research", "2026-09-23|SHOP")
    assert rec["reviewer_stop"] == "error" and rec["claims"][0]["problems"] == ["no usable review"]
    assert "review did not run" in W.not_researched_reason(rec)
    assert out["cost_usd"] == pytest.approx(W.cost_usd(reply(AUTHOR_OK).usage) + W.FAILED_CALL_USD, abs=1e-4)


def test_a_failed_openai_review_is_charged_and_the_claude_reviewer_takes_over(monkeypatch):
    from edge import research_openai as RO
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("EDGE_OPENAI_MODEL", "gpt-6-sol")

    class TimesOut(OAIHttp):
        def post(self, url, json=None, headers=None, timeout=None):
            self.posts.append(json)
            raise TimeoutError("read timed out")

    verdict, cost = RO.review(TimesOut(), symbol="SHOP", claims=[{"kind": "contract"}])
    assert verdict is None and cost >= RO.MAX_OUT * 10.00 / 1e6          # a full MAX_OUT at $10/1M
    store, c = MemoryStore(), Client([reply(AUTHOR_OK), reply(REVIEW_OK)])
    out = W.research_symbol(c, store, symbol="SHOP", day="2026-09-23", now=ts(8, 35), http=TimesOut())
    assert store.get("edge_research", "2026-09-23|SHOP")["reviewer"] == W.REVIEWER and len(c.calls) == 2
    assert out["cost_usd"] == pytest.approx(W.cost_usd(reply(AUTHOR_OK).usage) + W.cost_usd(reply(REVIEW_OK).usage)
                                            + cost, abs=1e-3)


def test_the_openai_reviewer_sees_dates_and_the_authors_unknowns(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("EDGE_OPENAI_MODEL", "gpt-x")
    h = OAIHttp(REVIEW_OK)
    W.research_symbol(Client([reply(AUTHOR_OK)]), MemoryStore(), symbol="SHOP", day="2026-09-23",
                      now=ts(8, 35), http=h)
    content = h.posts[0]["messages"][0]["content"]
    assert '"published_at"' in content and "2026-09-21T08:00:00-04:00" in content and '"unknowns"' in content


def test_a_pause_limit_keeps_what_was_written():
    store = MemoryStore()
    out = W.research_symbol(Client([reply("", stop="pause_turn"), reply("", stop="pause_turn"),
                                    reply(AUTHOR_OK, stop="pause_turn"), reply(REVIEW_OK)]),
                            store, symbol="SHOP", day="2026-09-23", now=ts(8, 35))
    assert out["status"] == "researched" and out["usable"] == 1
    assert store.get("edge_research", "2026-09-23|SHOP")["author_stop"] == "pause_limit"


def test_the_scheduler_retries_a_failed_symbol_only_after_its_backoff():
    """research_step used to skip any symbol with a record, so a failed call's one retry never ran."""
    from edge import research_worker as RW
    from edge.ledger import MemoryStore
    s = MemoryStore()
    s.put("edge_research", "2026-09-28|GLND", {"status": "not_researched", "call_failed": True,
                                                "attempts": 1, "retry_after": 1000})
    assert RW.due(s, day="2026-09-28", symbol="GLND", now=999) is False
    assert RW.due(s, day="2026-09-28", symbol="GLND", now=1000) is True
    s.put("edge_research", "2026-09-28|GLND", {"status": "not_researched", "call_failed": True,
                                                "attempts": RW.MAX_ATTEMPTS, "retry_after": 0})
    assert RW.due(s, day="2026-09-28", symbol="GLND", now=5000) is False
    s.put("edge_research", "2026-09-28|WHLR", {"status": "researched"})
    assert RW.due(s, day="2026-09-28", symbol="WHLR", now=5000) is False
    import inspect
    from edge import pipeline as P
    assert "rw.due(" in inspect.getsource(P.research_step)
