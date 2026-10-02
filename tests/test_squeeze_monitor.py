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



def _et_today():
    """The exchange (Eastern) date -- the basis squeeze evidence dates sessions on."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).date()

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
    assert "Watch level: $4.52" in msg
    assert "Upside reference: $4.70" in msg
    # No vwap/prior close: invalidation is the 2.5% buffer under the alert price.
    assert "Invalidation level: $4.41" in msg
    assert "Setup score (unvalidated): " in msg
    assert "/95 — not a probability" in msg


def test_radar_alert_never_claims_confidence_or_trade_instructions():
    """P1 audit: the radar score is an unvalidated proxy. The message must not
    call it confidence/probability nor issue Buy/Sell instructions."""
    msg = format_squeeze_alert(
        "SPCE",
        "squeeze_forming",
        {"price": 10.60, "session_high": 11.00, "peak_move_pct": 6.0,
         "prior_close": 10.0, "vwap": 10.40},
        3.0,
        {"squeeze_risk": "high"},
    )
    lowered = msg.lower()
    for banned in ("confidence", "probability:", "win prob", "expected value", "buy:", "sell:"):
        assert banned not in lowered, banned
    assert "Radar only — not a Ghost gated trade" in msg
    # Invalidation uses the same compute_stop anchors as the eligibility check.
    from core.squeeze_scorecard import compute_stop
    assert f"Invalidation level: ${compute_stop(10.60, vwap=10.40, prior_close=10.0):.2f}" in msg


def test_squeeze_trade_levels_anchor_on_alert_time_price_not_prior_high():
    """F38: the target is TP above the alert-time price, never the session
    high that already printed before the alert."""
    buy, sell = squeeze_trade_levels(4.52, 4.92, "squeeze_active")
    assert buy == 4.52
    assert sell == round(4.52 * 1.04, 2) == 4.70
    # Audit repro: close 10, high 11 printed, alert at 10.60.
    buy, sell = squeeze_trade_levels(10.60, 11.00, "squeeze_forming")
    assert (buy, sell) == (10.60, round(10.60 * 1.025, 2))
    assert sell < 11.00
    # Same target whatever the prior high was.
    assert squeeze_trade_levels(10.60, 15.0, "squeeze_active") == squeeze_trade_levels(
        10.60, 10.60, "squeeze_active"
    )


def test_alert_time_fade_pct():
    from core.squeeze_monitor import alert_time_fade_pct

    assert alert_time_fade_pct(10.60, 11.00) == 3.64
    assert alert_time_fade_pct(11.00, 11.00) == 0.0
    assert alert_time_fade_pct(0, 11.0) is None
    assert alert_time_fade_pct(None, 11.0) is None


def _legacy_ev_gate(buy, sell, stop, score):
    """The pre-P1 gate, reproduced verbatim to prove the cutoff is unchanged."""
    if buy <= 0 or sell <= buy or stop >= buy:
        return False
    win_prob = score / 100.0
    gain_pct = (sell - buy) / buy
    loss_pct = (buy - stop) / buy
    return win_prob * gain_pct - (1.0 - win_prob) * loss_pct > 0.0


def test_reward_risk_floor_no_longer_uses_prior_high_as_gain():
    """F38 repro: close 10, high 11, price 10.6, score 76. The old sell=max(TP,
    11.00) cleared the floor on an upside the alert could not capture; with
    the alert-time reference the reward is the configured TP."""
    from core.squeeze_monitor import _score_clears_reward_risk_floor

    stop = 10.60 * 0.975
    old_sell = 11.00
    assert _score_clears_reward_risk_floor(10.60, old_sell, stop, 76) is True
    buy, sell = squeeze_trade_levels(10.60, 11.00, "squeeze_forming")
    gain = (sell - buy) / buy
    assert abs(gain - 0.025) < 0.001
    assert _score_clears_reward_risk_floor(buy, sell, stop, 76) is _legacy_ev_gate(buy, sell, stop, 76)


def test_required_setup_score_is_reward_risk_share():
    from core.squeeze_monitor import required_setup_score

    # reward 1, risk 1 -> the score must exceed 50.
    assert required_setup_score(10.0, 11.0, 9.0) == 50.0
    # reward 0.25, risk 0.25 (2.5% TP, 2.5% buffer) -> 50 as well.
    assert abs(required_setup_score(10.0, 10.25, 9.75) - 50.0) < 1e-9
    # Wider invalidation -> stricter floor.
    assert required_setup_score(10.0, 10.25, 9.50) > required_setup_score(10.0, 10.25, 9.75)
    # Degenerate levels are never eligible.
    assert required_setup_score(10.0, 10.0, 9.0) is None
    assert required_setup_score(10.0, 11.0, 10.0) is None
    assert required_setup_score(0.0, 1.0, -1.0) is None


def test_score_floor_matches_legacy_ev_cutoff_exactly():
    """P1 audit: EV/win_prob were removed, but eligibility must not loosen.
    Exhaustive grid: the new floor never admits an alert the old EV gate
    rejected. The only allowed disagreement is an exact tie (EV exactly 0 in
    exact arithmetic), where the old gate's admission was a float-rounding
    artifact -- the new floor rejects ties, i.e. it is never more permissive."""
    import itertools
    from fractions import Fraction as F

    from core.squeeze_monitor import _score_clears_reward_risk_floor

    def exact_tie(buy, sell, stop, score):
        b, s_, st = F(str(buy)), F(str(sell)), F(str(stop))
        return F(score) * (s_ - st) == 100 * (b - st)

    prices = [0.37, 1.0, 2.13, 4.52, 10.6, 37.25, 182.4]
    tps = [0.0, 0.01, 0.025, 0.04, 0.08]
    buffers = [0.0, 0.005, 0.025, 0.04, 0.1, 0.3]
    for price, tp, buf, score in itertools.product(prices, tps, buffers, range(0, 96)):
        buy = round(price, 2)
        sell = round(buy * (1 + tp), 2)
        stop = round(buy * (1 - buf), 2)
        new_ok = _score_clears_reward_risk_floor(buy, sell, stop, score)
        old_ok = _legacy_ev_gate(buy, sell, stop, score)
        assert not (new_ok and not old_ok), ("loosened", buy, sell, stop, score)
        if new_ok != old_ok:
            assert exact_tie(buy, sell, stop, score), (buy, sell, stop, score)


def test_alert_gate_has_no_win_prob_or_expected_value():
    import inspect

    import core.squeeze_monitor as sm

    src = inspect.getsource(sm._maybe_alert) + inspect.getsource(sm._score_clears_reward_risk_floor)
    for banned in ("win_prob", "loss_prob", "expected_value", "negative-EV", " ev "):
        assert banned not in src, banned
    assert not hasattr(sm, "_check_expected_value")


def test_maybe_alert_suppresses_below_score_floor(monkeypatch):
    """End-to-end through _maybe_alert: a setup whose invalidation is far
    below the watch level needs a higher score and is suppressed."""
    import time as _time

    import core.squeeze_monitor as sm
    from core.daily_bar_contract import previous_session

    sent = []
    monkeypatch.setattr(sm, "_send_telegram", lambda key, msg: sent.append(msg) or True)
    monkeypatch.setattr(sm, "_symbol_loss_streak", lambda symbol: 0)
    monkeypatch.setattr(sm, "MIN_TELEGRAM_CONFIDENCE", 0)
    sm._last_alert.clear()
    base = {
        "price": 10.00, "session_high": 10.00, "peak_move_pct": 8.0, "current_move_pct": 8.0,
        "price_as_of_ts": _time.time() - 60, "daily_feed": "iex", "intraday_feed": "iex",
        "reference_session_date": previous_session(_et_today()).isoformat(),
        "bars_complete": True, "session_volume": 1000, "avg_daily_volume": 1000,
    }
    symbol = sorted(__import__("config.symbols", fromlist=["V3_WHITELIST_STOCKS"]).V3_WHITELIST_STOCKS)[0]
    # VWAP/prior close far below -> invalidation 9.0*0.995; reward 0.40 (4%).
    far = dict(base, vwap=9.0, prior_close=9.0)
    score = squeeze_confidence(8.0, 1.0, short_risk=None, kind="squeeze_active")
    floor = sm.required_setup_score(10.0, 10.40, round(9.0 * 0.995, 2))
    assert score <= floor
    assert sm._maybe_alert(symbol, "squeeze_active", far, 1.0, {}) is False
    assert sent == []
    # Tight invalidation (2.5% buffer) -> floor ~38.5, score clears it.
    near = dict(base, vwap=None, prior_close=9.80)
    assert sm._maybe_alert(symbol, "squeeze_active", near, 1.0, {}) is True
    assert sent and "Setup score (unvalidated)" in sent[0]
    sm._last_alert.clear()


def test_candidate_to_pick_matches_telegram_fields():
    from core.squeeze_monitor import candidate_to_pick, format_squeeze_alert

    metrics = {
        "price": 4.52,
        "session_high": 4.92,
        "peak_move_pct": 7.2,
        "current_move_pct": 1.0,
        "prior_close": 4.20,
    }
    import time
    from core.daily_bar_contract import previous_session
    from core.market_hours import session_hm
    metrics.update({"price_as_of_ts": time.time() - 60, "daily_feed": "iex", "intraday_feed": "iex",
                    "reference_session_date": previous_session(_et_today()).isoformat(),
                    "bars_complete": True, "session_volume": 1000, "avg_daily_volume": 1000})
    pick = candidate_to_pick("SPCE", "squeeze_active", metrics, 3.0, {"squeeze_risk": "high"})
    msg = format_squeeze_alert("SPCE", "squeeze_active", metrics, 3.0, {"squeeze_risk": "high"})
    assert pick["symbol"] == "SPCE"
    assert pick["buy"] == 4.52
    assert pick["sell"] == 4.70  # F38: 4% TP above the alert price, not the 4.92 high
    assert pick["alert_time_fade_pct"] == round((4.92 - 4.52) / 4.92 * 100, 2)
    assert "below today's high $4.92" in pick["message"]
    # 0607e8c recalibration: 7.2% move (36) + RVOL 3.0 (12) + high (10) +
    # active (8) = 66. The old >=70 expectation predated the move-weighted
    # rescore that demoted RVOL.
    assert pick["confidence_pct"] == 66
    assert pick["squeeze_score"] > 0
    assert "p_continue_3pct_60m" in pick["probabilities"]
    assert "Watch level: $4.52" in pick["message"]
    assert pick["confidence_pct_label"] == "Setup score (unvalidated)"
    assert pick["confidence_pct_status"] == "unvalidated_heuristic_score"
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


# ── fetch-outcome classification (premarket no-print vs real failure) ────────
#
# Regression fixture for a live-observed telemetry bug: a premarket scan
# logged "ok=21 fail=86" on a 107-symbol watchlist, and because
# ghost_console.html flips its badge to "Market data degraded" on
# fetch_fail > 0, the console reported degraded market data on every
# premarket cycle. The 86 were symbols batched_market_metrics itself
# documents as "an authoritative premarket absence, not a provider miss".

def test_no_intraday_print_is_not_counted_as_a_fetch_failure(monkeypatch):
    """A symbol the batch answered for with None has not traded yet -- it is
    not a provider failure and must not set fetch_fail."""
    import asyncio

    import core.squeeze_monitor as sm

    monkeypatch.setattr("core.market_hours.is_us_extended_hours", lambda *a, **k: True)
    monkeypatch.setattr("core.market_hours.is_us_premarket", lambda *a, **k: True)
    monkeypatch.setattr("core.market_hours.is_us_rth", lambda *a, **k: False)
    monkeypatch.setattr("config.symbols.get_edge_set", lambda: {"AAA", "BBB", "CCC"})
    # Explicit complete-empty status is required; None alone proves nothing.
    import time
    from core.daily_bar_contract import previous_session
    from core.market_hours import session_hm
    from core.squeeze_evidence import MarketSnapshot
    monkeypatch.setattr(sm, "batched_market_snapshot", lambda syms: MarketSnapshot(metrics={
        "AAA": {"session_volume": 1000.0, "avg_daily_volume": 500.0,
                "peak_move_pct": 1.0, "current_move_pct": 1.0, "price": 10.0,
                "prior_close": 9.9, "session_high": 10.0, "price_as_of_ts": time.time() - 60,
                "reference_session_date": previous_session(_et_today()).isoformat(),
                "daily_feed": "iex", "intraday_feed": "iex", "bars_complete": True},
        "BBB": None, "CCC": None,
    }, statuses={sym: {"status": "ready" if sym == "AAA" else "no_intraday_print"} for sym in syms}))

    def _boom(sym):  # the fallback path must never be reached for batch answers
        raise AssertionError(f"per-symbol fallback ran for batched symbol {sym}")

    monkeypatch.setattr(sm, "_single_market_snapshot", _boom)
    monkeypatch.setattr(sm, "_persist_scan_report", lambda r: None)
    monkeypatch.setattr(sm, "_maybe_alert", lambda *a, **k: False)
    monkeypatch.setattr(sm, "_enrich_watches_with_quorum", lambda w: None)
    monkeypatch.setattr(sm, "_reset_alert_history_if_new_session", lambda: None)

    asyncio.run(sm._run_watchlist_scan())
    report = sm._last_scan_report

    assert report["fetch_ok"] == 1
    assert report["fetch_fail"] == 0, "premarket absence must not read as failure"
    assert report["fetch_failed_symbols"] == []
    assert report["no_intraday_print"] == 2
    assert sorted(report["no_intraday_print_symbols"]) == ["BBB", "CCC"]


def test_symbols_never_attempted_are_skipped_not_failed(monkeypatch):
    """When the fallback loop stops early on a timeout, the symbols it never
    reached are 'skipped', not 'failed' -- never attempted != tried and failed."""
    import asyncio

    import core.squeeze_monitor as sm

    monkeypatch.setattr("core.market_hours.is_us_extended_hours", lambda *a, **k: True)
    monkeypatch.setattr("core.market_hours.is_us_premarket", lambda *a, **k: True)
    monkeypatch.setattr("core.market_hours.is_us_rth", lambda *a, **k: False)
    monkeypatch.setattr("config.symbols.get_edge_set", lambda: {"AAA", "BBB", "CCC"})
    from core.squeeze_evidence import MarketSnapshot
    monkeypatch.setattr(sm, "batched_market_snapshot", lambda syms: MarketSnapshot())  # batch disabled

    def _hang(sym):
        import time as _t
        _t.sleep(0.15)  # exceeds the patched timeout below
        return None

    monkeypatch.setattr(sm, "_single_market_snapshot", _hang)
    monkeypatch.setenv("SQUEEZE_FETCH_TIMEOUT_S", "0.05")
    monkeypatch.setenv("SQUEEZE_FETCH_DELAY_S", "0")
    monkeypatch.setattr(sm, "_persist_scan_report", lambda r: None)
    monkeypatch.setattr(sm, "_maybe_alert", lambda *a, **k: False)
    monkeypatch.setattr(sm, "_enrich_watches_with_quorum", lambda w: None)
    monkeypatch.setattr(sm, "_reset_alert_history_if_new_session", lambda: None)

    asyncio.run(sm._run_watchlist_scan())
    report = sm._last_scan_report

    # Exactly one symbol was attempted (then timed out, breaking the loop);
    # the other two were never tried.
    assert report["fetch_fail"] == 1
    assert report["fetch_skipped"] == 2
    assert len(report["fetch_failed_symbols"]) == 1
    assert len(report["fetch_skipped_symbols"]) == 2
    assert report["fetch_ok"] == 0
    sm._batch_worker_future.result(timeout=1)


def test_get_squeeze_picks_exposes_the_new_fetch_outcome_counts(monkeypatch):
    import core.squeeze_monitor as sm

    monkeypatch.setattr(sm, "_last_scan_report", {
        "status": "complete", "ts": 1, "ok": True,
        "fetch_ok": 21, "fetch_fail": 0, "fetch_failed_symbols": [],
        "no_intraday_print": 86, "no_intraday_print_symbols": ["ZZZ"],
        "fetch_skipped": 0, "fetch_skipped_symbols": [],
        "symbols": 107, "picks": [], "candidates": [], "leaders": [],
    })
    monkeypatch.setattr(sm, "_alert_history", [])

    board = sm.get_squeeze_picks()

    # The console's degraded badge keys off fetch_fail; a normal premarket
    # watchlist must leave it at zero.
    assert board["fetch_fail"] == 0
    assert board["no_intraday_print"] == 86
    assert board["no_intraday_print_symbols"] == ["ZZZ"]
    assert board["fetch_skipped"] == 0
    # P2 audit: no-print symbols are not usable evidence, so 21/107 is
    # degraded coverage even though fetch_fail is zero.
    assert board["usable_symbols"] == 21
    assert board["scanned_symbols"] == 107
    assert board["usable_coverage_pct"] == 19.6
    assert board["coverage_degraded"] is True


def test_scan_coverage_reports_usable_of_scanned(monkeypatch):
    from core.squeeze_monitor import scan_coverage

    monkeypatch.delenv("SQUEEZE_MIN_USABLE_COVERAGE", raising=False)
    audit = scan_coverage({"symbols": 105, "fetch_ok": 6})
    assert audit == {
        "scanned_symbols": 105, "usable_symbols": 6, "usable_coverage_pct": 5.7,
        "coverage_degraded": True, "min_usable_coverage_pct": 80.0,
    }
    full = scan_coverage({"symbols": 105, "fetch_ok": 100})
    assert full["coverage_degraded"] is False and full["usable_coverage_pct"] == 95.2
    assert scan_coverage({"symbols": 100, "fetch_ok": 80})["coverage_degraded"] is False
    assert scan_coverage({"symbols": 100, "fetch_ok": 79})["coverage_degraded"] is True
    # Missing / zero counts are never treated as full coverage.
    for report in ({}, {"symbols": 0, "fetch_ok": 0}, {"symbols": 10}, {"fetch_ok": 3},
                   {"symbols": "x", "fetch_ok": 1}):
        assert scan_coverage(report)["coverage_degraded"] is True, report
    # Usable can never exceed scanned.
    assert scan_coverage({"symbols": 5, "fetch_ok": 9})["usable_symbols"] == 5
    monkeypatch.setenv("SQUEEZE_MIN_USABLE_COVERAGE", "0.05")
    assert scan_coverage({"symbols": 105, "fetch_ok": 6})["coverage_degraded"] is False
    monkeypatch.setenv("SQUEEZE_MIN_USABLE_COVERAGE", "bogus")
    assert scan_coverage({"symbols": 105, "fetch_ok": 6})["min_usable_coverage_pct"] == 80.0


def test_coverage_is_display_only_and_does_not_change_scan_ok(monkeypatch):
    import core.squeeze_monitor as sm

    monkeypatch.setattr(sm, "_last_scan_report", {
        "status": "complete", "ts": 1, "ok": True, "fetch_ok": 6, "symbols": 105,
        "picks": [], "candidates": [], "leaders": [],
    })
    monkeypatch.setattr(sm, "_alert_history", [])
    board = sm.get_squeeze_picks()
    assert board["coverage_degraded"] is True
    # scan_ok keeps its own definition (stale here: ts=1); coverage never flips it.
    assert board["scan_ok"] is False
    assert board["scorecard"]["confidence_pct_note"].startswith("Legacy field name")
