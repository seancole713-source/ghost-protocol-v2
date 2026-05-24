"""roadmap #1b — short-interest squeeze wired into the Ghost Score composite.

Kept in a separate file so it doesn't collide with the other PRs' appends to
tests/test_wolf_app_core.py.
"""
import time


def test_squeeze_signal_bands_and_modifier():
    import api.wolf_endpoints as we
    assert we.squeeze_signal(None, None) == {"risk": None, "modifier": 1.0,
                                             "short_float_pct": None, "days_to_cover": None}
    extreme = we.squeeze_signal(40, 1)      # >=35% float
    assert extreme["risk"] == "extreme" and extreme["modifier"] == 1.08
    high = we.squeeze_signal(28, 1)
    assert high["risk"] == "high" and high["modifier"] == 1.05
    medium = we.squeeze_signal(18, 1)
    assert medium["risk"] == "medium" and medium["modifier"] == 1.02
    low = we.squeeze_signal(5, 0.5)
    assert low["risk"] == "low" and low["modifier"] == 1.0


def test_compute_ghost_score_applies_squeeze_modifier():
    import api.wolf_endpoints as we
    now = int(time.time())
    base = dict(
        latest_pick={"direction": "BUY", "confidence": 0.80, "predicted_at": now - 60},
        volume_ratio=1.0, sector={"signal": None},
        current_price=50.0, sma_5d=50.0, now_ts=now,
    )
    # raw: model 32 + volume 10 + sector 7.5 + momentum 7.5 + freshness 10 = 67
    neutral = we.compute_ghost_score(**base, regime={"modifier": 1.0}, squeeze={"modifier": 1.0})
    assert neutral["raw_score"] == 67.0 and neutral["score"] == 67.0

    sq = we.compute_ghost_score(**base, regime={"modifier": 1.0},
                                squeeze=we.squeeze_signal(40, 6))   # extreme => x1.08
    assert sq["raw_score"] == 67.0
    assert sq["score"] == 72.4                                       # round(67 * 1.08, 1)
    assert sq["squeeze"]["risk"] == "extreme"


def test_compute_ghost_score_squeeze_stacks_with_regime_and_clamps():
    import api.wolf_endpoints as we
    now = int(time.time())
    # Maxed components + bullish regime + extreme squeeze must not exceed 100.
    out = we.compute_ghost_score(
        latest_pick={"direction": "BUY", "confidence": 0.99, "predicted_at": now - 60},
        volume_ratio=3.0, sector={"signal": "wolf_lagging_up"},
        current_price=60.0, sma_5d=50.0, now_ts=now,
        regime={"modifier": 1.10}, squeeze=we.squeeze_signal(50, 8))
    assert out["score"] <= 100.0


def test_compute_ghost_score_squeeze_defaults_neutral():
    import api.wolf_endpoints as we
    now = int(time.time())
    # No squeeze arg => modifier 1.0, score unchanged from raw (neutral regime).
    out = we.compute_ghost_score(
        latest_pick=None, volume_ratio=None, sector=None,
        current_price=None, sma_5d=None, now_ts=now)
    assert out["squeeze"] is None
    assert out["score"] == out["raw_score"]
