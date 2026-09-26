"""An independent reviewer from a different model family (OpenAI), when available.

A Claude reviewer checking a Claude author shares the author's model and its
blind spots. A reviewer from another family catches a different set of
mistakes. It is used only when BOTH hold:
  * OPENAI_API_KEY answers the models endpoint (the probe checks), and
  * EDGE_OPENAI_MODEL names a model that key can use -- no model name is
    guessed here; the probe lists what the key can see so the right one can
    be set from evidence.
It has no web access: it reviews the author's claims and quoted source
sentences for wrong company, internal contradictions, dilution language and
staleness. Two families agreeing is still not independent EVIDENCE -- both
read the same citations -- and every record says so.

Having no search tool, it cannot hit a search limit: a reviewer "search hit its
usage limit" can only come from the Claude fallback reviewer, which runs only
when this review is unavailable or unusable. A review that
fails in flight (timeout, dropped connection) is charged at a full-review
estimate, since it may have been billed (audit U70).
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from edge.providers import base as B

API = "https://api.openai.com/v1"
# USD per 1M tokens (input, output), OpenAI list prices. A model not listed here is
# charged at the most expensive listed rate, so the daily cap can only over-count.
PRICES = {"gpt-6-luna": (0.10, 0.50), "gpt-6-sol": (2.00, 10.00), "gpt-6-astra": (10.00, 50.00),
          "gpt-4.1": (2.00, 8.00)}
_WORST = max(PRICES.values(), key=lambda p: p[1])
# Reasoning models spend hidden output tokens; this bounds one review's cost.
MAX_OUT = 4000


def cost_usd(model: str, usage: Optional[Dict[str, Any]]) -> float:
    p_in, p_out = PRICES.get(model, _WORST)
    u = usage or {}
    return ((u.get("prompt_tokens") or 0) * p_in + (u.get("completion_tokens") or 0) * p_out) / 1e6


def failed_call_estimate(model: str, prompt: str) -> float:
    """What a review that failed in flight may have cost: the prompt (~3 chars a token, rounded up)
    plus a full MAX_OUT of output, at the model's rate."""
    return cost_usd(model, {"prompt_tokens": len(prompt) // 3 + 1, "completion_tokens": MAX_OUT})


def _key() -> str:
    return (os.getenv("OPENAI_API_KEY") or "").strip()


def configured() -> bool:
    return bool(_key() and (os.getenv("EDGE_OPENAI_MODEL") or "").strip())


def probe(http) -> B.Probe:
    if not _key():
        return B.Probe("llm.reviewer.independent", "openai", B.NO_KEY, note="OPENAI_API_KEY not set")
    try:
        r = http.get(f"{API}/models", headers={"Authorization": f"Bearer {_key()}"}, timeout=15)
    except Exception as exc:  # noqa: BLE001
        return B.Probe("llm.reviewer.independent", "openai", B.ERROR, note=type(exc).__name__)
    st = B.classify(r.status_code)
    if st != B.OK:
        return B.Probe("llm.reviewer.independent", "openai", st, http_status=r.status_code)
    ids = sorted(str(m.get("id")) for m in (r.json() or {}).get("data") or [] if isinstance(m, dict))
    want = (os.getenv("EDGE_OPENAI_MODEL") or "").strip()
    listing = "chat models this key can use: " + ", ".join(chat_models(ids))
    if want and want in ids:
        # Listed is not the same as usable for this job: make one tiny real call.
        try:
            t = http.post(f"{API}/chat/completions", timeout=60,
                          headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"},
                          json={"model": want, "max_completion_tokens": 300,
                                "messages": [{"role": "user", "content": "Reply with the single word OK."}]})
        except Exception as exc:  # noqa: BLE001
            return B.Probe("llm.reviewer.independent", "openai", B.ERROR, rows=len(ids),
                           note=f"{want} test call failed: {type(exc).__name__}; {listing}")
        if t.status_code >= 400:
            msg = ""
            try:
                msg = str(((t.json() or {}).get("error") or {}).get("message") or "")[:160]
            except Exception:  # noqa: BLE001
                pass
            return B.Probe("llm.reviewer.independent", "openai", B.ERROR, http_status=t.status_code,
                           rows=len(ids), note=f"{want} refused a chat completion: {msg}; {listing}")
        return B.Probe("llm.reviewer.independent", "openai", B.OK, http_status=200, rows=len(ids),
                       note=f"using {want} (test call answered); {listing}")
    return B.Probe("llm.reviewer.independent", "openai", B.EMPTY, http_status=200, rows=len(ids),
                   note=("EDGE_OPENAI_MODEL not set; " if not want else f"{want} not available; ")
                        + "chat models this key can use: " + ", ".join(chat_models(ids)))


# Not chat-completions text models: media, embeddings, legacy completions, and
# Responses-only variants. Dated snapshots duplicate their undated alias.
_NOT_CHAT = ("audio", "realtime", "tts", "transcribe", "image", "embedding", "search",
             "instruct", "moderation", "dall-e", "whisper", "babbage", "davinci", "codex", "sora")
_DATED = re.compile(r"-(\d{4}-\d{2}-\d{2}|\d{4})$")


def chat_models(ids: List[str]) -> List[str]:
    """Every chat model the key lists -- ALL of them, so the strongest is never cut off
    by an alphabetical prefix (the first probe stopped at gpt-4.1 of 134)."""
    return sorted(i for i in ids
                  if (i.startswith(("gpt-", "chatgpt-", "chat-")) or re.match(r"o\d", i))
                  and not any(w in i for w in _NOT_CHAT) and not _DATED.search(i))


PROMPT = """You are checking claims about the stock {symbol} before they enter a trading-research
ledger. You cannot browse; judge only from the claims and the quoted source sentences below. A quote
may be a search-result headline; "published_at" is null and "unknowns" says so when the author only
saw a relative date ("1 day ago") -- weigh that when judging staleness.

{claims}

Reply with ONLY a JSON object:
{{"entity_ok": bool (are the claims about {symbol} itself, not a namesake or a sector?),
  "contradictions": [str] (claims contradicted by their own quotes or by each other),
  "dilution_found": bool (do the quotes mention an offering, ATM, registered direct, warrants?),
  "stale": bool (do the quotes show the event was public before the prior session's close?),
  "notes": str}}"""


def review(http, *, symbol: str, claims: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], float]:
    """(the reviewer's JSON verdict or None if unavailable / unusable, cost in USD).
    On None the caller falls back to the Claude reviewer; the cost is still counted."""
    if not configured():
        return None, 0.0
    model = os.environ["EDGE_OPENAI_MODEL"].strip()
    prompt = PROMPT.format(symbol=symbol, claims=json.dumps(claims, indent=1))
    body = {"model": model, "max_completion_tokens": MAX_OUT,
            "messages": [{"role": "user", "content": prompt}]}
    try:
        r = http.post(f"{API}/chat/completions", json=body, timeout=90,
                      headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"})
    except Exception:  # noqa: BLE001
        # A timeout or dropped connection may still have been billed: count it as a full review
        # (the whole prompt plus MAX_OUT) so the daily cap can only over-count (audit U70).
        return None, failed_call_estimate(model, prompt)
    if r.status_code >= 400:
        return None, 0.0                 # rejected, not billed
    try:
        payload = r.json() or {}
    except Exception:  # noqa: BLE001
        return None, failed_call_estimate(model, prompt)
    cost = cost_usd(model, payload.get("usage") or {})
    if not payload.get("usage"):
        cost = failed_call_estimate(model, prompt)   # answered but did not say what it used
    try:
        text = str(((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    except (AttributeError, IndexError, TypeError):
        text = ""
    m = re.search(r"\{.*\}", text, re.S)
    try:
        v = json.loads(m.group(0)) if m else None
    except (json.JSONDecodeError, ValueError):
        v = None
    return (v if isinstance(v, dict) else None), cost
