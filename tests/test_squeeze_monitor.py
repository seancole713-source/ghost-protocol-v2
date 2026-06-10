"""Watchlist squeeze radar — RVOL + signal bands."""

from core.squeeze_monitor import (
    compute_rvol,
    evaluate_squeeze_signal,
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

    et = ZoneInfo("America/New_York")
    # Wed Jun 10 2026 12:00 ET
    noon = datetime(2026, 6, 10, 12, 0, tzinfo=et)
    frac = rth_elapsed_fraction(noon)
    assert 0.35 < frac < 0.45


def test_squeeze_confidence_active_high_short():
    conf = squeeze_confidence(7.0, 3.0, short_risk="extreme", kind="squeeze_active")
    assert conf >= 70


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

    metrics = {"price": 4.52, "session_high": 4.92, "peak_move_pct": 7.2, "current_move_pct": 1.0}
    pick = candidate_to_pick("SPCE", "squeeze_active", metrics, 3.0, {"squeeze_risk": "high"})
    msg = format_squeeze_alert("SPCE", "squeeze_active", metrics, 3.0, {"squeeze_risk": "high"})
    assert pick["symbol"] == "SPCE"
    assert pick["buy"] == 4.52
    assert pick["sell"] == 4.92
    assert pick["confidence_pct"] >= 70
    assert "Buy: $4.52" in pick["message"]
    assert pick["message"] == msg
    assert prefilter_candidate(0.5, 0.2, 0.8) is False
    assert prefilter_candidate(3.0, 2.5, 2.0) is True
