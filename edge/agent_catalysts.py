"""Catalyst claims submitted by a connected research agent, for gap_and_go_assist@v1 only.

Operator decision 2026-10-09: "Ghost + Claude" -- the morning research the operator's Claude
session does (a web sweep for each premarket gapper's news) may supply rule E4's catalyst for ONE
separately named paper experiment, so its record can be compared with Ghost's keyword check alone.

Point-in-time by construction:
  first_seen_at  is the server's clock when the claim was written, never the agent's word;
  a claim counts for a card only if first_seen_at <= the card's data cutoff, and
  published_at (the source's own time, given by the agent) is within 24h of that cutoff and
  not after the claim was written.
Claims are append-only (a claim is never edited; a changed mind is a new claim) and are read by
no other experiment, gate or the operator's own rule.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from edge import catalysts as C
from edge.contracts import ET

COLLECTION = "edge_agent_catalysts"
KINDS = tuple(sorted(C.COMPANY_SPECIFIC | {C.OFFERING}))
MAX_HEADLINE, MAX_AUTHOR, MAX_URL = 300, 64, 500
MAX_AGE_S = 86_400
_SYM = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ClaimError(ValueError):
    pass


def _epoch(v: Any) -> int:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v)
    s = str(v or "").strip()
    if not s:
        raise ClaimError("published_at is required (ISO 8601 with a time zone, or epoch seconds)")
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ClaimError("published_at must be ISO 8601 with a time zone, or epoch seconds") from None
    if t.tzinfo is None:
        raise ClaimError("published_at needs a time zone (e.g. 2026-10-09T07:02:00-05:00)")
    return int(t.timestamp())


def write(store, *, symbol: str, headline: str, kind: str, source_url: str, published_at: Any,
          author: str, now: int, day: Optional[str] = None) -> Dict[str, Any]:
    sym = str(symbol or "").strip().upper()
    headline, kind = str(headline or "").strip(), str(kind or "").strip()
    url, author = str(source_url or "").strip(), str(author or "").strip()
    if not _SYM.match(sym):
        raise ClaimError("symbol must be a US ticker (letters, digits, '.')")
    if kind not in KINDS:
        raise ClaimError(f"kind must be one of {KINDS}")
    if not headline or len(headline) > MAX_HEADLINE:
        raise ClaimError(f"headline is required, at most {MAX_HEADLINE} characters")
    if not url.startswith("https://") or len(url) > MAX_URL:
        raise ClaimError("source_url must be an https:// link to the source")
    if not author or len(author) > MAX_AUTHOR:
        raise ClaimError(f"author is required, at most {MAX_AUTHOR} characters")
    pub = _epoch(published_at)
    if pub > now + 300:
        raise ClaimError("published_at is in the future")
    if now - pub > MAX_AGE_S:
        raise ClaimError("published_at is more than 24h old; rule E4 needs a catalyst from the last 24h")
    day = (day or datetime.fromtimestamp(now, tz=ET).date().isoformat()).strip()
    if not _DAY.match(day):
        raise ClaimError("day must be YYYY-MM-DD")
    digest = hashlib.sha256(f"{sym}|{kind}|{headline}|{url}".encode()).hexdigest()[:12]
    key = f"{day}|{sym}|{digest}"
    if store.get(COLLECTION, key):
        raise ClaimError("that claim already exists; claims are never overwritten")
    rec = {"id": key, "day": day, "symbol": sym, "kind": kind, "headline": headline, "source_url": url,
           "published_at": pub, "first_seen_at": int(now), "author": author}
    store.put(COLLECTION, key, rec)
    return {"status": "written", "id": key, "first_seen_at": int(now)}


def usable(store, *, day: str, symbol: str, issued_at: int) -> List[Dict[str, Any]]:
    """The claims a card with data cutoff `issued_at` could legitimately have used."""
    out = []
    for r in store.scan(COLLECTION, day=day, symbol=symbol.upper()):
        seen, pub = int(r.get("first_seen_at") or 0), int(r.get("published_at") or 0)
        if seen <= issued_at and pub <= seen + 300 and issued_at - pub <= MAX_AGE_S:
            out.append(r)
    return sorted(out, key=lambda r: r["first_seen_at"])
