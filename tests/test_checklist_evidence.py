"""Issue-time evidence must be complete, nonfuture, fresh, and nonconflicting."""
from __future__ import annotations

from core import checklist_evidence as ce


def _complete_record(*, value=3.0, source="feed-a", timestamp=1_000):
    return ce._record(
        source=source,
        source_timestamp=timestamp,
        observation_timestamp=timestamp,
        reporting_period="issue_time",
        unit="ratio",
        basis="point_in_time",
        actual_value=value,
        methodology="test methodology",
        request_timestamp=1_100,
    )


def test_confirmed_projection_accepts_complete_fresh_nonfuture_evidence():
    scalar, reconciled, bounded = ce._confirmed_projection(
        "relative_volume",
        [_complete_record()],
        asof_ts=1_100,
        max_age_s=200,
    )
    assert scalar == 3.0
    assert reconciled["confidence_status"] == ce.CONFIRMED
    assert bounded[0]["confidence_status"] == ce.CONFIRMED


def test_confirmed_projection_rejects_incomplete_timestamp_chain():
    record = _complete_record()
    record["source_timestamp"] = None
    scalar, reconciled, bounded = ce._confirmed_projection(
        "relative_volume", [record], asof_ts=1_100, max_age_s=200,
    )
    assert scalar is None
    assert reconciled["confidence_status"] != ce.CONFIRMED
    assert bounded[0]["confidence_status"] == "UNVERIFIED"


def test_confirmed_projection_rejects_future_and_stale_evidence():
    future_scalar, _, future_rows = ce._confirmed_projection(
        "relative_volume", [_complete_record(timestamp=1_101)], asof_ts=1_100, max_age_s=200,
    )
    stale_scalar, _, stale_rows = ce._confirmed_projection(
        "relative_volume", [_complete_record(timestamp=800)], asof_ts=1_100, max_age_s=200,
    )
    assert future_scalar is None
    assert future_rows[0]["future_of_decision"] is True
    assert stale_scalar is None
    assert stale_rows[0]["stale_at_decision"] is True


def test_confirmed_projection_rejects_conflicting_confirmed_claims():
    scalar, reconciled, _ = ce._confirmed_projection(
        "relative_volume",
        [
            _complete_record(value=2.0, source="feed-a"),
            _complete_record(value=4.0, source="feed-b"),
        ],
        asof_ts=1_100,
        max_age_s=200,
    )
    assert scalar is None
    assert reconciled["confidence_status"] == ce.VERIFIED_CONFLICT
    assert reconciled["data_conflict"]


def test_collect_evidence_does_not_launder_request_time_as_source_time(monkeypatch):
    monkeypatch.setattr(
        ce,
        "_default_market_ctx",
        lambda symbol: {"relative_volume": 5.0},
    )
    evidence = ce.collect_evidence("WOLF", asof_ts=1_100)
    assert "relative_volume" not in evidence
    record = evidence[ce.RECORDS_KEY]["relative_volume"][0]
    assert record["source_timestamp"] is None
    assert record["confidence_status"] == "UNVERIFIED"


def test_collect_evidence_scores_only_frozen_feature_timestamp():
    evidence = ce.collect_evidence(
        "WOLF",
        asof_ts=1_100,
        market_ctx={"relative_volume": 5.0, "feature_asof_ts": 1_000},
    )
    assert evidence["relative_volume"] == 5.0
    assert ce.sources_for(evidence)["relative_volume"] == "prediction_feature_snapshot"


# ---------------------------------------------------------- catalyst wiring --
#
# These signals were previously dead-wired: the collectors existed and were
# unit-testable but collect_evidence() never called them, and a static
# UNSUPPORTED_KEY list stood in for all seven regardless of whether a genuine
# point-in-time timestamp was actually available. The tests below pin that
# they are now live and still obey the same no-lookahead/no-stale rules as
# the market-feature signals above.

def test_earnings_surprise_reaches_evidence_with_its_own_report_timestamp(monkeypatch):
    monkeypatch.setattr(
        ce, "_collect_earnings",
        lambda symbol, *, asof_ts: {"earnings_surprise_pct": {"value": 42.0, "_ts": 1_000}},
    )
    monkeypatch.setattr(ce, "_collect_fundamentals", lambda symbol, *, asof_ts: {})
    monkeypatch.setattr(ce, "_collect_news", lambda symbol, *, asof_ts: {})
    monkeypatch.setattr(ce, "_collect_leadership_change", lambda symbol, *, asof_ts: {})

    evidence = ce.collect_evidence("WOLF", asof_ts=1_100, market_ctx={})

    assert evidence["earnings_surprise_pct"] == 42.0
    record = evidence[ce.RECORDS_KEY]["earnings_surprise_pct"][0]
    assert record["source_timestamp"] == 1_000
    assert record["source"] == "Earnings report"
    assert ce.UNSUPPORTED_KEY not in evidence or evidence[ce.UNSUPPORTED_KEY] == []


def test_a_report_from_after_decision_time_is_rejected_not_laundered(monkeypatch):
    """Mirrors test_confirmed_projection_rejects_future_and_stale_evidence for
    the catalyst-class signals: a future-of-decision fact must never leak in,
    even though it arrives through a different code path than market features."""
    monkeypatch.setattr(
        ce, "_collect_earnings",
        lambda symbol, *, asof_ts: {"earnings_surprise_pct": {"value": 42.0, "_ts": 1_101}},
    )
    monkeypatch.setattr(ce, "_collect_fundamentals", lambda symbol, *, asof_ts: {})
    monkeypatch.setattr(ce, "_collect_news", lambda symbol, *, asof_ts: {})
    monkeypatch.setattr(ce, "_collect_leadership_change", lambda symbol, *, asof_ts: {})

    evidence = ce.collect_evidence("WOLF", asof_ts=1_100, market_ctx={})

    assert "earnings_surprise_pct" not in evidence
    record = evidence[ce.RECORDS_KEY]["earnings_surprise_pct"][0]
    assert record["future_of_decision"] is True


def test_a_stale_leadership_change_beyond_30_days_drops_out(monkeypatch):
    monkeypatch.setattr(ce, "_collect_earnings", lambda symbol, *, asof_ts: {})
    monkeypatch.setattr(ce, "_collect_fundamentals", lambda symbol, *, asof_ts: {})
    monkeypatch.setattr(ce, "_collect_news", lambda symbol, *, asof_ts: {})
    decision_ts = 1_800_000_000
    stale_ts = decision_ts - 31 * 86400
    monkeypatch.setattr(
        ce, "_collect_leadership_change",
        lambda symbol, *, asof_ts: {
            "leadership_change_sentiment": {"value": -0.3, "_ts": stale_ts},
        },
    )

    evidence = ce.collect_evidence("WOLF", asof_ts=decision_ts, market_ctx={})

    assert "leadership_change_sentiment" not in evidence
    record = evidence[ce.RECORDS_KEY]["leadership_change_sentiment"][0]
    assert record["stale_at_decision"] is True


def test_a_leadership_change_within_30_days_is_confirmed(monkeypatch):
    monkeypatch.setattr(ce, "_collect_earnings", lambda symbol, *, asof_ts: {})
    monkeypatch.setattr(ce, "_collect_fundamentals", lambda symbol, *, asof_ts: {})
    monkeypatch.setattr(ce, "_collect_news", lambda symbol, *, asof_ts: {})
    decision_ts = 1_800_000_000
    recent_ts = decision_ts - 5 * 86400
    monkeypatch.setattr(
        ce, "_collect_leadership_change",
        lambda symbol, *, asof_ts: {
            "leadership_change_sentiment": {"value": -0.3, "_ts": recent_ts},
        },
    )

    evidence = ce.collect_evidence("WOLF", asof_ts=decision_ts, market_ctx={})

    assert evidence["leadership_change_sentiment"] == -0.3
    assert evidence[ce.RECORDS_KEY]["leadership_change_sentiment"][0]["source"] == "SEC 8-K filing (item 5.02)"


def test_a_signal_with_no_parseable_timestamp_stays_unverified(monkeypatch):
    """No source_timestamp means no proof of when it was knowable -- must be
    rejected the same way a market feature with no feature_asof_ts is."""
    monkeypatch.setattr(
        ce, "_collect_earnings",
        lambda symbol, *, asof_ts: {"earnings_surprise_pct": {"value": 42.0, "_ts": None}},
    )
    monkeypatch.setattr(ce, "_collect_fundamentals", lambda symbol, *, asof_ts: {})
    monkeypatch.setattr(ce, "_collect_news", lambda symbol, *, asof_ts: {})
    monkeypatch.setattr(ce, "_collect_leadership_change", lambda symbol, *, asof_ts: {})

    evidence = ce.collect_evidence("WOLF", asof_ts=1_100, market_ctx={})

    assert "earnings_surprise_pct" not in evidence
    record = evidence[ce.RECORDS_KEY]["earnings_surprise_pct"][0]
    assert record["confidence_status"] == "UNVERIFIED"


def test_margin_change_pct_timestamp_is_the_later_of_eps_and_revenue(monkeypatch):
    """The derived fact is only knowable once *both* inputs are filed."""
    monkeypatch.setattr(
        "core.sec_fundamentals.get_fundamentals",
        lambda symbol, *, asof_ts=None: {
            "available": True,
            "revenue": 480.0, "revenue_year_ago": 420.0, "revenue_filed_ts": 1_000,
            "actual_eps": 0.20, "eps_year_ago": 0.10, "eps_filed_ts": 2_000,
        },
    )

    out = ce._collect_fundamentals("WOLF", asof_ts=9_999)

    assert out["margin_change_pct"]["_ts"] == 2_000


def test_news_sentiment_timestamp_is_the_newest_contributing_event(monkeypatch):
    monkeypatch.setattr(
        "core.news_events.recent_events_for_symbol",
        lambda symbol, *, asof_ts=None, **kwargs: [
            {"sentiment": 0.5, "asof_ts": 900, "event_type": "earnings_beat"},
            {"sentiment": -0.2, "asof_ts": 1_500, "event_type": "guidance_cut"},
        ],
    )

    out = ce._collect_news("WOLF", asof_ts=9_999)

    assert out["news_sentiment"]["_ts"] == 1_500
    assert out["guidance_direction"]["_ts"] == 1_500
    assert out["guidance_direction"]["value"] == -1


def test_price_action_requires_its_own_observation_timestamp():
    evidence = ce.collect_evidence(
        "WOLF",
        asof_ts=1_100,
        market_ctx={
            "price": 12.0,
            "prior_close": 10.0,
            "feature_asof_ts": 1_000,
            "price_as_of_ts": None,
        },
    )

    assert "move_from_base_pct" not in evidence
    record = evidence[ce.RECORDS_KEY]["move_from_base_pct"][0]
    assert record["source_timestamp"] is None
    assert record["confidence_status"] == "UNVERIFIED"
