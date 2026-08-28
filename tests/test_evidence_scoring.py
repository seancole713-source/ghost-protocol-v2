"""Tests for the deterministic evidence-scoring engine.

Each test defends one of the three structural honesty rules: missing/
unrecognized fields score the floor (never a neutral average), corroboration
only counts independent domains, and contradiction can only subtract.
"""
from __future__ import annotations

from core import evidence_scoring as es

NOW = 1_800_000_000


def _ref(kind=None, locator=None, published_ts=None):
    ref = {}
    if kind is not None:
        ref["kind"] = kind
    if locator is not None:
        ref["locator"] = locator
    if published_ts is not None:
        ref["published_ts"] = published_ts
    return ref


# --------------------------------------------------------------- authority --

def test_no_sources_scores_the_floor_not_a_neutral_average():
    result = es.score_source_authority([])
    assert result["score"] == 0.0


def test_unrecognized_source_kind_scores_the_floor():
    result = es.score_source_authority([_ref(kind="mystery_blog")])
    assert result["score"] == es._SOURCE_AUTHORITY_FLOOR


def test_authority_takes_the_best_source_present():
    refs = [
        _ref(kind="web_search", locator="https://example.com/search"),
        _ref(kind="sec_filing", locator="https://sec.gov/Archives/filing"),
        _ref(kind="social_media", locator="https://x.com/post"),
    ]
    result = es.score_source_authority(refs)
    assert result["score"] == es._SOURCE_AUTHORITY["sec_filing"]


def test_privileged_kind_does_not_inflate_unrelated_domain():
    result = es.score_source_authority([
        _ref(kind="sec_filing", locator="https://random-blog.example/post"),
    ])
    assert result["score"] == es._SOURCE_AUTHORITY_FLOOR


def test_provider_web_search_ref_infers_regulatory_authority_from_domain():
    result = es.score_source_authority([
        _ref(kind="web_search", locator="https://www.sec.gov/Archives/filing"),
    ])
    assert result["score"] == 1.0
    assert result["per_source"][0]["domain_verified"] is True


# --------------------------------------------------------------- freshness --

def test_no_timestamps_scores_the_freshness_floor():
    refs = [_ref(kind="news_article", locator="https://a.com/x")]
    result = es.score_freshness(refs, now_ts=NOW)
    assert result["score"] == es._FRESHNESS_FLOOR
    assert result["newest_age_s"] is None


def test_one_hour_old_source_scores_top_band():
    refs = [_ref(published_ts=NOW - 1800)]
    result = es.score_freshness(refs, now_ts=NOW)
    assert result["score"] == 1.0


def test_month_old_source_scores_near_floor():
    refs = [_ref(published_ts=NOW - 60 * 86400)]
    result = es.score_freshness(refs, now_ts=NOW)
    assert result["score"] == es._FRESHNESS_FLOOR


def test_recent_retrieval_does_not_make_old_source_fresh():
    refs = [{"retrieved_ts": NOW - 10}]
    result = es.score_freshness(refs, now_ts=NOW)
    assert result["score"] == es._FRESHNESS_FLOOR
    assert result["newest_age_s"] is None


# ----------------------------------------------------------- corroboration --

def test_same_domain_repeated_does_not_inflate_corroboration():
    refs = [
        _ref(locator="https://www.sec.gov/a"),
        _ref(locator="https://sec.gov/b"),   # same domain once www. stripped
        _ref(locator="https://sec.gov/c"),
    ]
    result = es.score_corroboration(refs)
    assert result["independent_domains"] == 1


def test_distinct_domains_increase_corroboration_with_diminishing_returns():
    one = es.score_corroboration([_ref(locator="https://a.com/1")])
    two = es.score_corroboration([_ref(locator="https://a.com/1"), _ref(locator="https://b.com/2")])
    three = es.score_corroboration([
        _ref(locator="https://a.com/1"), _ref(locator="https://b.com/2"), _ref(locator="https://c.com/3"),
    ])
    assert 0.0 < one["score"] < two["score"] < three["score"]
    gain_1_to_2 = two["score"] - one["score"]
    gain_2_to_3 = three["score"] - two["score"]
    assert gain_2_to_3 < gain_1_to_2  # diminishing returns


def test_unparseable_locator_is_excluded_not_guessed():
    refs = [_ref(locator=""), _ref(locator="not a url at all !!")]
    result = es.score_corroboration(refs)
    assert result["independent_domains"] == 0


def test_subdomains_of_one_organization_are_not_independent_sources():
    refs = [
        _ref(locator="https://finance.yahoo.com/story"),
        _ref(locator="https://news.yahoo.com/story"),
    ]
    result = es.score_corroboration(refs)
    assert result["independent_domains"] == 1
    assert result["domains"] == ["yahoo.com"]


# ----------------------------------------------------------- contradiction --

def test_no_siblings_means_no_contradiction():
    result = es.score_contradiction("supports", "short_squeeze", sibling_evidence=[])
    assert result["score"] == 1.0
    assert result["conflicts"] == []


def test_conflicting_verdict_from_sibling_lowers_score():
    siblings = [{"agent_id": "other-agent", "verdict": "rejects", "classification": "short_squeeze"}]
    result = es.score_contradiction("supports", "short_squeeze", sibling_evidence=siblings)
    assert result["score"] < 1.0
    assert len(result["conflicts"]) == 1


def test_agreeing_siblings_do_not_raise_score_above_one():
    siblings = [{"agent_id": "other-agent", "verdict": "supports", "classification": "short_squeeze"}]
    result = es.score_contradiction("supports", "short_squeeze", sibling_evidence=siblings)
    assert result["score"] == 1.0


def test_insufficient_and_mixed_are_not_treated_as_a_conflict():
    """Both mean 'the evidence didn't resolve it' -- not disagreement."""
    siblings = [{"agent_id": "other-agent", "verdict": "mixed", "classification": None}]
    result = es.score_contradiction("insufficient", None, sibling_evidence=siblings)
    assert result["score"] == 1.0


def test_two_independent_conflicts_cost_more_than_one():
    one_conflict = es.score_contradiction(
        "supports", "short_squeeze",
        sibling_evidence=[{"agent_id": "a", "verdict": "rejects", "classification": "short_squeeze"}],
    )
    two_conflicts = es.score_contradiction(
        "supports", "short_squeeze",
        sibling_evidence=[
            {"agent_id": "a", "verdict": "rejects", "classification": "short_squeeze"},
            {"agent_id": "b", "verdict": "rejects", "classification": "earnings_gap"},
        ],
    )
    assert two_conflicts["score"] < one_conflict["score"]


# ------------------------------------------------------- catalyst relevance --

def test_insufficient_verdict_scores_low_relevance_even_with_named_classification():
    """Geometric mean of insufficient (0.25) and named (1.00) is exactly 0.5 --
    clearly below a confident call's >0.9, well above the floor. Moderate,
    not high, which is the point: a named classification can't rescue an
    honestly unresolved verdict."""
    result = es.score_catalyst_relevance("insufficient", "momentum_anomaly")
    assert result["score"] <= 0.5
    assert result["score"] < es.score_catalyst_relevance("supports", "momentum_anomaly")["score"]


def test_supports_with_named_classification_scores_high_relevance():
    result = es.score_catalyst_relevance("supports", "short_squeeze")
    assert result["score"] > 0.9


def test_unknown_classification_scores_lower_than_named_but_not_floor():
    named = es.score_catalyst_relevance("supports", "short_squeeze")
    unknown = es.score_catalyst_relevance("supports", "unknown")
    missing = es.score_catalyst_relevance("supports", None)
    assert missing["score"] < unknown["score"] < named["score"]


# ------------------------------------------------------------ full composite --

def test_composite_score_is_deterministic_for_identical_input():
    claims = {"verdict": "insufficient", "classification": "momentum_anomaly"}
    refs = [{"kind": "exchange_notice", "locator": "https://nasdaqtrader.com/x", "published_ts": NOW - 3600}]
    first = es.score_evidence(claims=claims, source_refs=refs, now_ts=NOW)
    second = es.score_evidence(claims=claims, source_refs=refs, now_ts=NOW)
    assert first == second


def test_empty_evidence_scores_near_zero_not_neutral():
    """~0.154: contradiction trivially defaults to 1.0 (nothing to conflict
    with) which is the ceiling, never a bonus above it -- rule 3 -- so the
    other four dimensions being zeroed still pulls the composite far below
    a neutral 0.5."""
    result = es.score_evidence(claims={}, source_refs=[], now_ts=NOW)
    assert result["composite_score"] < 0.2
    assert result["composite_score"] < 0.5 * 0.6  # well under half of neutral


def test_well_sourced_confident_evidence_scores_high():
    claims = {"verdict": "supports", "classification": "earnings_gap"}
    refs = [
        {"kind": "sec_filing", "locator": "https://sec.gov/a", "published_ts": NOW - 600},
        {"kind": "exchange_notice", "locator": "https://nasdaqtrader.com/b", "published_ts": NOW - 900},
    ]
    result = es.score_evidence(claims=claims, source_refs=refs, now_ts=NOW)
    assert result["composite_score"] > 0.75


def test_weights_sum_to_one():
    assert abs(sum(es._WEIGHTS.values()) - 1.0) < 1e-9


def test_brnx_style_insufficient_call_scores_moderately_not_high_not_zero():
    """Regression fixture: mirrors the real BRNX evidence submitted in this
    session -- well-sourced (4 real primary/secondary domains) but an honest
    'insufficient' verdict because no same-day catalyst was found. This
    should score meaningfully above zero (the sourcing was genuinely good)
    but well below a confident, catalyst-confirmed call."""
    claims = {"verdict": "insufficient", "classification": "momentum_anomaly"}
    refs = [
        {"kind": "exchange_notice", "locator": "https://www.nasdaqtrader.com/x", "published_ts": NOW - 86400},
        {"kind": "press_release", "locator": "https://www.newsfilecorp.com/y", "published_ts": NOW - 86400 * 2},
        {"kind": "news_article", "locator": "https://finance.yahoo.com/z", "published_ts": NOW - 86400 * 3},
        {"kind": "equity_research", "locator": "https://simplywall.st/w", "published_ts": NOW - 86400 * 5},
    ]
    confident = es.score_evidence(
        claims={"verdict": "supports", "classification": "momentum_anomaly"},
        source_refs=refs, now_ts=NOW,
    )
    honest_insufficient = es.score_evidence(claims=claims, source_refs=refs, now_ts=NOW)
    assert 0.25 < honest_insufficient["composite_score"] < confident["composite_score"]
