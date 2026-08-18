"""Tests for core/catalyst_freshness.py — pure, no I/O."""
from core import catalyst_freshness as cf


def test_freshness_weight_decays_with_age():
    assert cf.freshness_weight(1) == 1.0
    assert cf.freshness_weight(10) == 0.85
    assert cf.freshness_weight(48) == 0.65
    assert cf.freshness_weight(100) == 0.45
    assert cf.freshness_weight(200) == 0.25
    assert cf.freshness_weight(400) == 0.10
    assert cf.freshness_weight(1000) == 0.05
    assert cf.freshness_weight(-5) == 0.0


def test_classify_age_buckets():
    assert cf.classify_age(3)["bucket"] == "fresh"
    assert cf.classify_age(12)["bucket"] == "developing"
    assert cf.classify_age(48)["bucket"] == "recent"
    assert cf.classify_age(100)["bucket"] == "aging"
    assert cf.classify_age(200)["bucket"] == "stale"
    assert cf.classify_age(400)["bucket"] == "old"
    assert cf.classify_age(1000)["bucket"] == "ancient"


def test_future_catalyst_weighted_by_distance():
    now = 1_000_000.0
    # 1 day out = imminent.
    c = cf.classify_catalyst(asof_ts=None, now_ts=now, scheduled_ts=now + 86400)
    assert c["state"] == "future_imminent"
    assert c["weight"] == 0.9
    # 6 months out = distant, ~irrelevant to 1-14 day trade.
    c = cf.classify_catalyst(asof_ts=None, now_ts=now, scheduled_ts=now + 180 * 86400)
    assert c["state"] == "future_distant"
    assert c["weight"] == 0.03


def test_stale_catalyst_does_not_dominate():
    """SPCE lesson: a 30-day-old catalyst must score near zero."""
    now = 1_000_000.0
    events = [{"event_type": "earnings_beat", "asof_ts": now - 30 * 86400, "materiality": 0.9, "source_reliability": 0.9, "effect": 0.7}]
    out = cf.catalyst_timing_score(events, now_ts=now)
    assert out["score"] < 15.0  # stale → low


def test_fresh_catalyst_scores_high():
    now = 1_000_000.0
    events = [{"event_type": "earnings_beat", "asof_ts": now - 3600, "materiality": 0.9, "source_reliability": 0.9, "effect": 0.7}]
    out = cf.catalyst_timing_score(events, now_ts=now)
    assert out["score"] > 40.0  # fresh → high


def test_earnings_surprise_relative_not_absolute():
    """A loss smaller than expected is still a positive surprise."""
    out = cf.score_earnings_surprise(eps_actual=-0.10, eps_expected=-0.50)
    assert out["eps_surprise_pct"] == 80.0  # (-0.10 - -0.50)/0.50 = +80%
    assert out["score"] > 50.0


def test_earnings_surprise_beat_and_guidance_raise():
    out = cf.score_earnings_surprise(
        eps_actual=1.20, eps_expected=1.00,
        revenue_actual=500, revenue_expected=450,
        guidance_change=0.5,
    )
    assert out["eps_surprise_pct"] == 20.0
    assert out["revenue_surprise_pct"] == 11.11
    assert out["score"] > 60.0


def test_earnings_surprise_missing_is_unavailable():
    out = cf.score_earnings_surprise()
    assert out["available"] is False
    assert out["score"] == 0.0
