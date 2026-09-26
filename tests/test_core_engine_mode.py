"""CORE_ENGINE_MODE (operator decision 2026-09-26): core v3 is research-only.

Default is ``research``: operator-facing core copy opens with the research
header, carries no hype tier (SUPER BUY / STRONG BUY / BUY NOW), no
confidence-tiered or dollar sizing, and shows the raw model probability
labelled "uncalibrated". ``live`` restores the old copy only when set
explicitly.
"""
import re
import time

import pytest

from core.engine_mode import RESEARCH_HEADER, core_engine_mode, core_is_research

# Words and phrases that must never reach the operator for a research-mode core pick.
_HYPE_OR_SIZING = (
    "SUPER BUY", "STRONG BUY", "BUY NOW", "BUY SIGNAL", "ACTION:",
    "SIZING", "Suggested", "shares", "Account", "Max loss", "% of your capital",
    "Size (1% risk)", "$100 in", "$100 to", "deployed", "Confidence", "confident",
    "Conviction", "Prediction Rate",
)


def _assert_research_copy(text: str) -> None:
    assert RESEARCH_HEADER in text
    for word in _HYPE_OR_SIZING:
        assert word not in text, f"{word!r} leaked into research copy:\n{text}"


def _daily_payload(**over):
    d = {
        "date": "Friday Sep 25, 2026",
        "model_version": "v3.2",
        "symbol": "WOLF",
        "direction": "UP",
        "confidence": 0.95,
        "pick_action": "SUPER BUY",
        "conviction": "HIGH",
        "current_price": 65.0,
        "buy_point": 65.0,
        "sell_target": 68.12,
        "stop_loss": 63.7,
        "expected_move_pct": 4.8,
        "position_sizing": {
            "ok": True, "account_size_usd": 25000, "max_loss_usd": 250,
            "suggested_shares": 192, "suggested_notional_usd": 12480, "stop_distance_pct": 2.0,
        },
        "news": {"influence_pct": 0, "model_pct": 100},
        "rates": {"today_pct": 95, "week_high_pct": 95, "week_low_pct": 88},
        "track_record": {"wins": 3, "losses": 6, "win_rate_pct": 33.3,
                         "last5": ["L", "L", "W", "L", "L"], "streak": "2L"},
    }
    d.update(over)
    return d


# ── the flag ────────────────────────────────────────────────────────────

def test_flag_defaults_to_research(monkeypatch):
    monkeypatch.delenv("CORE_ENGINE_MODE", raising=False)
    assert core_engine_mode() == "research"
    assert core_is_research() is True


@pytest.mark.parametrize("raw", ["", "research", "RESEARCH", "paper", "on", "1", "livee"])
def test_anything_but_explicit_live_is_research(monkeypatch, raw):
    monkeypatch.setenv("CORE_ENGINE_MODE", raw)
    assert core_engine_mode() == "research"


@pytest.mark.parametrize("raw", ["live", "LIVE", " Live "])
def test_explicit_live(monkeypatch, raw):
    monkeypatch.setenv("CORE_ENGINE_MODE", raw)
    assert core_engine_mode() == "live"
    assert core_is_research() is False


# ── Telegram cards ──────────────────────────────────────────────────────

def test_research_daily_card_has_header_and_no_hype_or_sizing(monkeypatch):
    monkeypatch.delenv("CORE_ENGINE_MODE", raising=False)
    from core.telegram_cards import format_daily_card

    out = format_daily_card(_daily_payload())
    _assert_research_copy(out)
    assert out.splitlines()[0] == "<b>" + RESEARCH_HEADER + "</b>"
    assert "Raw model probability: 0.95 (uncalibrated" in out
    assert "95%" not in out  # no percent dressed as confidence
    # No dollar size or %-of-account anywhere.
    assert "12,480" not in out and "$250" not in out and "250.00" not in out
    assert "WOLF" in out and "Model target: $68.12" in out


def test_research_daily_card_ignores_even_a_sized_payload(monkeypatch):
    """Even if a caller passes sizing, research mode never prints it."""
    monkeypatch.setenv("CORE_ENGINE_MODE", "research")
    from core.telegram_cards import format_daily_card

    out = format_daily_card(_daily_payload(news={"influence_pct": 30, "model_pct": 70,
                                                 "summary": "WOLF beats on revenue"}))
    _assert_research_copy(out)
    assert "WOLF beats on revenue" in out


def test_live_daily_card_unchanged(monkeypatch):
    monkeypatch.setenv("CORE_ENGINE_MODE", "live")
    from core.telegram_cards import format_daily_card

    out = format_daily_card(_daily_payload())
    assert RESEARCH_HEADER not in out
    assert out.startswith("<b>GHOST PROTOCOL | WOLF Daily Card</b>")
    assert "<b>ACTION:</b> SUPER BUY" in out
    assert "Confidence: 95%" in out
    assert "1% RISK SIZING" in out
    assert "Suggested: 192 shares (~$12,480)" in out
    # explicit mode argument agrees with the env
    assert format_daily_card(_daily_payload(), mode="live") == out
    assert RESEARCH_HEADER in format_daily_card(_daily_payload(), mode="research")


def test_research_silence_card_raw_probs(monkeypatch):
    monkeypatch.delenv("CORE_ENGINE_MODE", raising=False)
    from core.telegram_cards import format_silence_card

    out = format_silence_card({
        "ghost_score": 46, "bias_label": "neutral bias", "reason": "prob below floor",
        "trade_action": "NO TRADE", "trade_note": "x",
        "top_candidates": [
            {"symbol": "WOLF", "up_prob": 0.724, "min_win_proba": 0.80, "skip_code": "v3_prob_low"},
            {"symbol": "AMC", "up_prob": 0.61, "skip_code": "v3_regime_gate"},
        ],
        "next_scan_note": "soon",
    })
    _assert_research_copy(out)
    assert "1. WOLF p=0.72 (floor 0.80)" in out
    assert "uncalibrated" in out
    cand = [ln for ln in out.splitlines() if re.match(r"^\d\. ", ln)]
    assert cand and all("%" not in ln for ln in cand)


def test_research_weekly_card_has_no_dollars(monkeypatch):
    monkeypatch.delenv("CORE_ENGINE_MODE", raising=False)
    from core.telegram_cards import format_weekly_summary

    out = format_weekly_summary({
        "week_range": "Sep 20 - Sep 26",
        "followed": {"wins": 1, "losses": 3, "win_rate_pct": 25.0, "pnl_usd": -47.25},
        "alltime": {"win_rate_pct": 33.3, "wins": 3, "losses": 6},
        "retrain_in_days": 4,
        "top_pick": {"day": "Monday", "confidence_pct": 95},
        "weakest_pick": {"day": "Friday", "confidence_pct": 76},
        "news_driven": {"count": 1, "total": 4},
    })
    _assert_research_copy(out)
    assert "$" not in out
    assert "Monday @ 0.95" in out


# ── Telegram senders (capture instead of sending) ───────────────────────

@pytest.fixture
def sent(monkeypatch):
    import core.telegram as tg
    box = []
    monkeypatch.setattr(tg, "_send", lambda text: (box.append(text), True)[1])
    return box


def test_research_position_alert(monkeypatch, sent):
    monkeypatch.delenv("CORE_ENGINE_MODE", raising=False)
    from core.telegram import send_position_alert

    send_position_alert("WOLF", "UP", "WIN", 65.0, 68.12, 4.8, 104.8)
    _assert_research_copy(sent[-1])
    assert "TARGET HIT" not in sent[-1] and "104.8" not in sent[-1]


def test_live_position_alert_unchanged(monkeypatch, sent):
    monkeypatch.setenv("CORE_ENGINE_MODE", "live")
    from core.telegram import send_position_alert

    send_position_alert("WOLF", "UP", "WIN", 65.0, 68.12, 4.8, 104.8)
    assert sent[-1].startswith("<b>WOLF TARGET HIT -- WIN</b>")
    assert "$100 to $104.8" in sent[-1]


def test_research_legacy_morning_card(monkeypatch, sent):
    monkeypatch.delenv("CORE_ENGINE_MODE", raising=False)
    from core.telegram import send_morning_card

    send_morning_card([{"symbol": "WOLF", "direction": "UP", "confidence": 0.95,
                        "entry_price": 65.0, "target_price": 68.12, "stop_price": 63.7,
                        "pos_size_pct": 5.0, "expires_at": time.time() + 86400}],
                      week_stats={"wins": 1, "losses": 2, "pnl_usd": -12.5, "alltime_wr": 33})
    _assert_research_copy(sent[-1])
    assert "raw model prob 0.95 (uncalibrated)" in sent[-1]
    assert "5.0%" not in sent[-1] and "-12.5" not in sent[-1]


def test_research_withdrawn_and_weekly_legacy(monkeypatch, sent):
    monkeypatch.delenv("CORE_ENGINE_MODE", raising=False)
    from core.telegram import send_pick_withdrawn, send_weekly_summary

    send_pick_withdrawn("WOLF", "model_withdrawn", 65.0, 64.0, -1.5)
    _assert_research_copy(sent[-1])
    send_weekly_summary({"wins": 2, "losses": 1, "avg_win": 3.0, "avg_loss": -2.0})
    _assert_research_copy(sent[-1])
    assert "$" not in sent[-1]


# ── trade alerts / action tier / sizing ─────────────────────────────────

def test_research_mode_sends_no_trade_alert(monkeypatch, sent):
    monkeypatch.delenv("CORE_ENGINE_MODE", raising=False)
    monkeypatch.setenv("CRON_SECRET", "")
    import wolf_app

    def _no_db():
        raise AssertionError("research mode must not read candidates for a trade alert")

    monkeypatch.setattr(wolf_app, "db_conn", _no_db)
    out = wolf_app.wolf_signal_alert_check(x_cron_secret="")
    assert out["ok"] is True and out["sent"] == []
    assert out["core_engine_mode"] == "research"
    assert "research-only" in out["skipped_reason"]
    assert sent == []


def test_research_mode_still_requires_cron_secret(monkeypatch):
    monkeypatch.delenv("CORE_ENGINE_MODE", raising=False)
    monkeypatch.setenv("CRON_SECRET", "supersecret")
    import wolf_app
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        wolf_app.wolf_signal_alert_check(x_cron_secret="wrong")


def test_trade_action_for_official_pick(monkeypatch):
    from core.risk_discipline import trade_action_from_context

    kw = dict(has_official_pick=True, pick_confidence=0.95, ghost_score=85,
              gates_blocked=False, engine_paused=False, daily_locked=False)
    monkeypatch.delenv("CORE_ENGINE_MODE", raising=False)
    research = trade_action_from_context(**kw)
    assert research["trade_action"] == "RESEARCH ONLY"
    assert "BUY" not in research["trade_action"] and "size to" not in research["trade_note"]
    monkeypatch.setenv("CORE_ENGINE_MODE", "live")
    assert trade_action_from_context(**kw)["trade_action"] == "SUPER BUY"


def _stub_card_context(monkeypatch):
    import api.wolf_endpoints as we
    import wolf_app

    monkeypatch.setattr(wolf_app, "_wolf_week_rate_bounds", lambda: (95, 88))
    monkeypatch.setattr(wolf_app, "_wolf_track_record",
                        lambda: {"wins": 3, "losses": 6, "win_rate_pct": 33.3, "last5": [], "streak": "--"})
    monkeypatch.setattr(we, "ghost_score_payload_sync", lambda use_cache=True: {"ok": True, "score": 85})
    return wolf_app


_PICK = {"symbol": "WOLF", "direction": "UP", "confidence": 0.95,
         "entry_price": 65.0, "target_price": 68.12, "stop_price": 63.7, "features": {}}


def test_research_card_data_is_never_sized(monkeypatch):
    monkeypatch.delenv("CORE_ENGINE_MODE", raising=False)
    wolf_app = _stub_card_context(monkeypatch)
    import core.risk_discipline as rd

    def _boom(*a, **k):
        raise AssertionError("research mode must not size a core pick")

    monkeypatch.setattr(rd, "position_sizing_plan", _boom)
    monkeypatch.setattr(rd, "pick_action_tier", _boom)
    data = wolf_app._build_daily_card_data(dict(_PICK))
    assert data["position_sizing"] is None and data["pick_action"] is None
    assert data["core_engine_mode"] == "research" and data["symbol"] == "WOLF"
    from core.telegram_cards import format_daily_card
    _assert_research_copy(format_daily_card(data))


def test_live_card_data_still_sized(monkeypatch):
    monkeypatch.setenv("CORE_ENGINE_MODE", "live")
    wolf_app = _stub_card_context(monkeypatch)
    data = wolf_app._build_daily_card_data(dict(_PICK))
    assert data["pick_action"] == "SUPER BUY"
    assert data["position_sizing"]["ok"] is True


def test_research_silence_data_floor_is_raw_prob(monkeypatch):
    monkeypatch.delenv("CORE_ENGINE_MODE", raising=False)
    import wolf_app

    monkeypatch.setattr("api.wolf_endpoints.ghost_score_payload_sync",
                        lambda **kw: {"ok": True, "score": 65.0})
    out = wolf_app._build_silence_card_data({"top_reason_label": "v3 model prob below floor",
                                             "confidence_floor": 0.75})
    assert "floor 0.75, raw prob" in out["reason"] and "75%" not in out["reason"]


def test_norm_pred_hides_pos_size_in_research(monkeypatch):
    import wolf_app

    row = {"id": 1, "symbol": "WOLF", "direction": "UP", "confidence": 0.95,
           "entry_price": 65.0, "target_price": 68.12, "stop_price": 63.7}
    monkeypatch.delenv("CORE_ENGINE_MODE", raising=False)
    assert wolf_app._norm_pred(row)["pos_size_pct"] is None
    monkeypatch.setenv("CORE_ENGINE_MODE", "live")
    assert wolf_app._norm_pred(row)["pos_size_pct"] == 5.0
