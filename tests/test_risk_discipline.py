"""Tests for operator risk discipline (sizing, daily lock, action labels)."""

import pytest


def test_position_sizing_1pct_rule():
    from core.risk_discipline import position_sizing_plan

    plan = position_sizing_plan(100.0, 98.0, account_size=25000, risk_pct=1.0)
    assert plan["ok"] is True
    assert plan["max_loss_usd"] == 250.0
    assert plan["stop_distance_pct"] == pytest.approx(2.0, abs=0.01)
    assert plan["suggested_shares"] >= 1
    assert plan["estimated_loss_at_stop_usd"] <= plan["max_loss_usd"] * 1.05


def test_pick_action_tiers():
    from core.risk_discipline import pick_action_tier

    assert pick_action_tier(0.95, 85) == "SUPER BUY"
    assert pick_action_tier(0.80, 70) == "BUY NOW"
    assert pick_action_tier(0.70) == "BUY"


def test_bias_label_not_buy_language():
    from core.risk_discipline import bias_label_from_score

    assert "bias" in bias_label_from_score(65)
    assert "bullish" in bias_label_from_score(65)


def test_trade_action_silence_when_gates_blocked():
    from core.risk_discipline import trade_action_from_context

    out = trade_action_from_context(
        has_official_pick=False,
        ghost_score=65,
        gates_blocked=True,
        engine_paused=False,
        daily_locked=False,
    )
    assert out["trade_action"] == "NO TRADE"
    assert "bias" in out["trade_note"].lower() or "gates" in out["trade_note"].lower()


def test_format_silence_card_shows_no_trade():
    from core.telegram_cards import format_silence_card

    out = format_silence_card({
        "ghost_score": 65,
        "bias_label": "mild bullish bias",
        "trade_action": "NO TRADE",
        "trade_note": "Ghost Score is mild bullish bias only — no setup cleared the gates.",
        "reason": "objective gate (symbol WR below target) (floor 75%)",
    })
    assert "NO TRADE" in out
    assert "mild bullish bias" in out
    assert "BUY NOW" not in out


def test_format_daily_card_includes_sizing():
    from core.telegram_cards import format_daily_card

    out = format_daily_card({
        "date": "Mon",
        "direction": "UP",
        "confidence": 0.95,
        "pick_action": "SUPER BUY",
        "current_price": 65.0,
        "buy_point": 65.0,
        "sell_target": 66.6,
        "stop_loss": 64.0,
        "expected_move_pct": 2.5,
        "position_sizing": {
            "ok": True,
            "account_size_usd": 25000,
            "max_loss_usd": 250,
            "suggested_shares": 192,
            "suggested_notional_usd": 12480,
            "stop_distance_pct": 1.54,
        },
        "news": {"influence_pct": 0, "model_pct": 100},
        "rates": {"today_pct": 95},
        "track_record": {"wins": 2, "losses": 6, "win_rate_pct": 25, "last5": [], "streak": "3L"},
    })
    assert "SUPER BUY" in out
    assert "1% RISK SIZING" in out
    assert "250" in out


def test_daily_loss_lock_triggers_on_loss_count(monkeypatch):
    from core import risk_discipline as rd

    monkeypatch.setenv("GHOST_DAILY_MAX_LOSSES", "2")
    monkeypatch.setenv("GHOST_DAILY_LOSS_LIMIT_USD", "99999")
    monkeypatch.setattr(rd, "_today_resolved_stats", lambda: {
        "losses": 2,
        "realized_pnl_usd": -50,
        "wins": 0,
        "trades": 2,
    })
    st = rd.daily_loss_lock_state()
    assert st["should_lock"] is True
