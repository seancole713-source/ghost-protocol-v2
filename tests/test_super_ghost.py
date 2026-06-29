from fastapi.testclient import TestClient

import wolf_app
from core.super_ghost import (
    CHECKLIST,
    build_super_ghost,
    checklist_manifest,
    detect_market_regime,
    generate_ai_brief,
)


def _trend_rows(start=10.0, step=0.05, n=260, volume=1_000_000):
    rows = []
    price = start
    for i in range(n):
        price += step
        rows.append({
            "ts": i,
            "open": round(price - 0.03, 4),
            "high": round(price + 0.08, 4),
            "low": round(price - 0.08, 4),
            "close": round(price, 4),
            "volume": volume + i * 1000,
        })
    return rows


def _sample_snapshot(*, daily_locked=False):
    hist = _trend_rows()
    current = hist[-1]["close"]
    return {
        "symbol": "WOLF",
        "current_price": current,
        "history": hist,
        "spy_history": _trend_rows(start=400, step=0.10, n=80, volume=50_000_000),
        "qqq_history": _trend_rows(start=350, step=0.12, n=80, volume=40_000_000),
        "spx_history": _trend_rows(start=5200, step=1.1, n=80, volume=0),
        "ixic_history": _trend_rows(start=17000, step=4.0, n=80, volume=0),
        "sector_history": _trend_rows(start=220, step=0.22, n=80, volume=5_000_000),
        "vix_history": _trend_rows(start=13.0, step=0.0, n=30, volume=0),
        "week52_low": 9.0,
        "week52_high": current + 1.0,
        "avg_volume": 1_000_000,
        "volume": 2_400_000,
        "sector": "Technology",
        "sector_etf": "SMH",
        "earnings": {
            "actual_eps": 0.12,
            "estimate_eps": 0.09,
            "revenue": 120_000_000,
            "revenue_year_ago": 95_000_000,
            "guidance": "Management raised guidance with a strong outlook for next quarter.",
        },
        "news": [
            {"title": "WOLF wins new contract and launches upgraded product", "symbols": ["WOLF"], "sentiment": 0.8},
            {"title": "Fed rate cut hopes rise as CPI cools", "category": "macro", "sentiment": 0.4},
        ],
        "macro_news": [
            {"title": "Fed rate cut hopes rise as CPI cools", "category": "macro", "sentiment": 0.4},
        ],
        "insider_trading": {"net_shares": 50_000, "buys": 3, "sells": 0},
        "institutional_ownership": {"institutional_pct": 67.0, "recent_change_pct": 4.5},
        "analysts": {
            "current_price": current,
            "price_target_avg": current * 1.25,
            "recommendations": {"strong_buy": 3, "buy": 6, "hold": 2, "underperform": 0, "sell": 0},
        },
        "vix": 13.5,
        "fed_rate": 4.75,
        "cpi_yoy": 2.9,
        "stop_loss": current * 0.95,
        "target_price": current * 1.16,
        "market_correlation": 0.52,
        "sector_exposure_pct": 18.0,
        "open_positions": 2,
        "risk": {
            "risk_pct_per_trade": 1.0,
            "account_size_usd": 25000,
            "sector_exposure_pct": 18.0,
            "open_positions": 2,
        },
        "daily_loss_lock": {
            "locked": daily_locked,
            "should_lock": daily_locked,
            "daily_loss_limit_usd": 250,
            "realized_pnl_usd": -260 if daily_locked else 0,
            "losses_today": 3 if daily_locked else 0,
            "reason": "test lock" if daily_locked else "",
        },
    }


def test_super_ghost_manifest_has_exact_25_point_checklist():
    manifest = checklist_manifest()
    assert [x["id"] for x in manifest] == list(range(1, 26))
    assert len(CHECKLIST) == 25
    categories = {x["category"] for x in manifest}
    assert categories == {
        "company_fundamentals_news",
        "price_action_performance",
        "market_context_indicators",
        "risk_management_planning",
    }


def test_super_ghost_full_snapshot_scores_all_25_points():
    report = build_super_ghost("WOLF", snapshot=_sample_snapshot())
    assert report["ok"] is True
    assert report["prediction"]["direction"] == "UP"
    assert report["prediction"]["accuracy_grade"] in {"A+", "A", "B+", "B"}
    assert len(report["checklist"]) == 25
    assert report["coverage"]["available"] == 25
    assert report["coverage"]["total"] == 25
    by_id = {item["id"]: item for item in report["checklist"]}
    assert set(by_id) == set(range(1, 26))
    assert by_id[1]["key"] == "eps" and by_id[1]["status"] in {"bullish", "strong_bullish"}
    assert by_id[4]["key"] == "news_catalysts" and by_id[4]["available"] is True
    assert by_id[20]["key"] == "risk_reward" and by_id[20]["value"]["risk_reward_ratio"] >= 2
    assert "Super Ghost Brain" in report["ai_brain"]["name"]


def test_super_ghost_never_fakes_unknown_data():
    report = build_super_ghost("WOLF", snapshot={"symbol": "WOLF"})
    assert report["ok"] is True
    assert len(report["checklist"]) == 25
    assert report["prediction"]["action"] == "NO EDGE — WATCH ONLY"
    assert report["coverage"]["available"] < 25
    unknown = [x for x in report["checklist"] if not x["available"]]
    assert unknown
    assert any(x["key"] == "eps" for x in unknown)


def test_super_ghost_daily_loss_lock_blocks_prediction_action():
    report = build_super_ghost("WOLF", snapshot=_sample_snapshot(daily_locked=True))
    assert report["prediction"]["direction"] == "UP"
    assert report["prediction"]["action"] == "NO PREDICTION — RISK BLOCKED"
    assert any(b["key"] == "daily_loss_limit" for b in report["prediction"]["blockers"])


def test_super_ghost_endpoint(monkeypatch):
    from api import wolf_endpoints

    wolf_endpoints._CACHE.clear()

    def fake_build(symbol):
        return {"ok": True, "symbol": symbol, "engine": "test", "prediction": {"direction": "HOLD"}, "checklist": []}

    monkeypatch.setattr("core.super_ghost.build_super_ghost", fake_build)
    client = TestClient(wolf_app.APP)
    r = client.get("/api/wolf/super-ghost?symbol=ABCD")
    assert r.status_code == 200
    assert r.json()["symbol"] == "ABCD"


def _risk_off_snapshot():
    """Same strong stock, but a falling broad market + high VIX (risk-off)."""
    snap = _sample_snapshot()
    snap["spx_history"] = [{"ts": i, "close": 5200 - i * 2.0, "volume": 0} for i in range(80)]
    snap["qqq_history"] = [{"ts": i, "close": 350 - i * 0.3, "volume": 0} for i in range(80)]
    snap["ixic_history"] = [{"ts": i, "close": 17000 - i * 10.0, "volume": 0} for i in range(80)]
    snap["sector_history"] = [{"ts": i, "close": 220 - i * 0.4, "volume": 0} for i in range(80)]
    snap["vix"] = 32.0
    snap["vix_history"] = [{"ts": i, "close": 32.0, "volume": 0} for i in range(30)]
    return snap


def test_super_ghost_report_includes_market_regime_block():
    report = build_super_ghost("WOLF", snapshot=_sample_snapshot())
    assert "market_regime" in report
    mr = report["market_regime"]
    for key in ("label", "risk_state", "conviction_multiplier", "macro_inputs_available"):
        assert key in mr
    # ai_brain must now explain the regime effect (deterministic, no key needed).
    assert "regime_effect" in report["ai_brain"]


def test_super_ghost_regime_dampens_conviction_in_risk_off_high_vol():
    risk_on = build_super_ghost("WOLF", snapshot=_sample_snapshot())
    risk_off = build_super_ghost("WOLF", snapshot=_risk_off_snapshot())
    # Same underlying bullish stock; a risk-off, high-VIX tape must lower an UP
    # call's conviction and apply a sub-1.0 regime multiplier.
    assert risk_off["market_regime"]["risk_state"] == "risk_off"
    assert risk_off["market_regime"]["high_volatility"] is True
    assert risk_off["market_regime"]["conviction_multiplier"] < 1.0
    assert risk_off["prediction"]["conviction_score"] < risk_on["prediction"]["conviction_score"]


def test_detect_market_regime_unknown_when_no_macro():
    # Empty snapshot -> all macro items unknown -> neutral multiplier, no guess.
    report = build_super_ghost("WOLF", snapshot={"symbol": "WOLF"})
    mr = report["market_regime"]
    assert mr["label"] == "unknown"
    assert mr["conviction_multiplier"] == 1.0


def test_super_ghost_ai_brief_degrades_without_key(monkeypatch):
    monkeypatch.setattr("core.super_ghost.ANTHROPIC_KEY", "")
    report = build_super_ghost("WOLF", snapshot=_sample_snapshot(), ai=True)
    assert "ai_brief" in report
    assert report["ai_brief"]["available"] is False
    # The deterministic brain is still present so the report is never empty.
    assert "Super Ghost Brain" in report["ai_brain"]["name"]


def test_super_ghost_ai_brief_uses_real_brain_when_key_present(monkeypatch):
    monkeypatch.setattr("core.super_ghost.ANTHROPIC_KEY", "test-key")

    class FakeResp:
        status_code = 200

        def json(self):
            return {"content": [{"type": "text", "text": (
                '{"thesis":"demand improving","news_read":"contract + launch bullish",'
                '"regime_effect":"risk-on supports longs","bull_case":["EPS beat"],'
                '"bear_case":["thin float"],"what_would_change_my_mind":["guidance cut"],'
                '"trust":"medium","one_liner":"Constructive but verify"}'
            )}]}

    import requests as _rq
    monkeypatch.setattr(_rq, "post", lambda *a, **k: FakeResp())
    report = build_super_ghost("WOLF", snapshot=_sample_snapshot(), ai=True)
    brief = report["ai_brief"]
    assert brief["available"] is True
    assert brief["format"] == "json"
    assert brief["trust"] == "medium"
    assert "thesis" in brief and brief["thesis"]


def test_super_ghost_endpoint_ai_param_passes_through(monkeypatch):
    from api import wolf_endpoints

    wolf_endpoints._CACHE.clear()
    captured = {}

    def fake_build(symbol, ai=False):
        captured["symbol"] = symbol
        captured["ai"] = ai
        return {"ok": True, "symbol": symbol, "engine": "test", "prediction": {"direction": "HOLD"}, "checklist": [], "ai_brief": {"available": False}}

    monkeypatch.setattr("core.super_ghost.build_super_ghost", fake_build)
    client = TestClient(wolf_app.APP)
    r = client.get("/api/wolf/super-ghost?symbol=ABCD&ai=1")
    assert r.status_code == 200
    assert captured["ai"] is True
