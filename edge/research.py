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
# A reviewer check that was not affirmatively completed leaves the claim unchecked (unknown) --
# never clean and never rejected. Every such problem starts with REVIEW_INCOMPLETE.
REVIEW_INCOMPLETE = "review incomplete"
NO_USABLE_REVIEW = "no usable review"
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
    schema_errors: List[str] = field(default_factory=list)


def _bool_or_none(raw: Dict[str, object], key: str, errors: List[str]) -> Optional[bool]:
    v = raw.get(key)
    if v is None or isinstance(v, bool):
        return v
    errors.append(f"{key} is not true/false/null")
    return None


def parse_review(raw: object, *, reviewer: str) -> Review:
    """A reviewer's JSON reply, type-checked. Anything missing or mistyped is left UNKNOWN (None) and
    noted -- an empty reply is an unchecked claim, never a clean one (audit EDGE-06)."""
    if not isinstance(raw, dict):
        return Review(reviewer=reviewer, schema_errors=["reply is not a JSON object"])
    errors: List[str] = []
    cx = raw.get("contradictions")
    if isinstance(cx, list):
        contradictions = [str(x) for x in cx]
    else:
        contradictions = []
        errors.append("contradictions not reported" if cx is None else "contradictions is not a list")
    return Review(reviewer=reviewer, entity_ok=_bool_or_none(raw, "entity_ok", errors),
                  contradictions=contradictions,
                  dilution_found=_bool_or_none(raw, "dilution_found", errors),
                  stale=_bool_or_none(raw, "stale", errors), notes=str(raw.get("notes") or ""),
                  schema_errors=errors)


def review_gaps(r: Review) -> List[str]:
    """The checks this review did not affirmatively complete. A claim is usable only when the
    reviewer CONFIRMED the entity (entity_ok is True) and ANSWERED the dilution and staleness
    checks. Unanswered is unknown, never clean."""
    gaps = [f"{REVIEW_INCOMPLETE}: {e}" for e in r.schema_errors]
    if r.entity_ok is None:
        gaps.append(f"{REVIEW_INCOMPLETE}: entity not confirmed")
    if r.dilution_found is None:
        gaps.append(f"{REVIEW_INCOMPLETE}: dilution not checked")
    if r.stale is None:
        gaps.append(f"{REVIEW_INCOMPLETE}: staleness not checked")
    return gaps


def unchecked(problems: List[str]) -> bool:
    """Quarantined ONLY because the review did not run or did not finish: unknown, not rejected."""
    return bool(problems) and all(p == NO_USABLE_REVIEW or str(p).startswith(REVIEW_INCOMPLETE)
                                  for p in problems)


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
    if r.stale is True:
        problems.append("reviewer: stale")
    # Only an affirmative, complete review clears a claim: entity_ok must be True (not merely "not
    # False"), and the dilution and staleness checks must have been answered (audit EDGE-06).
    problems.extend(review_gaps(r))
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
