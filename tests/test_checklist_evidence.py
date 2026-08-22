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
