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
computed from each response's real token and search usage -- and from calls that
FAILED, which can be billed too (a timed-out request may have run to the end).
A failed call is recorded, charged, and retried at most once, after a backoff
(audit U70); it is never re-run every tick.

Searching (fixed 2026-09-26): research produced nothing on 2026-09-23/24/25. The
author had 5 searches per request while its prompt listed nine kinds of event;
it fanned out one query per kind in a single batch, went past max_uses ("Server
tool use limit exceeded") on the first round, could not open a page, saw only
titles, and discarded what it had found. Now: one symbol per request, a stated
search plan (broad first, then dilution, then confirm), a small page-open
allowance, and what it found before a limit is reported and reviewed, not dropped.
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

# Server-tool allowances PER REQUEST, and one request researches ONE symbol. Enough to find and confirm
# one dated catalyst plus a dilution check; the prompts say how to spend them. A search bills $0.01; a
# page open bills only as input tokens, bounded by FETCH_MAX_TOKENS.
AUTHOR_SEARCHES, AUTHOR_FETCHES, FETCH_MAX_TOKENS = 8, 2, 6000
REVIEWER_SEARCHES = 4

# A call that raised may still have been billed (a timeout can fire after the server finished), so it
# is charged at this estimate -- about the dearest research call seen -- and the cap can only
# over-count. Rejections before any work (4xx incl. 429, and 529 overloaded) are not billed.
FAILED_CALL_USD = 0.35
# A symbol whose call failed is retried at most MAX_ATTEMPTS times a day, no sooner than RETRY_AFTER_S
# after the failure. The research window is 08:30-09:00 ET, so that is one retry at most.
RETRY_AFTER_S, MAX_ATTEMPTS = 15 * 60, 2

AUTHOR_PROMPT = """You research one stock for a trading-research ledger. Your output is checked
mechanically and anything unsupported is discarded, so precision beats coverage.

Stock: {symbol}. Research cutoff: {cutoff} (US/Eastern). Only use information published
before the cutoff and within the 24 hours before it.

Find dated, company-specific events for {symbol}: earnings or guidance, FDA/regulatory
decisions, material contracts, M&A, index inclusion, analyst actions with a price target.
Also look specifically for anything dilutive (offerings, ATM programs, registered directs,
warrants) and for reverse splits, and report those as claims too.

Search budget: at most {searches} web searches and {fetches} page opens for this whole task;
anything past that fails. Search one query at a time, in this order:
1. One broad search for {symbol}'s latest news. Read the results before searching again.
2. One search for dilution or a reverse split at {symbol} (offering, ATM, warrants, reverse split).
3. Use what is left only to confirm an event you already found: open its press release, filing or
   article (web_fetch) for the exact sentence and publish time.
Do NOT run one search per event type, and do not batch several searches at once.
If a search or page open fails or the limit is reached, stop searching and report the claims that
the results you already have support. Do not drop them because you could not search more.

Rules:
- One checkable fact per claim. Every claim cites at least one URL and quotes the sentence it rests
  on: from the page if you opened it, otherwise the headline or snippet the search result showed.
- If the only date you have is relative ("1 day ago"), report the claim only if that date could fall
  inside the window, set published_at to null, and state the relative date in the claim's "unknowns".
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

Search budget: at most {searches} web searches in total, one query at a time. Use one on {symbol}'s
recent SEC filings and offerings, and the rest only on the claim that matters most. Do not search
once per claim. If search fails or the limit is reached, stop and answer from what you verified.

Check, and only report what you verified (null for what you could not check):
- entity_ok: are the claims about {symbol} itself, not a similarly named company or a sector?
- contradictions: statements a primary source (company release, SEC filing) contradicts.
- dilution_found: is there an offering, ATM program, registered direct or warrant issue in the
  last 30 days that the claims do not mention?
- stale: were the events already public before the prior trading day's close?

Reply with ONLY a JSON object:
{{"entity_ok": bool or null, "contradictions": [str], "dilution_found": bool or null,
  "stale": bool or null, "notes": str}}"""

# Sent once, only when a search limit or error left the author without a usable JSON reply: what it
# already saw is turned into claims (then reviewed like any other) instead of being thrown away.
FINISH_PROMPT = """Searching is over for this task (a search limit or error was reached). Using ONLY the
results you have already seen above, reply now with the JSON object in the shape asked for. Include
every claim those results support, cited as the rules say, and put what you could not check in
"unknowns"."""


class CallFailed(Exception):
    """A research call raised. Carries what it cost: completed responses plus an estimate for the
    failed request, which may have been billed (audit U70)."""

    def __init__(self, cost: float, error: str):
        super().__init__(error)
        self.cost, self.error = cost, error


def _failed_call_charge(exc: BaseException) -> float:
    code = getattr(exc, "status_code", None)
    if isinstance(code, int) and (code < 500 or code == 529):
        return 0.0            # rejected before any work: not billed
    return FAILED_CALL_USD    # timeout, dropped connection, 5xx: may have run, so count it


def enabled() -> bool:
    return os.getenv("EDGE_RESEARCH_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


def daily_cap_usd() -> float:
    try:
        return max(0.0, float(os.getenv("EDGE_RESEARCH_DAILY_USD", "3")))
    except ValueError:
        return 3.0


def _client():
    import anthropic
    # No SDK retries: a retried timeout is billed twice and was counted zero times. A failed call is
    # charged and retried by research_symbol's backoff instead (audit U70).
    return anthropic.Anthropic(timeout=110.0, max_retries=0)


def probe(client=None):
    """One tiny real call with the research worker's own settings -- model, betas, fallback --
    and which model actually SERVED it (a server-side fallback can answer instead)."""
    from edge.providers import base as B
    cap, prov = "llm.research.author", "anthropic"
    state = f"research {'ON' if enabled() else 'OFF (EDGE_RESEARCH_ENABLED)'}, cap ${daily_cap_usd():g}/day"
    if not (os.getenv("ANTHROPIC_API_KEY") or "").strip():
        return B.Probe(cap, prov, B.NO_KEY, note=f"ANTHROPIC_API_KEY not set; {state}")
    try:
        c = client or _client()
        resp = c.beta.messages.create(
            model=MODEL, max_tokens=300,
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            thinking={"type": "adaptive"}, output_config={"effort": "low"},
            messages=[{"role": "user", "content": "Reply with the single word OK."}],
        )
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "status_code", None)
        msg = str(getattr(exc, "message", "") or exc)[:160]
        return B.Probe(cap, prov, B.classify(code) if code else B.ERROR, http_status=code,
                       note=f"{MODEL} test call failed: {type(exc).__name__}: {msg}; {state}")
    served = str(getattr(resp, "model", "") or "")
    if resp.stop_reason == "refusal":
        return B.Probe(cap, prov, B.ERROR, http_status=200, note=f"{MODEL} refused the test call; {state}")
    who = f"served by {served}" if served and served != MODEL else "served by it"
    return B.Probe(cap, prov, B.OK, http_status=200, rows=1,
                   note=f"using {MODEL} (test call answered, {who}); {state}")


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


NOT_RESEARCHED = "not_researched"
# What a failed search looks like when it only surfaces in the model's own words (the tool's
# error block is the primary signal; this catches the author reporting it, e.g. in "unknowns").
_TOOL_FAILURE = re.compile(
    r"tool use limit|server tool|max_uses_exceeded|too_many_requests|"
    r"(web[ _-]?search|search tool)\b.{0,40}\b(fail|error|unavailable|limit|exceeded)", re.I)


def _tool_errors(content: Any) -> List[str]:
    """Server web-search errors in one response. They do not raise: the result block's content is
    a single error object (e.g. error_code "max_uses_exceeded") where a success is a list."""
    out = []
    for b in content or []:
        if getattr(b, "type", "") != "web_search_tool_result":
            continue
        c = getattr(b, "content", None)
        if isinstance(c, (list, tuple)):
            continue
        code = getattr(c, "error_code", None) if not isinstance(c, dict) else c.get("error_code")
        out.append(str(code or "unknown_error"))
    return out


def _fetch_errors(content: Any) -> List[str]:
    """Page-open (web_fetch) errors in one response. A success is ONE result object, not a list, so an
    error is told apart by its error_code. Kept apart from search errors: a page that would not open
    does not mean the search could not run."""
    out = []
    for b in content or []:
        if getattr(b, "type", "") != "web_fetch_tool_result":
            continue
        c = getattr(b, "content", None)
        code = c.get("error_code") if isinstance(c, dict) else getattr(c, "error_code", None)
        if code:
            out.append(str(code))
    return out


def not_researched_reason(rec: Optional[Dict[str, Any]]) -> Optional[str]:
    """Why a research record is NOT a research result, or None when it is one.

    A search tool that failed, or an author whose output was unusable, found nothing because it
    could not look -- that is not "no catalyst", and must never be booked as a rejection. Also
    reads records written before this status existed (claims empty + a tool failure in unknowns).
    """
    if not rec:
        return None
    if rec.get("status") == NOT_RESEARCHED:
        return str(rec.get("not_researched_reason") or "research did not run")
    claims = rec.get("claims") or []
    if claims:
        # Claims made, but the reviewer never produced a usable check: every claim sits in
        # quarantine for THAT reason alone. Unchecked is unknown, not rejected.
        if all(list(c.get("problems") or []) == ["no usable review"] for c in claims):
            return f"review did not run (reviewer stop={rec.get('reviewer_stop')})"
        return None
    if rec.get("author_tool_errors"):
        return f"web search failed: {', '.join(rec['author_tool_errors'])}"
    for u in rec.get("unknowns") or []:
        if str(u).startswith("author output unusable"):
            return str(u)
        if _TOOL_FAILURE.search(str(u)):
            return f"web search failed: {str(u)[:160]}"
    return None


def _tools(max_searches: int, max_fetches: int) -> List[Dict[str, Any]]:
    tools: List[Dict[str, Any]] = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}]
    if max_fetches:
        tools.append({"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": max_fetches,
                      "max_content_tokens": FETCH_MAX_TOKENS})
    return tools


def _text(resp: Any) -> str:
    return "".join(getattr(b, "text", "") or "" for b in getattr(resp, "content", None) or []
                   if getattr(b, "type", "") == "text")


def _ask(client, prompt: str, *, max_searches: int = 5, max_fetches: int = 0,
         tool_errors: Optional[List[str]] = None, fetch_errors: Optional[List[str]] = None,
         finish: bool = False) -> Tuple[Optional[str], float, str]:
    """(final text, cost, stop_reason). ONE request's worth of research: continues server-tool pauses,
    honours refusals. Web-search / page-open errors (result blocks, not exceptions) are appended to
    tool_errors / fetch_errors. With finish=True, a reply left without usable JSON by such an error
    gets one tool-less follow-up that reports what was already found (stop "finish:<reason>").
    A call that raises becomes CallFailed carrying everything spent, the failed request included."""
    messages: List[Dict[str, Any]] = [{"role": "user", "content": prompt}]
    tools = _tools(max_searches, max_fetches)
    total = 0.0
    errors_seen: List[str] = []

    def create(**extra):
        nonlocal total
        try:
            resp = client.beta.messages.create(
                model=MODEL, max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                thinking={"type": "adaptive"}, output_config={"effort": "medium"},
                tools=tools, messages=messages, **extra,
            )
        except Exception as exc:  # noqa: BLE001 - billed or not, the caller must record it
            msg = str(getattr(exc, "message", "") or exc)[:160]
            raise CallFailed(total + _failed_call_charge(exc), f"{type(exc).__name__}: {msg}") from exc
        total += cost_usd(getattr(resp, "usage", None))
        s_err, f_err = _tool_errors(getattr(resp, "content", None)), _fetch_errors(getattr(resp, "content", None))
        errors_seen.extend(s_err + f_err)
        if tool_errors is not None:
            tool_errors.extend(s_err)
        if fetch_errors is not None:
            fetch_errors.extend(f_err)
        return resp

    resp, text, stop = None, None, "pause_limit"
    for _ in range(3):
        resp = create()
        if resp.stop_reason == "refusal":
            return None, total, "refusal"
        text = _text(resp)
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        stop = resp.stop_reason or ""
        break
    if stop == "pause_limit":
        return text or None, total, stop       # whatever it wrote so far; a paused turn cannot be finished
    if finish and _json(text) is None and (errors_seen or _TOOL_FAILURE.search(text or "")):
        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": FINISH_PROMPT})
        last = create(tool_choice={"type": "none"})
        if last.stop_reason == "refusal":
            return text, total, "refusal"
        return _text(last), total, f"finish:{last.stop_reason or ''}"
    return text, total, stop


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


def _epoch_to_iso(ts: Optional[int]) -> Optional[str]:
    return datetime.fromtimestamp(ts, tz=ET).isoformat() if ts else None


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


def _charge(store, day: str, usd: float) -> None:
    budget = store.get("edge_research_budget", day) or {"spent_usd": 0.0}
    budget["spent_usd"] = round(budget["spent_usd"] + usd, 6)
    store.put("edge_research_budget", day, budget)


def due(store, *, day: str, symbol: str, now: int) -> bool:
    """Should research run for this symbol now? Yes when it has no record, or when its last call
    FAILED, the backoff has passed and it has attempts left. Never every tick (audit U70)."""
    rec = store.get("edge_research", f"{day}|{symbol.upper()}")
    if not rec:
        return True
    return bool(rec.get("call_failed")) and int(rec.get("attempts") or 1) < MAX_ATTEMPTS \
        and now >= int(rec.get("retry_after") or 0)


def _call_failed(store, *, key: str, day: str, symbol: str, now: int, attempts: int, prior_usd: float,
                 exc: CallFailed) -> Dict[str, Any]:
    """Record a failed author call: charged to the day's cap, not_researched (unknown, never a
    rejection), and retryable once the backoff passes -- if attempts remain."""
    _charge(store, day, exc.cost)
    retry_after = now + RETRY_AFTER_S if attempts < MAX_ATTEMPTS else None
    why = f"research call failed ({exc.error}); attempt {attempts} of {MAX_ATTEMPTS}"
    total = round(prior_usd + exc.cost, 4)
    store.put("edge_research", key, {
        "day": day, "symbol": symbol.upper(), "made_at": now, "status": NOT_RESEARCHED,
        "not_researched_reason": why, "call_failed": True, "attempts": attempts, "retry_after": retry_after,
        "last_error": exc.error, "claims": [], "author_tool_errors": [], "unknowns": [], "review": {},
        "author_stop": "error", "reviewer_stop": "skipped", "cost_usd": total, "reviewer": REVIEWER})
    return {"status": NOT_RESEARCHED, "symbol": symbol.upper(), "reason": why, "claims": 0, "usable": 0,
            "cost_usd": round(exc.cost, 4), "retry_after": retry_after}


def research_symbol(client, store, *, symbol: str, day: str, now: int, http=None) -> Dict[str, Any]:
    """Research ONE symbol in one author request (plus one review). Every dollar it spends -- failed
    calls included -- goes on the day's cap; a failed call backs off instead of re-running next tick."""
    key = f"{day}|{symbol.upper()}"
    prev = store.get("edge_research", key)
    attempts, prior_usd = 1, 0.0
    if prev:
        if not prev.get("call_failed"):
            return {"status": "already_researched", "symbol": symbol}
        if not due(store, day=day, symbol=symbol, now=now):
            return {"status": "backoff", "symbol": symbol.upper(), "attempts": prev.get("attempts"),
                    "retry_after": prev.get("retry_after"), "reason": prev.get("not_researched_reason")}
        attempts, prior_usd = int(prev.get("attempts") or 1) + 1, float(prev.get("cost_usd") or 0.0)
    budget = store.get("edge_research_budget", day) or {"spent_usd": 0.0}
    if budget["spent_usd"] >= daily_cap_usd():
        return {"status": "budget_exhausted", "spent_usd": round(budget["spent_usd"], 4)}
    cutoff = datetime.fromtimestamp(now, tz=ET).strftime("%Y-%m-%d %H:%M")
    author_errors: List[str] = []
    fetch_errors: List[str] = []
    try:
        text, c1, stop1 = _ask(client, AUTHOR_PROMPT.format(symbol=symbol, cutoff=cutoff, kinds=list(RS.KINDS),
                                                            searches=AUTHOR_SEARCHES, fetches=AUTHOR_FETCHES),
                               max_searches=AUTHOR_SEARCHES, max_fetches=AUTHOR_FETCHES,
                               tool_errors=author_errors, fetch_errors=fetch_errors, finish=True)
    except CallFailed as exc:
        return _call_failed(store, key=key, day=day, symbol=symbol, now=now, attempts=attempts,
                            prior_usd=prior_usd, exc=exc)
    raw =_json(text) or {"claims": [], "unknowns": [f"author output unusable (stop={stop1})"]}
    claims = to_claims(symbol, raw, made_at=now)
    if not claims:
        # Nothing found BECAUSE the search could not run is not a finding. Record it as
        # not_researched -- never as a rejection -- and spend nothing on a review.
        why = not_researched_reason({"claims": [], "author_tool_errors": author_errors,
                                     "unknowns": raw.get("unknowns") or []})
        if why:
            _charge(store, day, c1)
            store.put("edge_research", key, {
                "day": day, "symbol": symbol.upper(), "made_at": now, "status": NOT_RESEARCHED,
                "not_researched_reason": why, "claims": [], "author_tool_errors": author_errors,
                "author_fetch_errors": fetch_errors, "attempts": attempts,
                "unknowns": [str(u) for u in raw.get("unknowns") or []],
                "review": {}, "author_stop": stop1, "reviewer_stop": "skipped",
                "cost_usd": round(c1 + prior_usd, 4), "reviewer": REVIEWER})
            return {"status": NOT_RESEARCHED, "symbol": symbol.upper(), "reason": why, "claims": 0,
                    "usable": 0, "cost_usd": round(c1, 4)}
    review_raw, c2, c_oai, stop2, reviewer = None, 0.0, 0.0, "skipped", REVIEWER
    if claims and http is not None:
        from edge import research_openai as RO
        if RO.configured():
            review_raw, c_oai = RO.review(http, symbol=symbol, claims=[
                {"kind": c.kind, "statement": c.statement,
                 "quotes": [x.quote for x in c.citations], "urls": [x.url for x in c.citations],
                 "published_at": [_epoch_to_iso(x.published_at) for x in c.citations],
                 "unknowns": list(c.unknowns)}
                for c in claims])
            if review_raw is not None:
                reviewer, stop2 = f"openai:{os.environ.get('EDGE_OPENAI_MODEL', '').strip()}", "openai"
    if claims and review_raw is None:
        try:
            rtext, c2, stop2 = _ask(client, REVIEWER_PROMPT.format(
                symbol=symbol, cutoff=cutoff, searches=REVIEWER_SEARCHES,
                claims=json.dumps([{"kind": c.kind, "statement": c.statement, "unknowns": list(c.unknowns),
                                    "urls": [x.url for x in c.citations]} for c in claims], indent=1)),
                max_searches=REVIEWER_SEARCHES)
        except CallFailed as exc:
            # The author's work is kept and charged; its claims stay unchecked ("review did not run").
            rtext, c2, stop2 = None, exc.cost, "error"
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
    _charge(store, day, spent)
    rec = {"day": day, "symbol": symbol.upper(), "made_at": now, "status": "researched", "claims": graded,
           "author_tool_errors": author_errors, "author_fetch_errors": fetch_errors, "attempts": attempts,
           # Claims made before a search limit / error: what it found, reviewed like any other.
           "partial": bool(author_errors or fetch_errors or stop1.startswith("finish:")),
           "unknowns": [str(u) for u in raw.get("unknowns") or []],
           "review": {"entity_ok": review.entity_ok, "contradictions": review.contradictions,
                      "dilution_found": review.dilution_found, "stale": review.stale, "notes": review.notes},
           "author_stop": stop1, "reviewer_stop": stop2, "cost_usd": round(spent + prior_usd, 4),
           "reviewer": reviewer,
           "independence": ("reviewer is a different model family, but both read the same citations: "
                            "agreement is still not independent evidence") if reviewer.startswith("openai:")
                           else "author and reviewer share a model: agreement is correlated, not independent"}
    store.put("edge_research", key, rec)
    return {"status": "researched", "symbol": symbol.upper(), "claims": len(graded),
            "usable": sum(1 for g in graded if g["status"] != RS.QUARANTINED), "cost_usd": round(spent, 4)}


def verdict(store, *, day: str, symbol: str, issued_at: int) -> Dict[str, Optional[bool]]:
    """What the card may use: (catalyst, dilutive), each True / False / None (unknown).
    A not_researched record answers None for both -- unknown, never "no catalyst"."""
    rec = store.get("edge_research", f"{day}|{symbol.upper()}")
    if not rec or rec["made_at"] >= issued_at:
        return {"catalyst": None, "dilutive": None}
    why = not_researched_reason(rec)
    if why:
        return {"catalyst": None, "dilutive": None, "not_researched": why}
    usable = [c for c in rec["claims"] if c["status"] != RS.QUARANTINED]
    specific = {"earnings", "guidance", "fda_regulatory", "contract", "m_and_a", "index_inclusion", "analyst_action"}
    dilutive = bool(rec["review"].get("dilution_found")) or any(
        c["kind"] in ("offering_dilution", "reverse_split") for c in usable)
    return {"catalyst": any(c["kind"] in specific for c in usable), "dilutive": dilutive,
            "headline": next((c["statement"] for c in usable if c["kind"] in specific), None)}
