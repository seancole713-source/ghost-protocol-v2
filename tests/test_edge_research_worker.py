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
    assert kw["model"] == "claude-opus-5" and kw["fallbacks"] == "default"
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
