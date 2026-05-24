"""roadmap #4b — performance attribution (A features, B components). New file."""


def test_feature_attribution_win_loss_and_regime():
    from core.attribution import feature_attribution
    trades = [
        {"outcome": "WIN", "features": {"rsi": 60, "volume_ratio": 1.5}, "regime_label": "Trend-up"},
        {"outcome": "WIN", "features": {"rsi": 64, "volume_ratio": 2.0}, "regime_label": "Trend-up"},
        {"outcome": "LOSS", "features": {"rsi": 48, "volume_ratio": 1.0}, "regime_label": "Choppy"},
    ]
    out = feature_attribution(trades)
    assert out["wins"] == 2 and out["losses"] == 1
    rsi = next(f for f in out["features"] if f["feature"] == "rsi")
    assert rsi["win_avg"] == 62.0 and rsi["loss_avg"] == 48.0 and rsi["delta"] == 14.0
    by = {r["regime"]: r for r in out["by_regime"]}
    assert by["Trend-up"]["win_rate_pct"] == 100.0
    assert by["Choppy"]["win_rate_pct"] == 0.0


def test_component_attribution_availability():
    from core.attribution import component_attribution
    # no journaled components yet
    empty = component_attribution([{"outcome": "WIN", "features": {}}])
    assert empty["available"] is False
    # with components
    trades = [
        {"outcome": "WIN", "components": {"model": 36, "volume": 18, "sector": None, "momentum": 12, "freshness": 10}},
        {"outcome": "LOSS", "components": {"model": 30, "volume": 8, "sector": None, "momentum": 3, "freshness": 10}},
    ]
    out = component_attribution(trades)
    assert out["available"] is True
    model = next(c for c in out["components"] if c["component"] == "model")
    assert model["win_avg"] == 36.0 and model["loss_avg"] == 30.0 and model["delta"] == 6.0
    sector = next(c for c in out["components"] if c["component"] == "sector")
    assert sector["win_avg"] is None   # never journaled


def test_ghost_components_snapshot():
    from core.attribution import ghost_components
    now = 1_000_000
    c = ghost_components(0.90, "UP", {"volume_ratio": 2.0, "mom_4h": 0.05}, now - 60, now)
    assert c["model"] == 36.0          # 0.90 * 40
    assert c["volume"] == 20.0         # min(20, 2.0*10)
    assert c["momentum"] == 15.0       # mom >= 0.03
    assert c["freshness"] == 10.0      # 60s old
    assert c["sector"] is None


def test_ghost_components_matches_live_scorers():
    """Drift guard: the B snapshot must agree with the live ghost-score scorers
    in api.wolf_endpoints for the components it computes."""
    import api.wolf_endpoints as we
    from core.attribution import ghost_components
    now = 1_000_000
    feats = {"volume_ratio": 1.3, "mom_4h": 0.012}
    c = ghost_components(0.83, "BUY", feats, now - 300, now)
    assert c["model"] == we._score_model({"direction": "BUY", "confidence": 0.83})
    assert c["volume"] == we._score_volume(1.3)
    # momentum band parity (the live scorer keys off price/sma; both map mom>=0.01 -> 12.0)
    assert c["momentum"] == 12.0
    assert c["freshness"] == we._score_freshness(now - 300, now)
