"""Watchlist squeeze radar — RVOL + signal bands."""

from core.squeeze_monitor import (
    compute_rvol,
    evaluate_squeeze_signal,
    evaluate_watch_signal,
    format_squeeze_alert,
    prefilter_candidate,
    rth_elapsed_fraction,
    squeeze_confidence,
    squeeze_trade_levels,
)


def test_rvol_doubles_at_half_session_with_full_day_pace():
    # At 50% of session, 50% of avg daily vol => RVOL ~1.0
    rvol = compute_rvol(session_volume=20_000_000, avg_daily_volume=40_000_000, elapsed_frac=0.5)
    assert abs(rvol - 1.0) < 0.01


def test_rvol_spike_when_volume_front_loaded():
    # 30M vol by 10am (25% session) with 40M avg daily => RVOL >> 1
    rvol = compute_rvol(session_volume=30_000_000, avg_daily_volume=40_000_000, elapsed_frac=0.25)
    assert rvol >= 2.5


def test_premarket_rvol_uses_premarket_baseline():
    # Premarket: 3:00 AM volume must NOT be compared against a near-zero RTH
    # fraction. With the premarket baseline (5% of daily), a modest premarket
    # volume reads as a sane RVOL, not 156×.
    rvol = compute_rvol(
        session_volume=1_000_000, avg_daily_volume=40_000_000,
        elapsed_frac=0.1, premarket=True,
    )
    # expected = 40M * 0.05 * 0.1 = 200k; 1M / 200k = 5.0
    assert abs(rvol - 5.0) < 0.01


def test_premarket_rvol_not_exploding_at_open():
    # Same volume, RTH baseline (no premarket flag) would be 1M / (40M*0.1) = 0.25
    rvol = compute_rvol(
        session_volume=1_000_000, avg_daily_volume=40_000_000,
        elapsed_frac=0.1, premarket=False,
    )
    assert abs(rvol - 0.25) < 0.01


def test_rth_elapsed_fraction_premarket_uses_premarket_minutes():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ct = ZoneInfo("America/Chicago")
    # 4:00 AM CT = 60 min into the 330-min premarket session (~18%)
    pre = datetime(2026, 6, 10, 4, 0, tzinfo=ct)
    frac = rth_elapsed_fraction(pre)
    assert 0.15 < frac < 0.25


def test_evaluate_watch_signal_high_recall():
    # A +2.5% move with low RVOL is NOT a trade, but IS a WATCH (detection).
    assert evaluate_watch_signal(2.5, 2.0, 1.0) is True
    # A hot RVOL with a small move is also a WATCH.
    assert evaluate_watch_signal(1.0, 0.5, 2.0) is True
    # A quiet name is not even a WATCH.
    assert evaluate_watch_signal(1.0, 0.5, 0.8) is False


def test_watch_observation_escalates_on_repetition(monkeypatch):
    import core.squeeze_monitor as sm
    sm._watch_observations.clear()
    # First two observations: not escalated.
    r1 = sm._record_watch_observation("ARCT", 2.5, 2.0, 1.0)
    r2 = sm._record_watch_observation("ARCT", 2.6, 2.1, 1.1)
    assert r1["escalated"] is False
    assert r2["escalated"] is False
    assert r2["observations"] == 2
    # Third independent observation escalates.
    r3 = sm._record_watch_observation("ARCT", 2.7, 2.2, 1.2)
    assert r3["escalated"] is True
    assert r3["observations"] == 3
    sm._watch_observations.clear()


def test_evaluate_squeeze_active():
    assert evaluate_squeeze_signal(7.2, 5.0, 3.0, short_risk="high") == "squeeze_active"


def test_evaluate_squeeze_forming_high_short():
    assert evaluate_squeeze_signal(3.5, 3.2, 2.1, short_risk="high") == "squeeze_forming"


def test_no_alert_quiet_name():
    assert evaluate_squeeze_signal(1.0, 0.5, 0.8, short_risk="low") is None


def test_peak_move_catches_fade():
    # Morning high +7%, now faded to +1% — still active if RVOL hot
    assert evaluate_squeeze_signal(7.0, 1.0, 3.0, short_risk="extreme") == "squeeze_active"


def test_rth_elapsed_fraction_midday_near_half():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ct = ZoneInfo("America/Chicago")
    # Wed Jun 10 2026 11:00 AM CT (~38% through RTH)
    mid = datetime(2026, 6, 10, 11, 0, tzinfo=ct)
    frac = rth_elapsed_fraction(mid)
    assert 0.35 < frac < 0.45


def test_squeeze_confidence_active_high_short():
    # 0607e8c recalibration: move ceiling 50pts, RVOL demoted to 18pts
    # (participation noise, not edge), extreme short bonus halved to 8.
    # 7% move (35) + RVOL 3.0 (12) + extreme (8) + active (8) = 63.
    conf = squeeze_confidence(7.0, 3.0, short_risk="extreme", kind="squeeze_active")
    assert conf == 63
    # Move quality dominates: a 10% mover outranks a hotter-RVOL 5% mover.
    strong_move = squeeze_confidence(10.0, 2.0, short_risk="high", kind="squeeze_active")
    hot_rvol = squeeze_confidence(5.0, 6.0, short_risk="high", kind="squeeze_active")
    assert strong_move > hot_rvol


def test_format_squeeze_alert_simple():
    msg = format_squeeze_alert(
        "SPCE",
        "squeeze_active",
        {"price": 4.52, "session_high": 4.92, "peak_move_pct": 7.2},
        3.0,
        {"squeeze_risk": "high"},
    )
    assert "SPCE" in msg
    assert "Buy: $4.52" in msg
    assert "Sell: $" in msg
    assert "Confidence:" in msg


def test_squeeze_trade_levels_uses_session_high():
    buy, sell = squeeze_trade_levels(4.52, 4.92, "squeeze_active")
    assert buy == 4.52
    assert sell == 4.92


def test_candidate_to_pick_matches_telegram_fields():
    from core.squeeze_monitor import candidate_to_pick, format_squeeze_alert

    metrics = {
        "price": 4.52,
        "session_high": 4.92,
        "peak_move_pct": 7.2,
        "current_move_pct": 1.0,
        "prior_close": 4.20,
    }
    pick = candidate_to_pick("SPCE", "squeeze_active", metrics, 3.0, {"squeeze_risk": "high"})
    msg = format_squeeze_alert("SPCE", "squeeze_active", metrics, 3.0, {"squeeze_risk": "high"})
    assert pick["symbol"] == "SPCE"
    assert pick["buy"] == 4.52
    assert pick["sell"] == 4.92
    # 0607e8c recalibration: 7.2% move (36) + RVOL 3.0 (12) + high (10) +
    # active (8) = 66. The old >=70 expectation predated the move-weighted
    # rescore that demoted RVOL.
    assert pick["confidence_pct"] == 66
    assert pick["squeeze_score"] > 0
    assert "p_continue_3pct_60m" in pick["probabilities"]
    assert "Buy: $4.52" in pick["message"]
    assert pick["message"] == msg
    assert pick["message"] == msg
    assert prefilter_candidate(0.5, 0.2, 0.8) is False
    assert prefilter_candidate(3.0, 2.5, 2.0) is True


def test_get_squeeze_picks_exposes_fetch_failed_symbols(monkeypatch):
    import core.squeeze_monitor as sm

    monkeypatch.setattr(
        sm,
        "_last_scan_report",
        {
            "status": "complete",
            "ts": 1,
            "fetch_ok": 40,
            "fetch_fail": 3,
            "fetch_failed_symbols": ["SAP", "IQ", "TME"],
            "symbols": 43,
            "picks": [],
            "candidates": [],
            "leaders": [],
        },
    )
    monkeypatch.setattr(sm, "_alert_history", [])
    board = sm.get_squeeze_picks()
    assert board["fetch_failed_symbols"] == ["SAP", "IQ", "TME"]
    assert board["symbols"] == 43  # mocked passthrough value, not live count


def test_watch_quorum_prioritizes_strongest_anomaly_and_preserves_order(monkeypatch):
    import core.data_quorum as dq
    import core.squeeze_monitor as sm

    monkeypatch.setenv("SQUEEZE_QUORUM_WATCH_BUDGET", "1")
    calls = []
    monkeypatch.setattr(
        dq,
        "evaluate_quorum",
        lambda symbol, use_cache=False: calls.append((symbol, use_cache)) or {
            "verdict": "disagree", "advisory_only": True,
        },
    )
    watches = [
        {
            "symbol": "ARCT", "peak_move_pct": 2.1, "current_move_pct": 1.0,
            "rvol": 1.1, "observations": 1, "escalated": False,
            "confidence_pct": 55, "candidate": False,
        },
        {
            "symbol": "WOLF", "peak_move_pct": 6.0, "current_move_pct": 5.0,
            "rvol": 1.2, "observations": 2, "escalated": False,
            "confidence_pct": 60, "candidate": False,
        },
    ]
    before = [{k: v for k, v in watch.items()} for watch in watches]
    sm._enrich_watches_with_quorum(watches)

    assert calls == [("WOLF", True)]
    assert [watch["symbol"] for watch in watches] == ["ARCT", "WOLF"]
    assert watches[0]["quorum"] == {
        "verdict": "deferred", "advisory_only": True,
        "reason": "per_scan_budget",
    }
    assert watches[1]["quorum"]["verdict"] == "disagree"
    for index, watch in enumerate(watches):
        for field in (
            "symbol", "peak_move_pct", "current_move_pct", "rvol",
            "observations", "escalated", "confidence_pct", "candidate",
        ):
            assert watch[field] == before[index][field]


def test_watch_quorum_prioritizes_escalation_before_raw_strength(monkeypatch):
    import core.data_quorum as dq
    import core.squeeze_monitor as sm

    monkeypatch.setenv("SQUEEZE_QUORUM_WATCH_BUDGET", "1")
    calls = []
    monkeypatch.setattr(
        dq,
        "evaluate_quorum",
        lambda symbol, use_cache=False: calls.append(symbol) or {
            "verdict": "agree", "advisory_only": True,
        },
    )
    watches = [
        {
            "symbol": "STRONG", "peak_move_pct": 8.0, "current_move_pct": 7.0,
            "rvol": 4.0, "observations": 1, "escalated": False,
        },
        {
            "symbol": "REPEAT", "peak_move_pct": 2.1, "current_move_pct": 1.0,
            "rvol": 1.1, "observations": 3, "escalated": True,
        },
    ]

    sm._enrich_watches_with_quorum(watches)

    assert calls == ["REPEAT"]
    assert watches[0]["quorum"]["reason"] == "per_scan_budget"
    assert watches[1]["quorum"]["verdict"] == "agree"


def test_watch_quorum_tie_breaks_by_symbol_ascending(monkeypatch):
    import core.data_quorum as dq
    import core.squeeze_monitor as sm

    monkeypatch.setenv("SQUEEZE_QUORUM_WATCH_BUDGET", "1")
    calls = []
    monkeypatch.setattr(
        dq, "evaluate_quorum",
        lambda symbol, use_cache=False: calls.append(symbol) or {
            "verdict": "agree", "advisory_only": True,
        },
    )
    watches = [
        {"symbol": "ZETA", "peak_move_pct": 4.0, "rvol": 2.0},
        {"symbol": "ALFA", "peak_move_pct": 4.0, "rvol": 2.0},
    ]

    sm._enrich_watches_with_quorum(watches)

    assert calls == ["ALFA"]
    assert [watch["symbol"] for watch in watches] == ["ZETA", "ALFA"]


def test_squeeze_alert_labels_radar_not_trade():
    msg = format_squeeze_alert(
        "SPCE",
        "squeeze_active",
        {"price": 4.52, "session_high": 4.92, "peak_move_pct": 7.2},
        3.0,
        {"squeeze_risk": "high"},
    )
    assert "SQUEEZE RADAR" in msg
    assert "Radar only" in msg
    assert "Ghost gated trade" in msg


def test_squeeze_maybe_alert_suppresses_low_conf(monkeypatch):
    import core.squeeze_monitor as sm
    sent = []
    monkeypatch.setattr(sm, "MIN_TELEGRAM_CONFIDENCE", 80)
    monkeypatch.setattr(sm, "_send_telegram", lambda key, msg: sent.append((key, msg)))
    ok = sm._maybe_alert(
        "SPCE",
        "squeeze_forming",
        {"price": 4.52, "session_high": 4.6, "peak_move_pct": 3.1},
        1.6,
        {"squeeze_risk": "low"},
    )
    assert ok is False
    assert sent == []


def test_candidate_to_pick_rejects_external_advisory_row(monkeypatch):
    import pytest
    import core.squeeze_monitor as sm

    metrics = {
        "price": 20.0, "session_high": 22.0, "peak_move_pct": 10.0,
        "current_move_pct": 8.0, "prior_close": 20.0,
        "advisory_only": True, "decision_eligible": False,
    }
    with pytest.raises(ValueError, match="official squeeze candidate required"):
        sm.candidate_to_pick("ARCT", "squeeze_active", metrics, 3.0, {})


def test_maybe_alert_rejects_external_advisory_row(monkeypatch):
    import core.squeeze_monitor as sm

    sent = []
    monkeypatch.setattr(sm, "_send_telegram", lambda key, msg: sent.append((key, msg)))
    assert sm._maybe_alert(
        "ARCT", "squeeze_active",
        {"price": 20.0, "session_high": 22.0, "peak_move_pct": 10.0,
         "advisory_only": True, "decision_eligible": False},
        3.0, {"squeeze_risk": "extreme"},
    ) is False
    assert sent == []


def test_squeeze_alert_key_changes_only_on_material_reprice():
    import core.squeeze_monitor as sm
    monkeypatch_pct = sm.REPRICE_ALERT_PCT
    try:
        sm.REPRICE_ALERT_PCT = 1.5
        k1 = sm._squeeze_alert_key("BABA", "squeeze_forming", 109.20, 113.57, 86)
        k2 = sm._squeeze_alert_key("BABA", "squeeze_forming", 109.25, 113.62, 86)
        k3 = sm._squeeze_alert_key("BABA", "squeeze_forming", 112.00, 116.48, 86)
        assert k1 == k2
        assert k1 != k3
    finally:
        sm.REPRICE_ALERT_PCT = monkeypatch_pct
