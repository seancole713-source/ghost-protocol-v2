from fastapi.testclient import TestClient

import wolf_app
from core.super_ghost import CHECKLIST, build_super_ghost, checklist_manifest


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
