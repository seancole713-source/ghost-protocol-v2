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
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from edge.providers import base as B

API = "https://api.openai.com/v1"


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
    if want and want in ids:
        return B.Probe("llm.reviewer.independent", "openai", B.OK, http_status=200, rows=len(ids),
                       note=f"using {want}")
    return B.Probe("llm.reviewer.independent", "openai", B.EMPTY, http_status=200, rows=len(ids),
                   note=("EDGE_OPENAI_MODEL not set; " if not want else f"{want} not available; ")
                        + "key can use: " + ", ".join(ids[:15]))


PROMPT = """You are checking claims about the stock {symbol} before they enter a trading-research
ledger. You cannot browse; judge only from the claims and the quoted source sentences below.

{claims}

Reply with ONLY a JSON object:
{{"entity_ok": bool (are the claims about {symbol} itself, not a namesake or a sector?),
  "contradictions": [str] (claims contradicted by their own quotes or by each other),
  "dilution_found": bool (do the quotes mention an offering, ATM, registered direct, warrants?),
  "stale": bool (do the quotes show the event was public before the prior session's close?),
  "notes": str}}"""


def review(http, *, symbol: str, claims: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The reviewer's JSON verdict, or None if unavailable / unusable (the caller falls back)."""
    if not configured():
        return None
    body = {"model": os.environ["EDGE_OPENAI_MODEL"].strip(),
            "messages": [{"role": "user", "content": PROMPT.format(symbol=symbol, claims=json.dumps(claims, indent=1))}]}
    try:
        r = http.post(f"{API}/chat/completions", json=body, timeout=90,
                      headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"})
        if r.status_code >= 400:
            return None
        text = (((r.json() or {}).get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"\{.*\}", text, re.S)
    try:
        v = json.loads(m.group(0)) if m else None
    except (json.JSONDecodeError, ValueError):
        v = None
    return v if isinstance(v, dict) else None
