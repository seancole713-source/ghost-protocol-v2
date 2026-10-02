"""F09: the WOLF catalyst adjustment must not depend on call order.

The shared catalyst cache used to store the adjustment AFTER the
direction-specific misalignment penalty and serve it to either direction
(fresh DOWN -> +0.04, but UP then DOWN -> +0.06).
"""
import pytest

from core import wolf_context as wc

BULLISH = [
    "Wolfspeed awarded contract with GM",
    "Wolfspeed raises guidance on strong revenue",
    "Wolfspeed secures $500 million design win",
    "Wolfspeed beats estimate, record revenue",
]
BEARISH = [
    "Wolfspeed missed estimate, cuts guidance",
    "Wolfspeed delays production ramp, layoffs",
    "Wolfspeed faces lawsuit and downgrade rating",
    "Wolfspeed cancelled contract, production cut",
]


@pytest.fixture
def headlines(monkeypatch):
    state = {"items": BULLISH, "fetches": 0}

    def _fake(query, max_items=10):
        state["fetches"] += 1
        return list(state["items"])

    monkeypatch.setattr(wc, "_fetch_gnews_headlines", _fake)
    monkeypatch.setattr(wc, "_CACHE", {})
    return state


def _fresh(direction, items, monkeypatch):
    monkeypatch.setattr(wc, "_CACHE", {})
    monkeypatch.setattr(wc, "_fetch_gnews_headlines", lambda q, max_items=10: list(items))
    return wc._get_catalyst_news_score(direction)


def test_fresh_values_reproduce_the_documented_case(headlines):
    up = wc._get_catalyst_news_score("UP")
    wc._CACHE.clear()
    down = wc._get_catalyst_news_score("DOWN")
    assert up[0] == 0.06
    assert down[0] == 0.04  # bullish evidence penalises a DOWN call


def test_up_then_down_matches_fresh_down(headlines, monkeypatch):
    up = wc._get_catalyst_news_score("UP")
    down = wc._get_catalyst_news_score("DOWN")
    assert headlines["fetches"] == 3  # evidence fetched once and reused
    assert up[0] == 0.06
    assert down[0] == 0.04
    assert down == _fresh("DOWN", BULLISH, monkeypatch)


def test_down_then_up_matches_fresh_up(headlines, monkeypatch):
    down = wc._get_catalyst_news_score("DOWN")
    up = wc._get_catalyst_news_score("UP")
    assert headlines["fetches"] == 3
    assert down[0] == 0.04
    assert up[0] == 0.06
    assert up == _fresh("UP", BULLISH, monkeypatch)


@pytest.mark.parametrize("items", [BULLISH, BEARISH, ["no signal here"], []])
def test_result_per_direction_is_order_independent(monkeypatch, items):
    fresh = {d: _fresh(d, items, monkeypatch) for d in ("UP", "DOWN")}
    for order in (("UP", "DOWN", "UP"), ("DOWN", "UP", "DOWN")):
        monkeypatch.setattr(wc, "_CACHE", {})
        monkeypatch.setattr(wc, "_fetch_gnews_headlines", lambda q, max_items=10: list(items))
        for d in order:
            assert wc._get_catalyst_news_score(d) == fresh[d]


def test_bearish_evidence_penalises_up_only(monkeypatch):
    up = _fresh("UP", BEARISH, monkeypatch)
    down = _fresh("DOWN", BEARISH, monkeypatch)
    assert up[0] == -0.06
    assert down[0] == -0.06
    # Same evidence, UP first then DOWN from cache: still identical.
    monkeypatch.setattr(wc, "_CACHE", {})
    wc._get_catalyst_news_score("UP")
    assert wc._get_catalyst_news_score("DOWN") == down


def test_cache_holds_only_direction_neutral_evidence(headlines):
    wc._get_catalyst_news_score("DOWN")
    entry = wc._CACHE[wc._CATALYST_EVIDENCE_CACHE_KEY]["value"]
    assert set(entry) == {"n", "avg_score", "strong_bull", "strong_bear", "headline_samples"}
    assert "catalyst_news" not in wc._CACHE


def test_cache_expiry_refetches_and_reapplies_direction(headlines, monkeypatch):
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(wc.time, "time", lambda: clock["t"])
    assert wc._get_catalyst_news_score("UP")[0] == 0.06
    headlines["items"] = BEARISH
    clock["t"] += wc._CACHE_TTL - 1
    assert wc._get_catalyst_news_score("DOWN")[0] == 0.04  # still cached bullish evidence
    assert headlines["fetches"] == 3
    clock["t"] += 2  # past the TTL
    assert wc._get_catalyst_news_score("DOWN")[0] == -0.06
    assert wc._get_catalyst_news_score("UP")[0] == -0.06
    assert headlines["fetches"] == 6


def test_cached_reasons_are_not_shared_mutable_state(headlines):
    _, reasons = wc._get_catalyst_news_score("UP")
    reasons.append("mutated by caller")
    _, again = wc._get_catalyst_news_score("UP")
    assert "mutated by caller" not in again
