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
# The headline classifier is versioned like the resolver (docs/resolver_versions.md, "Headline
# classifier"): a change in what counts as a catalyst changes which names a frozen rule selects, so
# forecasts made under different classifiers are never pooled. Forecasts carry their version in
# evidence; one without it is dated: session 2026-10-02 or later ran this classifier (#229, deployed
# before that session; the file is unchanged since), anything earlier ran an older one.
CLASSIFIER_VERSION = "headlines_v5"
CLASSIFIER_SINCE = "2026-10-12"        # first session decided by headlines_v5 (every row it makes is tagged)
UNTAGGED_V4_SINCE = "2026-10-06"       # an UNTAGGED row from this session on ran headlines_v4
UNTAGGED_V3_SINCE = "2026-10-05"       # an UNTAGGED row from this session on ran headlines_v3
UNTAGGED_V2_SINCE = "2026-10-02"       # an UNTAGGED row from this session on ran headlines_v2
LEGACY_CLASSIFIER = "headlines_v1"
CLASSIFIER_LABELS = {
    "headlines_v1": ("headlines_v1 (legacy, sessions before 2026-10-02: product news that mentioned "
                     "'earnings' could count as an earnings catalyst, e.g. HOOD 2026-09-30)"),
    "headlines_v2": ("headlines_v2 (2026-10-02 to 2026-10-03: earnings previews/dates and "
                     "earnings-named products are not catalysts; revenue-milestone releases were not "
                     "catalysts, e.g. MEDS 2026-10-01)"),
    "headlines_v3": ("headlines_v3 (2026-10-05: as headlines_v2, plus a company's "
                     "monthly/preliminary/record revenue or a revenue milestone counts as a results "
                     "catalyst; an earnings-call transcript re-post could count as earnings, e.g. RXO "
                     "2026-10-05)"),
    "headlines_v4": ("headlines_v4 (2026-10-06 to 2026-10-09: as headlines_v3, plus a transcript of an "
                     "earnings or conference call is a re-post of an old event, never a catalyst; "
                     "'raises fiscal 2027 revenue guidance', 'wins order', CMS star ratings and "
                     "'offering of common stock' were not recognized, e.g. HAE 2026-10-08)"),
    "headlines_v5": ("headlines_v5 (current, from 2026-10-12: as headlines_v4, plus guidance raised/cut "
                     "with words between the verb and 'guidance', 'sees ... vs est' guidance, contract "
                     "wins/orders/awards, CMS star ratings as a regulatory decision, and more offering "
                     "phrasings as dilution)"),
}


def classifier_of(evidence: Optional[Dict] = None, session_date: Optional[str] = None) -> str:
    """The classifier a forecast or card row was decided with: its own tag, else by session date:
    from CLASSIFIER_SINCE the current one (a session with nothing to tag -- no intraday forecast,
    an empty card -- still ran it), from UNTAGGED_V3_SINCE headlines_v3, from UNTAGGED_V2_SINCE
    headlines_v2, earlier headlines_v1."""
    tag = (evidence or {}).get("classifier_version") if isinstance(evidence, dict) else None
    if tag:
        return str(tag)
    day = session_date or ""
    if day >= CLASSIFIER_SINCE:
        return CLASSIFIER_VERSION
    if day >= UNTAGGED_V4_SINCE:
        return "headlines_v4"
    if day >= UNTAGGED_V3_SINCE:
        return "headlines_v3"
    return "headlines_v2" if day >= UNTAGGED_V2_SINCE else LEGACY_CLASSIFIER


DILUTIVE = frozenset({OFFERING})
MECHANICAL = frozenset({REVERSE_SPLIT})
PRICE_ACTION = "price_action"          # describes a move; never a catalyst (never in COMPANY_SPECIFIC)
# Rule E4 counts an analyst UPGRADE (or a raised target / new coverage). Reiterating, maintaining
# or cutting a rating is not new information for a long: 2026-09-24 "Guggenheim Reiterates Buy
# on Everpure, Maintains $150 Price Target" qualified P as a catalyst_breakout.
ANALYST_NO_CHANGE = "analyst_no_change"   # never in COMPANY_SPECIFIC
# A NEGATIVE regulatory or clinical outcome -- an FDA rejection / complete response letter, a
# clinical hold, a failed primary endpoint, a panel vote against -- is news against a long, never
# a reason to buy one (audit 2026-09-25 U14: "FDA rejects" tagged as a strong FDA catalyst).
# Tightening only: it is neither a catalyst nor dilution. Never in COMPANY_SPECIFIC.
REGULATORY_SETBACK = "regulatory_setback"
# "Earnings" is not always an earnings RESULT. A preview or a date announcement says a report is
# coming, not what it said; product news can merely contain the word: 2026-10 "Robinhood launches
# earnings contracts on its prediction markets hub" was tagged an EARNINGS catalyst for HOOD.
# Tightening only: neither is in COMPANY_SPECIFIC.
EARNINGS_SCHEDULED = "earnings_scheduled"   # preview / date / call announcement
PRODUCT_NEWS = "product_news"               # a product or feature that mentions earnings
COMPANY_SPECIFIC = frozenset({EARNINGS, GUIDANCE, FDA, CONTRACT, MNA, INDEX, ANALYST})

# A market wrap -- index moves and/or several stories joined by ";" -- is never one company's catalyst,
# whatever words it contains. 2026-09-23: "Dow Falls 100 Points; General Mills Posts Upbeat Q1
# Earnings" qualified WHLR as an EARNINGS catalyst (General Mills' earnings, not Wheeler's).
_MARKET_WRAP = re.compile(
    r"\b(dow|nasdaq|s&p( 500)?|russell 2000|stock market|us stocks|wall street)\b.{0,30}"
    r"\b(points?|falls?|gains?|rises?|drops?|jumps?|higher|lower|slips?|climbs?|down|up)\b"
    # Several stories joined by ";" -- but only when the part before the ";" is about the
    # market, not the company: "Crude Oil Down Over 1%; Thor Industries Shares Gain" is a
    # wrap, "FDA Panel Endorses GRAIL's Galleri Test; GRAL Stock Reflects ..." is not.
    r"|^[^;]*\b(dow|nasdaq|s&p|russell|crude|oil|gold|bitcoin|treasur\w*|yields?|futures|stocks|markets?)\b"
    r"[^;]*;\s*\S.*\b(posts?|reports?|shares?|stock)\b")

# Price-action words describe a move; they do not explain it. They are checked AFTER the
# company events, so "GRAL Stock Surges 36% Following Positive FDA Advisory Panel Vote" is
# the FDA event it names (2026-09-24 live test: 10 of 51 real catalysts were lost, several
# this way). A headline with only price words still falls through to PRICE_ACTION.
_PRICE_WORDS = (r"\b(stocks?|shares)\b.{0,40}\b(soar|soars|surge|surges|jump|jumps|rall(y|ies|ying)|pops?|"
                r"explodes?|rockets?|spikes?|climbs?|plunges?|tumbles?|sinks?|whipsaws?|skyrockets?|rips?|"
                r"slid(es|ing)|slides?)\b|"
                r"\bvolatility\b|\bwhy .{0,40} (stock|shares) (is|are) (up|down|trading|rallying|sliding)\b|"
                r"\b(top )?(gainers|losers|movers)\b|\bstocks moving\b")

_RULES = [  # first match wins; dilution is checked first on purpose
    # 2026-09-24: "Greenland Mines Completes $12-Per-Share Equity Financing" (a registered direct
    # with pre-funded warrants) did not read as dilution.
    (OFFERING, r"\b(public offering|registered direct|direct offering|at-the-market|atm program|private placement|"
               r"priced .* offering|warrants?|equity financing|equity offering|share offering|pre-funded)\b"
               # headlines_v5: 2026-10-08 "BiOptio... Commences ~$4M Offering Of Common Stock" (BIAF) read as OTHER.
               r"|\b(offering of (\$[\d.]+[mb]? (of )?)?(its )?(common )?(stock|shares)|commences? .{0,30}\boffering"
               r"|pricing of .{0,40}\boffering|proposed .{0,20}\boffering)\b"),
    (REVERSE_SPLIT, r"\breverse (stock |share )?split\b|\b1[- ]for[- ]\d+\b|\bshare consolidation\b"),
    # Previews, report dates and filings are not results or decisions (rule E4 needs the event itself).
    (EARNINGS_SCHEDULED,
     r"\bahead of (its |the |q[1-4] )?earnings\b|\bupcoming earnings\b|\bearnings (preview|scheduled|date)\b"
     r"|\b(to (report|announce|release|post|host)|will (report|announce|release|post|host)|sets? (the )?date|"
     r"date (of|for)|schedules?|scheduled)\b.{0,60}\b(earnings|results|conference call|revenue|sales)\b"
     r"|\bconference call to discuss\b|\bearnings (this|next) week\b|\bearnings\b.{0,40}\bwhat to expect\b"),
    (OTHER, r"\b(ind|investigational new drug)\b|\bfda submission\b"),
    (MNA, r"\b(to acquire|acquisition of|merger|to be acquired|takeover|buyout|definitive agreement)\b"),
    # Lifting a hold or resubmitting after a rejection is the regulatory event moving forward.
    (FDA, r"\b(lifts?|lifted|removes?|removed|resolves?|resolved)\b.{0,20}\bclinical hold\b|\bresubmi(ts?|tted|ssion)\b"),
    (REGULATORY_SETBACK,
     r"\bfda\b.{0,40}\b(rejects?|rejected|rejection|declines?|declined|denies|denied|refuses?|refused)\b"
     r"|\b(complete response letter|crl|clinical hold|refusal to file|refuse to file|not approvable)\b"
     r"|\b(fail(s|ed)?|miss(es|ed)?|did not meet|does not meet|not meet)\b.{0,30}\bprimary (end ?point|goal)\b"
     r"|\b(panel|committee|adcom)\b.{0,30}\bvotes? against\b"),
    (FDA, r"\b(fda|pdufa|breakthrough therapy|phase (1|2|3|i|ii|iii)|(nda|bla|510\(k\)|ema|marketing) (approval|clearance))\b"),
    # headlines_v5: a Medicare (CMS) star-ratings release is a regulatory decision on the company's
    # plans (2026-10-09 "Humana Announces Improved CMS Star Ratings", HUM +16%, read as OTHER).
    # A cut or decline is news against a long (2026-10-09 ALHC -23% on its ratings): never a catalyst.
    (REGULATORY_SETBACK, r"\bstar ratings?\b.{0,40}\b(cut|lower(ed)?|declin\w*|drop\w*|fall\w*|downgrad\w*)\b"
                         r"|\b(cut|lower(ed|s)?|declin\w*|drop\w*|downgrad\w*)\b.{0,40}\bstar ratings?\b"),
    (FDA, r"\b(cms|medicare( advantage)?)\b.{0,40}\bstar ratings?\b|\bstar ratings?\b.{0,40}\b(cms|medicare)\b"),
    # A product or feature named after earnings ("earnings contracts", "earnings prediction markets",
    # "earnings calls feature") is not a report -- unless the headline also carries a result.
    (PRODUCT_NEWS,
     r"^(?!.*\b(beats?|miss(es|ed)?|tops|results|eps|revenue|profit|loss|quarterly)\b).*("
     r"\bearnings[- ](calls?[- ]|event[- ])?(contracts?|prediction[- ]markets?|predictions?|markets?|bets?|"
     r"wagers?|hub|features?|tools?|products?)\b"
     r"|\bprediction[- ]markets?\b.{0,40}\bearnings\b)"),
    # headlines_v3 (operator decision 2026-10-03): a company reporting its own monthly, preliminary
    # or record revenue, or a revenue milestone, is a results event. 2026-10-01 "DataMeds AI's Corexa
    # Pharmacy Surpasses $1 Million In Monthly Revenue" (MEDS +37%) read as OTHER under headlines_v2.
    (EARNINGS, r"\b(monthly|preliminary|unaudited|record) ([\w-]+ ){0,2}(revenue|sales|net sales)\b"
               r"|\b(surpass(es|ed)?|exceed(s|ed)?|tops|cross(es|ed)?|reach(es|ed)?|hits|achiev(es|ed))\b"
               r".{0,40}\b(revenue|sales)\b"
               r"|\b(revenue|sales) (milestone|record)\b"),
    (EARNINGS, r"\b(earnings|quarterly results|q[1-4] results|eps|revenue (rose|grew|increased|beat))\b"
               r"|\bq[1-4]\b.{0,20}\b(beats?|miss(es)?|results?|revenue|sales)\b"
               r"|\breports? (slower|weaker|stronger|record) (growth|sales)\b"),
    (GUIDANCE, r"\b(raises|lifts|boosts|cuts|lowers) (its )?(full-year |annual |fy\d{2,4} |fiscal \d{4} )?"
               r"(guidance|outlook|forecast)\b|\b(outlook|guidance|forecast) (tops|beats|exceeds|trails|misses)\b"),
    # headlines_v5: words between the verb and "guidance" (2026-10-08 "Haemonetics Raises Fiscal 2027
    # Revenue Guidance" read as OTHER), and Benzinga's "Sees FY27 Revenue $X vs $Y Est" format.
    # ("Wall Street raises Everpure targets after upbeat outlook" stays an analyst action.)
    (GUIDANCE, r"\b(raises|lifts|boosts|ups|increases|cuts|lowers)\b(?:(?!\b(targets?|pts?)\b).){0,40}"
               r"\b(guidance|outlook|forecast)\b"
               r"|\bsees\b.{0,40}\b(revenue|sales|eps)\b.{0,60}\b(vs\.? .{0,20}\best|prior)\b"),
    (CONTRACT, r"\b(contract|award(ed|s)?|partnership|collaboration|agreement with|order from)\b"
               # headlines_v5: "wins/secures/receives/lands ... order(s)"
               r"|\b(wins|won|secures|secured|receives|received|lands|landed|books|booked)\b.{0,40}\b(orders?|deal)\b"),
    (INDEX, r"\b(added to|join(s|ing)?) the (s&p|russell|nasdaq)|index inclusion\b"),
    (ANALYST, r"\b(upgrade[sd]?|raise[sd]? (\w+ ){0,2}(price )?targets?|initiat(es|ed) coverage)\b"),
    (ANALYST_NO_CHANGE, r"\b(reiterat\w*|maintain\w*|keeps|affirm\w*|downgrade[sd]?|"
                        r"(lower|cut|trim)s? (its |the )?(price target|pt)|"
                        r"cuts? (\w+ )?(rating|to (sell|underperform|underweight|neutral|hold))|"
                        r"receives \W?(buy|overweight|outperform)\W? rating)\b"),
    (ANALYST, r"\b(price target|overweight|outperform)\b"),
    (PRICE_ACTION, _PRICE_WORDS),
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


# headlines_v4: a transcript re-posts an earnings or conference call that already happened -- it is
# not a new event. 2026-10-05: "Transcript: RXO Q2 2026 Earnings Conference Call" (Q2 reported in
# August) read as EARNINGS and approved an RXO catalyst_breakout forecast; the real news that morning
# was C.H. Robinson's offer to buy RXO.
# A transcript headline that also carries the result ("Earnings call transcript: Darden Q1 meets EPS
# view, shares slip") is same-day results coverage and keeps its kind.
_TRANSCRIPT = re.compile(r"^(?!.*\b(beats?|meets?|miss(es|ed)?|tops|eps|guidance|outlook|revenue (rose|grew|"
                         r"fell|increased|beat))\b)"
                         r".*(^\W*transcripts?\b|\b(call|earnings|conference|webcast|presentation|"
                         r"remarks|q[1-4]( \d{4})?( results)?) transcripts?\b|\btranscripts? of\b)")


def classify(headline: str) -> str:
    h = headline.lower()
    if _MARKET_WRAP.search(h):
        return PRICE_ACTION
    if _TRANSCRIPT.search(h):
        return OTHER
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
