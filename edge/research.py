"""The contract research workers (Claude, OpenAI) must meet for their output to count.

A worker returns CLAIMS, not opinions: each is one checkable statement with
citations, a timestamp, and its explicit unknowns. A claim without a source is
quarantined, not "low confidence". A reviewer -- a different worker -- checks
entity match, contradictions, dilution and staleness, and its verdict is stored
separately from the author's.

Two models agreeing is NOT independent evidence: they read the same sources and
fail in correlated ways. Agreement is recorded as agreement, never promoted to a
probability. No worker may emit a confidence percentage or change a rule.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

KINDS = ("earnings", "guidance", "fda_regulatory", "contract", "m_and_a", "index_inclusion",
         "analyst_action", "offering_dilution", "reverse_split", "policy_macro", "halt", "other")
VERIFIED, SINGLE_SOURCE, QUARANTINED = "verified", "single_source", "quarantined"
_PERCENT_CONFIDENCE = re.compile(r"\b\d{1,3}(\.\d+)?\s*%\s*(chance|probability|likely|confiden)", re.I)


@dataclass(frozen=True)
class Citation:
    url: str
    retrieved_at: int
    quote: str = ""                 # the sentence the claim rests on
    published_at: Optional[int] = None


@dataclass(frozen=True)
class Claim:
    symbol: str
    kind: str
    statement: str
    author: str                     # "claude" | "openai" | ...
    made_at: int
    citations: Tuple[Citation, ...] = ()
    value: Optional[float] = None
    unit: Optional[str] = None
    unknowns: Tuple[str, ...] = ()


@dataclass
class Review:
    reviewer: str
    entity_ok: Optional[bool] = None
    contradictions: List[str] = field(default_factory=list)
    dilution_found: Optional[bool] = None
    stale: Optional[bool] = None
    notes: str = ""


def validate(c: Claim, *, issued_at: Optional[int] = None) -> Tuple[str, List[str]]:
    """(status, problems). Anything structural fails closed into quarantine."""
    problems = []
    if c.kind not in KINDS:
        problems.append(f"unknown kind {c.kind!r}")
    if not c.statement.strip():
        problems.append("empty statement")
    if not c.citations:
        problems.append("no citation")
    for cit in c.citations:
        if not re.match(r"^https?://", cit.url):
            problems.append(f"citation is not a URL: {cit.url[:40]}")
        if cit.retrieved_at > c.made_at:
            problems.append("citation retrieved after the claim was made")
    if c.value is not None and not c.unit:
        problems.append("number without a unit")
    if _PERCENT_CONFIDENCE.search(c.statement):
        problems.append("workers may not state probabilities")
    if issued_at is not None and c.made_at > issued_at:
        problems.append("claim made after the forecast was issued")
    if problems:
        return QUARANTINED, problems
    domains = {re.sub(r"^https?://(www\.)?", "", x.url).split("/")[0] for x in c.citations}
    return (VERIFIED if len(domains) >= 2 else SINGLE_SOURCE), []


def reviewed_status(c: Claim, r: Review, *, issued_at: Optional[int] = None) -> Tuple[str, List[str]]:
    status, problems = validate(c, issued_at=issued_at)
    if status == QUARANTINED:
        return status, problems
    if r.reviewer == c.author:
        return QUARANTINED, ["a claim cannot be reviewed by its own author"]
    if r.entity_ok is False:
        problems.append("reviewer: wrong entity")
    if r.contradictions:
        problems.extend(f"reviewer: {x}" for x in r.contradictions)
    if r.stale:
        problems.append("reviewer: stale")
    if problems:
        return QUARANTINED, problems
    return status, []


def agreement(claims: List[Claim]) -> Dict[str, object]:
    """Which authors made the same kind of claim about a symbol -- recorded, not scored."""
    by: Dict[Tuple[str, str], set] = {}
    for c in claims:
        by.setdefault((c.symbol, c.kind), set()).add(c.author)
    return {f"{s}:{k}": {"authors": sorted(a), "agree": len(a) > 1,
                         "note": "agreement between models is correlated, not independent evidence"}
            for (s, k), a in by.items()}
