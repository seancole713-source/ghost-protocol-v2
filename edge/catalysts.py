"""Catalyst events, point-in-time.

Two timestamps per event, and they are never interchangeable:
  published_at   when the source says it was published
  first_seen_at  when THIS system first received it
A forecast may only use an event it had SEEN before issuance. News fetched
today about last year must not pose as news the system had last year.

Classification here is keyword_v1 -- a tagger, not a judge. Verification
(entity match, dilution, staleness, contradictions) belongs to the research
worker and its independent reviewer; their claims arrive with citations.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, List, Optional

EARNINGS, GUIDANCE, FDA, CONTRACT, MNA = "earnings", "guidance", "fda_regulatory", "contract", "m_and_a"
INDEX, ANALYST, OFFERING, REVERSE_SPLIT, POLICY, OTHER = (
    "index_inclusion", "analyst_action", "offering_dilution", "reverse_split", "policy_macro", "other")
DILUTIVE = frozenset({OFFERING})
MECHANICAL = frozenset({REVERSE_SPLIT})
PRICE_ACTION = "price_action"          # describes a move; never a catalyst (never in COMPANY_SPECIFIC)
COMPANY_SPECIFIC = frozenset({EARNINGS, GUIDANCE, FDA, CONTRACT, MNA, INDEX, ANALYST})

_RULES = [  # first match wins; dilution is checked first on purpose
    (OFFERING, r"\b(public offering|registered direct|at-the-market|atm program|private placement|priced .* offering|warrants?)\b"),
    (REVERSE_SPLIT, r"\breverse (stock |share )?split\b|\b1[- ]for[- ]\d+\b|\bshare consolidation\b"),
    # Price action describes a move, it never explains one: "WHLR stock explodes on volatility".
    (PRICE_ACTION, r"\b(stocks?|shares)\b.{0,40}\b(soar|soars|surge|surges|jump|jumps|rall(y|ies)|pops?|explodes?|"
                   r"rockets?|spikes?|climbs?|plunges?|tumbles?|sinks?|whipsaws?|skyrockets?|rips?)\b|"
                   r"\bvolatility\b|\bwhy .{0,40} (stock|shares) (is|are) (up|down|trading)\b|"
                   r"\b(top )?(gainers|losers|movers)\b|\bstocks moving\b"),
    (MNA, r"\b(to acquire|acquisition of|merger|to be acquired|takeover|buyout|definitive agreement)\b"),
    (FDA, r"\b(fda|pdufa|breakthrough therapy|phase (1|2|3|i|ii|iii)|(nda|bla|510\(k\)|ema|marketing) (approval|clearance))\b"),
    (EARNINGS, r"\b(earnings|quarterly results|q[1-4] results|eps|revenue (rose|grew|increased|beat))\b"),
    (GUIDANCE, r"\b(raises|lifts|boosts|cuts|lowers) (its )?(full-year |annual )?(guidance|outlook|forecast)\b"),
    (CONTRACT, r"\b(contract|award(ed)?|partnership|collaboration|agreement with|order from)\b"),
    (INDEX, r"\b(added to|join(s|ing)?) the (s&p|russell|nasdaq)|index inclusion\b"),
    (ANALYST, r"\b(upgrade[sd]?|price target|initiat(es|ed) coverage|overweight|outperform)\b"),
    (POLICY, r"\b(tariff|executive order|administration|white house|treasury|sanction|security deal)\b"),
]


@dataclass(frozen=True)
class CatalystEvent:
    symbol: str
    headline: str
    source: str
    url: str
    published_at: int
    first_seen_at: int
    kind: str = OTHER
    tickers: tuple = ()
    scheduled: bool = False          # a date is known; its OUTCOME is not
    story_key: str = ""
    sources_seen: tuple = field(default_factory=tuple)

    @property
    def company_specific(self) -> bool:
        return self.kind in COMPANY_SPECIFIC

    @property
    def dilutive(self) -> bool:
        return self.kind in DILUTIVE


def classify(headline: str) -> str:
    h = headline.lower()
    for kind, pattern in _RULES:
        if re.search(pattern, h):
            return kind
    return OTHER


def story_key(headline: str) -> str:
    norm = re.sub(r"[^a-z0-9 ]", "", headline.lower())
    norm = " ".join(w for w in norm.split() if len(w) > 2)
    return hashlib.sha1(norm.encode()).hexdigest()[:16]


# A story tagged with more tickers than this is a market wrap or sector roundup, never a
# company-specific catalyst (rule E4). 2026-09-23: "Crude Oil Down Over 1%; Thor Industries
# Shares Gain After Q4 Results" was tagged to DCOY, VKTX and QNME.
MAX_STORY_TICKERS = 3


def make(symbol: str, headline: str, *, source: str, url: str, published_at: int, first_seen_at: int,
         tickers: Iterable[str] = (), scheduled: bool = False) -> CatalystEvent:
    if first_seen_at < published_at - 300:
        # Receiving an item before its own publication stamp means a clock or
        # feed problem, not prescience. Keep it, but pin receipt to publication.
        first_seen_at = published_at
    return CatalystEvent(symbol.upper(), headline, source, url, int(published_at), int(first_seen_at),
                         classify(headline), tuple(t.upper() for t in tickers), scheduled,
                         story_key(headline), (source,))


def dedupe(events: Iterable[CatalystEvent]) -> List[CatalystEvent]:
    """One story, many outlets -> one event, keeping the EARLIEST sighting."""
    by: Dict[tuple, CatalystEvent] = {}
    for e in events:
        k = (e.symbol, e.story_key)
        cur = by.get(k)
        if cur is None:
            by[k] = e
            continue
        first = e if e.first_seen_at < cur.first_seen_at else cur
        by[k] = replace(first, sources_seen=tuple(sorted(set(cur.sources_seen) | set(e.sources_seen))),
                        published_at=min(cur.published_at, e.published_at))
    return sorted(by.values(), key=lambda e: e.first_seen_at)


def usable_at(events: Iterable[CatalystEvent], symbol: str, *, issued_at: int,
              max_age_s: int = 86_400) -> List[CatalystEvent]:
    """Events a forecast issued at `issued_at` could legitimately have used."""
    return [e for e in events
            if e.symbol == symbol.upper()
            and e.first_seen_at <= issued_at
            and issued_at - e.published_at <= max_age_s]


def entity_match(e: CatalystEvent, company_name: str) -> float:
    """Crude confidence that the story is about THIS company, not a namesake."""
    score = 0.0
    if e.symbol in e.tickers:
        score += 0.6
    words = [w for w in re.findall(r"[a-z]+", company_name.lower()) if w not in {"inc", "corp", "ltd", "plc", "co", "the"}]
    if words and all(w in e.headline.lower() for w in words[:2]):
        score += 0.4
    return min(score, 1.0)
