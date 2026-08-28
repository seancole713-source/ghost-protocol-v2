"""Deterministic scoring for agent-submitted research evidence.

This module turns one piece of ACCEPTED agent evidence (core.agent_workflow)
into a reproducible quality score along five dimensions the roadmap calls
for: source authority, freshness, corroboration, contradiction, and catalyst
relevance. Nothing here is a probability or a trading signal -- it is a
measurement of how much the evidence itself deserves to be trusted, computed
the same way every time from the same inputs.

Three honesty rules carry over from the rest of Ghost's evidence machinery
(catalyst_checklist.py, evidence_integrity.py):

1. A missing or unrecognized field scores as the WORST case for its
   dimension, never an average or a neutral default. An evidence submission
   that omits timestamps or uses an unranked source kind should never look
   as good as one that supplied real, verifiable provenance.
2. Corroboration only counts INDEPENDENT sources. Three source_refs that all
   resolve to the same domain are one source wearing three hats, not three
   confirmations -- the same principle catalyst_checklist.py applies to
   correlated boxes.
3. Contradiction can only ever subtract. Disagreeing verdicts across
   multiple agents on the same task lower the composite score; agreement
   never raises it above what the other four dimensions independently earn.

Pure functions only. No network I/O, no database access, no side effects --
that is what makes the score reproducible and is what
core.shadow_evidence_ledger persists. Storage and scheduling live there.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Sequence
from urllib.parse import urlparse

SCORING_VERSION = "evidence_score_v1"

# ---------------------------------------------------------------------------
# Dimension weights. Deterministic and documented -- change the version
# string above whenever these change, so old and new scores are never
# silently pooled as if they measured the same thing.
# ---------------------------------------------------------------------------
_WEIGHTS = {
    "source_authority": 0.28,
    "freshness": 0.18,
    "corroboration": 0.20,
    "contradiction": 0.14,   # stored as a bonus in [0,1]; 1.0 = no contradiction found
    "catalyst_relevance": 0.20,
}
assert abs(sum(_WEIGHTS.values()) - 1.0) < 1e-9

# Source authority: primary/regulatory sources outrank secondary commentary.
# An unrecognized or missing `kind` gets the floor, not the middle -- rule 1.
_SOURCE_AUTHORITY: Dict[str, float] = {
    "filing": 1.00,
    "sec_filing": 1.00,
    "exchange_notice": 0.95,
    "official_release": 0.90,
    "press_release": 0.80,
    "regulatory_filing": 1.00,
    "company_ir": 0.85,
    "news_article": 0.55,
    "equity_research": 0.50,
    "analyst_note": 0.45,
    "web_search": 0.35,
    "social_media": 0.15,
}
_SOURCE_AUTHORITY_FLOOR = 0.10  # unrecognized kind, or kind missing entirely

_REGULATORY_DOMAINS = frozenset({
    "sec.gov", "finra.org", "federalregister.gov", "fca.org.uk",
})
_EXCHANGE_DOMAINS = frozenset({
    "nasdaq.com", "nasdaqtrader.com", "nyse.com", "cboe.com", "otcmarkets.com",
})
_REGULATORY_KINDS = frozenset({"filing", "sec_filing", "regulatory_filing"})
_EXCHANGE_KINDS = frozenset({"exchange_notice"})

# Freshness decay bands, keyed by age in seconds at scoring time.
_FRESHNESS_BANDS = (
    (3_600, 1.00),        # <= 1 hour
    (86_400, 0.80),       # <= 1 day
    (7 * 86_400, 0.50),   # <= 1 week
    (30 * 86_400, 0.25),  # <= 1 month
)
_FRESHNESS_FLOOR = 0.05  # older than a month, or no timestamp at all

# Catalyst relevance by verdict: a confident supports/rejects call is more
# decision-relevant than "mixed", and "insufficient" -- while an honest and
# valuable answer -- is not itself catalyst evidence, so it scores low here
# without being penalized elsewhere. Missing/unknown verdict is the floor.
_VERDICT_RELEVANCE: Dict[str, float] = {
    "supports": 1.00,
    "rejects": 1.00,
    "mixed": 0.55,
    "insufficient": 0.25,
}
_VERDICT_RELEVANCE_FLOOR = 0.05

# Classification field (task-specific, e.g. external_mover_triage's
# earnings_gap/short_squeeze/news_breakout/momentum_anomaly/unknown).
# "unknown" is an honest answer, not a bad one, but it carries no specific
# catalyst information -- scored lower than a named classification without
# being treated as a missing/invalid field.
_CLASSIFICATION_RELEVANCE_UNKNOWN = 0.40
_CLASSIFICATION_RELEVANCE_NAMED = 1.00
_CLASSIFICATION_RELEVANCE_MISSING = 0.10


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _source_domain(locator: str) -> Optional[str]:
    """Return a conservative organization-level domain for HTTP(S) URLs."""
    try:
        parsed = urlparse(locator)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    host = parsed.netloc.lower().strip()
    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    labels = [label for label in host.split(".") if label]
    if len(labels) < 2:
        return None
    # Keep the registrable label for common country-code second-level
    # suffixes; otherwise collapse arbitrary subdomains to one organization.
    if len(labels) >= 3 and ".".join(labels[-2:]) in {
        "co.uk", "com.au", "co.jp", "co.nz", "com.br", "com.cn",
    }:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def score_source_authority(source_refs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Highest-authority source present, plus the full per-source breakdown.

    Uses the max rather than the average deliberately: one primary filing
    alongside three low-grade blog mentions is still primary-source-backed
    evidence. An evidence set with NO sources scores the floor (rule 1).
    """
    if not source_refs:
        return {"score": 0.0, "per_source": [], "note": "no source_refs supplied"}
    per_source = []
    best = _SOURCE_AUTHORITY_FLOOR
    for ref in source_refs:
        if not isinstance(ref, dict):
            per_source.append({"kind": None, "authority": _SOURCE_AUTHORITY_FLOOR})
            continue
        kind = str(ref.get("kind") or "").strip().lower()
        domain = _source_domain(str(ref.get("locator") or "").strip())
        domain_authority = None
        if domain in _REGULATORY_DOMAINS:
            domain_authority = 1.0
        elif domain in _EXCHANGE_DOMAINS:
            domain_authority = 0.95

        declared_authority = _SOURCE_AUTHORITY.get(kind, _SOURCE_AUTHORITY_FLOOR)
        if kind in _REGULATORY_KINDS and domain not in _REGULATORY_DOMAINS:
            declared_authority = _SOURCE_AUTHORITY_FLOOR
        elif kind in _EXCHANGE_KINDS and domain not in _EXCHANGE_DOMAINS:
            declared_authority = _SOURCE_AUTHORITY_FLOOR
        authority = max(declared_authority, domain_authority or _SOURCE_AUTHORITY_FLOOR)
        per_source.append({
            "kind": kind or None,
            "domain": domain,
            "authority": authority,
            "domain_verified": domain_authority is not None,
        })
        best = max(best, authority)
    return {"score": round(best, 4), "per_source": per_source}


def score_freshness(source_refs: Sequence[Dict[str, Any]], *, now_ts: int) -> Dict[str, Any]:
    """Freshest usable timestamp across sources, decayed by age band.

    Prefers published_ts, then observed_ts. ``retrieved_ts`` is intentionally
    excluded: fetching a year-old article now does not make its catalyst
    fresh. A source with no knowable-time timestamp scores the floor.
    """
    if not source_refs:
        return {"score": 0.0, "newest_age_s": None}
    best_score = _FRESHNESS_FLOOR
    newest_age: Optional[int] = None
    any_ts = False
    for ref in source_refs:
        if not isinstance(ref, dict):
            continue
        ts = None
        for field in ("published_ts", "observed_ts"):
            candidate = _num(ref.get(field))
            if candidate is not None:
                ts = int(candidate)
                break
        if ts is None:
            continue
        any_ts = True
        age = max(0, now_ts - ts)
        if newest_age is None or age < newest_age:
            newest_age = age
        band_score = _FRESHNESS_FLOOR
        for max_age_s, value in _FRESHNESS_BANDS:
            if age <= max_age_s:
                band_score = value
                break
        best_score = max(best_score, band_score)
    if not any_ts:
        return {"score": _FRESHNESS_FLOOR, "newest_age_s": None, "note": "no source timestamps supplied"}
    return {"score": round(best_score, 4), "newest_age_s": newest_age}


def score_corroboration(source_refs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Reward independent domains, not raw source count.

    Diminishing returns via 1 - 1/(1 + n): 1 domain -> 0.50, 2 -> 0.667,
    3 -> 0.75, 4 -> 0.80 -- a second independent confirmation matters a lot,
    a fifth barely moves the needle. Sources that share a domain, or whose
    locator can't be parsed into one, count once or not at all -- never
    inflate the independent-domain count (rule 2).
    """
    if not source_refs:
        return {"score": 0.0, "independent_domains": 0, "domains": []}
    domains = set()
    for ref in source_refs:
        if not isinstance(ref, dict):
            continue
        locator = str(ref.get("locator") or "").strip()
        if not locator:
            continue
        domain = _source_domain(locator)
        if domain:
            domains.add(domain)
    n = len(domains)
    score = 1.0 - 1.0 / (1.0 + n) if n > 0 else 0.0
    return {"score": round(score, 4), "independent_domains": n, "domains": sorted(domains)}


def score_contradiction(
    verdict: Optional[str],
    classification: Optional[str],
    *,
    sibling_evidence: Sequence[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    """1.0 if no contradicting sibling evidence exists yet, else discounted.

    ``sibling_evidence`` is every OTHER accepted submission for the same
    task (the multi-agent consensus case this exists for -- a second
    worker, e.g. an independent Codex agent, disagreeing on verdict or
    classification). With a single agent per task this is always a clean
    1.0; the dimension is built now specifically so consensus disagreement
    has somewhere honest to register the moment a second agent exists. This
    can only subtract (rule 3) -- agreement never raises the score above 1.0.
    """
    own_verdict = (verdict or "").strip().lower() or None
    own_class = (classification or "").strip().lower() or None
    conflicts = []
    for sibling in sibling_evidence:
        if not isinstance(sibling, dict):
            continue
        s_verdict = (sibling.get("verdict") or "").strip().lower() or None
        s_class = (sibling.get("classification") or "").strip().lower() or None
        verdict_conflict = bool(own_verdict and s_verdict and own_verdict != s_verdict
                                 and {own_verdict, s_verdict} != {"mixed", "insufficient"})
        class_conflict = bool(
            own_class and s_class and own_class != s_class
            and "unknown" not in (own_class, s_class)
        )
        if verdict_conflict or class_conflict:
            conflicts.append({
                "agent_id": sibling.get("agent_id"),
                "verdict": s_verdict,
                "classification": s_class,
                "verdict_conflict": verdict_conflict,
                "classification_conflict": class_conflict,
            })
    if not conflicts:
        return {"score": 1.0, "conflicts": []}
    # Each additional independent conflict costs more (diminishing floor at 0.2).
    penalty = min(0.8, 0.35 * len(conflicts))
    return {"score": round(1.0 - penalty, 4), "conflicts": conflicts}


def score_catalyst_relevance(verdict: Optional[str], classification: Optional[str]) -> Dict[str, Any]:
    """How decision-relevant the evidence's own conclusion is.

    Deliberately separate from source_authority/freshness/corroboration,
    which measure the EVIDENCE; this measures the CONCLUSION. A perfectly
    sourced, perfectly fresh piece of evidence whose own verdict is
    "insufficient" should not score as if it identified a catalyst.
    """
    verdict_key = (verdict or "").strip().lower()
    verdict_score = _VERDICT_RELEVANCE.get(verdict_key, _VERDICT_RELEVANCE_FLOOR)

    class_key = (classification or "").strip().lower()
    if not class_key:
        class_score = _CLASSIFICATION_RELEVANCE_MISSING
    elif class_key == "unknown":
        class_score = _CLASSIFICATION_RELEVANCE_UNKNOWN
    else:
        class_score = _CLASSIFICATION_RELEVANCE_NAMED

    # Geometric mean: a named classification paired with "insufficient" (the
    # honest BRNX case) should read as low-relevance overall, not averaged up
    # by the classification alone.
    combined = math.sqrt(max(verdict_score, 1e-6) * max(class_score, 1e-6))
    return {
        "score": round(combined, 4),
        "verdict_component": verdict_score,
        "classification_component": class_score,
    }


def score_evidence(
    *,
    claims: Dict[str, Any],
    source_refs: Sequence[Dict[str, Any]],
    now_ts: int,
    sibling_evidence: Sequence[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    """Full five-dimension deterministic score for one accepted evidence row.

    Returns every dimension's sub-score alongside the weighted composite, so
    the shadow ledger can persist (and a dashboard can show) exactly which
    dimension drove the number -- never just a bare float.
    """
    claims = claims if isinstance(claims, dict) else {}
    verdict = claims.get("verdict")
    classification = claims.get("classification")
    refs = [r for r in (source_refs or []) if isinstance(r, dict)]

    authority = score_source_authority(refs)
    freshness = score_freshness(refs, now_ts=now_ts)
    corroboration = score_corroboration(refs)
    contradiction = score_contradiction(verdict, classification, sibling_evidence=sibling_evidence)
    relevance = score_catalyst_relevance(verdict, classification)

    dims = {
        "source_authority": authority["score"],
        "freshness": freshness["score"],
        "corroboration": corroboration["score"],
        "contradiction": contradiction["score"],
        "catalyst_relevance": relevance["score"],
    }
    composite = sum(dims[k] * _WEIGHTS[k] for k in _WEIGHTS)

    return {
        "scoring_version": SCORING_VERSION,
        "composite_score": round(composite, 4),
        "weights": dict(_WEIGHTS),
        "dimensions": dims,
        "detail": {
            "source_authority": authority,
            "freshness": freshness,
            "corroboration": corroboration,
            "contradiction": contradiction,
            "catalyst_relevance": relevance,
        },
    }
