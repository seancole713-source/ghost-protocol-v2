"""The research worker: Claude researches catalysts BEFORE the card, a second
Claude reviews, and only reviewed, cited claims can feed a decision.

Why before: a claim made after a forecast was issued is hindsight -- the claim
contract (edge/research.py) quarantines it. So the worker runs 08:30-09:04 ET,
one mover per scheduler tick, and the 09:05 card reads what it found.

Author and reviewer are both Claude, with web search. They share a model and
read the same web, so their agreement is recorded as CORRELATED, never as
independent evidence. The reviewer's job is mechanical checking -- right
company, contradictions, dilution the author missed, staleness -- not a second
opinion to be counted.

It spends the operator's API money, so it is OFF unless EDGE_RESEARCH_ENABLED
is set, and it stops for the day at EDGE_RESEARCH_DAILY_USD (default $3),
computed from each response's real token and search usage.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from edge import research as RS
from edge.contracts import ET

MODEL = "claude-opus-5-5"
# claude-opus-5-5 list prices, USD per token, and per web search; cache reads bill at 5% of
# input (platform.claude.com/docs/en/about-claude/pricing).
PRICE_IN, PRICE_OUT, PRICE_SEARCH = 4.00 / 1e6, 20.00 / 1e6, 10.00 / 1000
PRICE_CACHE_READ = PRICE_IN * 0.05
AUTHOR, REVIEWER = f"{MODEL}/author", f"{MODEL}/reviewer"

AUTHOR_PROMPT = """You research one stock for a trading-research ledger. Your output is checked
mechanically and anything unsupported is discarded, so precision beats coverage.

Stock: {symbol}. Research cutoff: {cutoff} (US/Eastern). Only use information published
before the cutoff and within the 24 hours before it.

Find dated, company-specific events for {symbol}: earnings or guidance, FDA/regulatory
decisions, material contracts, M&A, index inclusion, analyst actions with a price target.
Also look specifically for anything dilutive (offerings, ATM programs, registered directs,
warrants) and for reverse splits, and report those as claims too.

Rules:
- One checkable fact per claim. Every claim cites at least one URL and quotes the sentence it rests on.
- Numbers carry units.
- No predictions, no price targets of your own, no probabilities or confidence percentages.
- Sector or policy news that does not name {symbol} is not a company catalyst; if that is all
  you find, return no claims and say so in "unknowns".

Reply with ONLY a JSON object, no prose, in this shape:
{{"claims": [{{"kind": one of {kinds}, "statement": str, "value": number or null,
  "unit": str or null, "citations": [{{"url": str, "quote": str, "published_at": ISO-8601 or null}}],
  "unknowns": [str]}}], "unknowns": [str]}}"""

REVIEWER_PROMPT = """You check another researcher's claims about the stock {symbol} before they
enter a trading-research ledger. Research cutoff: {cutoff} (US/Eastern). Use web search to verify.

Claims:
{claims}

Check, and only report what you verified:
- entity_ok: are the claims about {symbol} itself, not a similarly named company or a sector?
- contradictions: statements a primary source (company release, SEC filing) contradicts.
- dilution_found: is there an offering, ATM program, registered direct or warrant issue in the
  last 30 days that the claims do not mention?
- stale: were the events already public before the prior trading day's close?

Reply with ONLY a JSON object:
{{"entity_ok": bool, "contradictions": [str], "dilution_found": bool, "stale": bool, "notes": str}}"""


def enabled() -> bool:
    return os.getenv("EDGE_RESEARCH_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


def daily_cap_usd() -> float:
    try:
        return max(0.0, float(os.getenv("EDGE_RESEARCH_DAILY_USD", "3")))
    except ValueError:
        return 3.0


def _client():
    import anthropic
    return anthropic.Anthropic(timeout=110.0, max_retries=1)


def cost_usd(usage: Any) -> float:
    if usage is None:
        return 0.0
    searches = 0
    stu = getattr(usage, "server_tool_use", None)
    if stu is not None:
        searches = int(getattr(stu, "web_search_requests", 0) or 0)
    tokens_in = (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
    return (tokens_in * PRICE_IN + (getattr(usage, "cache_read_input_tokens", 0) or 0) * PRICE_CACHE_READ
            + (getattr(usage, "output_tokens", 0) or 0) * PRICE_OUT + searches * PRICE_SEARCH)


def _ask(client, prompt: str, *, max_searches: int = 5) -> Tuple[Optional[str], float, str]:
    """(final text, cost, stop_reason). Continues server-tool pauses; honours refusals."""
    messages: List[Dict[str, Any]] = [{"role": "user", "content": prompt}]
    total = 0.0
    for _ in range(3):
        resp = client.beta.messages.create(
            model=MODEL, max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            thinking={"type": "adaptive"}, output_config={"effort": "medium"},
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}],
            messages=messages,
        )
        total += cost_usd(getattr(resp, "usage", None))
        if resp.stop_reason == "refusal":
            return None, total, "refusal"
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return text, total, resp.stop_reason or ""
    return None, total, "pause_limit"


def _json(text: Optional[str]) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    return v if isinstance(v, dict) else None


def _iso_to_epoch(s: Any) -> Optional[int]:
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def to_claims(symbol: str, raw: Dict[str, Any], *, made_at: int) -> List[RS.Claim]:
    out = []
    for c in raw.get("claims") or []:
        if not isinstance(c, dict):
            continue
        cits = tuple(RS.Citation(str(x.get("url") or ""), made_at - 1, str(x.get("quote") or ""),
                                 _iso_to_epoch(x.get("published_at")))
                     for x in c.get("citations") or [] if isinstance(x, dict))
        val = c.get("value")
        out.append(RS.Claim(symbol=symbol.upper(), kind=str(c.get("kind") or "other"),
                            statement=str(c.get("statement") or ""), author=AUTHOR, made_at=made_at,
                            citations=cits, value=float(val) if isinstance(val, (int, float)) else None,
                            unit=c.get("unit"), unknowns=tuple(str(u) for u in c.get("unknowns") or [])))
    return out


def research_symbol(client, store, *, symbol: str, day: str, now: int, http=None) -> Dict[str, Any]:
    key = f"{day}|{symbol.upper()}"
    if store.get("edge_research", key):
        return {"status": "already_researched", "symbol": symbol}
    budget = store.get("edge_research_budget", day) or {"spent_usd": 0.0}
    if budget["spent_usd"] >= daily_cap_usd():
        return {"status": "budget_exhausted", "spent_usd": round(budget["spent_usd"], 4)}
    cutoff = datetime.fromtimestamp(now, tz=ET).strftime("%Y-%m-%d %H:%M")
    text, c1, stop1 = _ask(client, AUTHOR_PROMPT.format(symbol=symbol, cutoff=cutoff, kinds=list(RS.KINDS)))
    raw = _json(text) or {"claims": [], "unknowns": [f"author output unusable (stop={stop1})"]}
    claims = to_claims(symbol, raw, made_at=now)
    review_raw, c2, c_oai, stop2, reviewer = None, 0.0, 0.0, "skipped", REVIEWER
    if claims and http is not None:
        from edge import research_openai as RO
        if RO.configured():
            review_raw, c_oai = RO.review(http, symbol=symbol, claims=[
                {"kind": c.kind, "statement": c.statement,
                 "quotes": [x.quote for x in c.citations], "urls": [x.url for x in c.citations]} for c in claims])
            if review_raw is not None:
                reviewer, stop2 = f"openai:{os.environ.get('EDGE_OPENAI_MODEL', '').strip()}", "openai"
    if claims and review_raw is None:
        rtext, c2, stop2 = _ask(client, REVIEWER_PROMPT.format(
            symbol=symbol, cutoff=cutoff,
            claims=json.dumps([{"kind": c.kind, "statement": c.statement,
                                "urls": [x.url for x in c.citations]} for c in claims], indent=1)))
        review_raw = _json(rtext)
    review = RS.Review(reviewer=reviewer,
                       entity_ok=(review_raw or {}).get("entity_ok"),
                       contradictions=[str(x) for x in (review_raw or {}).get("contradictions") or []],
                       dilution_found=(review_raw or {}).get("dilution_found"),
                       stale=(review_raw or {}).get("stale"),
                       notes=str((review_raw or {}).get("notes") or ""))
    graded = []
    for c in claims:
        status, problems = (RS.reviewed_status(c, review) if review_raw is not None
                            else (RS.QUARANTINED, ["no usable review"]))
        graded.append({"kind": c.kind, "statement": c.statement, "status": status, "problems": problems,
                       "urls": [x.url for x in c.citations]})
    spent = c1 + c2 + c_oai            # the daily cap covers BOTH providers
    budget["spent_usd"] = round(budget["spent_usd"] + spent, 6)
    store.put("edge_research_budget", day, budget)
    rec = {"day": day, "symbol": symbol.upper(), "made_at": now, "claims": graded,
           "unknowns": [str(u) for u in raw.get("unknowns") or []],
           "review": {"entity_ok": review.entity_ok, "contradictions": review.contradictions,
                      "dilution_found": review.dilution_found, "stale": review.stale, "notes": review.notes},
           "author_stop": stop1, "reviewer_stop": stop2, "cost_usd": round(spent, 4),
           "reviewer": reviewer,
           "independence": ("reviewer is a different model family, but both read the same citations: "
                            "agreement is still not independent evidence") if reviewer.startswith("openai:")
                           else "author and reviewer share a model: agreement is correlated, not independent"}
    store.put("edge_research", key, rec)
    return {"status": "researched", "symbol": symbol.upper(), "claims": len(graded),
            "usable": sum(1 for g in graded if g["status"] != RS.QUARANTINED), "cost_usd": round(spent, 4)}


def verdict(store, *, day: str, symbol: str, issued_at: int) -> Dict[str, Optional[bool]]:
    """What the card may use: (catalyst, dilutive), each True / False / None (unknown)."""
    rec = store.get("edge_research", f"{day}|{symbol.upper()}")
    if not rec or rec["made_at"] >= issued_at:
        return {"catalyst": None, "dilutive": None}
    usable = [c for c in rec["claims"] if c["status"] != RS.QUARANTINED]
    specific = {"earnings", "guidance", "fda_regulatory", "contract", "m_and_a", "index_inclusion", "analyst_action"}
    dilutive = bool(rec["review"].get("dilution_found")) or any(
        c["kind"] in ("offering_dilution", "reverse_split") for c in usable)
    return {"catalyst": any(c["kind"] in specific for c in usable), "dilutive": dilutive,
            "headline": next((c["statement"] for c in usable if c["kind"] in specific), None)}
