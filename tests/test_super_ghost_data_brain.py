from fastapi.testclient import TestClient

import wolf_app
from core.super_ghost_data_brain import classify_news_articles, parse_form4_xml


def test_parse_form4_xml_open_market_buys_and_sells():
    xml = """
    <ownershipDocument>
      <nonDerivativeTransaction>
        <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
        <transactionAmounts><transactionShares><value>1000</value></transactionShares></transactionAmounts>
      </nonDerivativeTransaction>
      <nonDerivativeTransaction>
        <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
        <transactionAmounts><transactionShares><value>250</value></transactionShares></transactionAmounts>
      </nonDerivativeTransaction>
      <nonDerivativeTransaction>
        <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
        <transactionAmounts><transactionShares><value>999</value></transactionShares></transactionAmounts>
      </nonDerivativeTransaction>
    </ownershipDocument>
    """
    out = parse_form4_xml(xml)
    assert out["available"] is True
    assert out["buys"] == 1
    assert out["sells"] == 1
    assert out["net_shares"] == 750
    assert out["transactions_scanned"] == 3


def test_classify_news_articles_dedupes_and_scores_freshness():
    now = 1_800_000_000
    articles = [
        {"title": "WOLF wins major contract", "published_at": now - 3600},
        {"title": "WOLF wins major contract", "published_at": now - 3500},
        {"title": "WOLF faces lawsuit delay", "published_at": now - 7200},
        {"title": "Management raised guidance with strong outlook", "published_at": now - 1800},
    ]
    out = classify_news_articles(articles, now_ts=now)
    assert out["article_count"] == 4
    assert out["unique_count"] == 3
    assert out["duplicate_count"] == 1
    assert out["freshness"] == "fresh"
    assert "contract" in out["bullish_terms"]
    assert "lawsuit" in out["bearish_terms"]
    assert out["guidance_score"] > 0


def test_data_brain_endpoint(monkeypatch):
    def fake_brain(symbol, use_cache=True):
        return {"ok": True, "symbol": symbol, "coverage": {"sec_fundamentals": True, "form4_activity": False}, "sources": {}, "derived": {"guidance_signal": "neutral"}}

    monkeypatch.setattr("core.super_ghost_data_brain.build_data_brain", fake_brain)
    client = TestClient(wolf_app.APP)
    r = client.get("/api/wolf/super-ghost/data-brain?symbol=WOLF")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["coverage"]["sec_fundamentals"] is True


def test_data_brain_refresh_requires_auth():
    client = TestClient(wolf_app.APP)
    r = client.post("/api/wolf/super-ghost/data-brain/refresh", json={"symbol": "WOLF"})
    assert r.status_code in (401, 403)


def test_data_brain_persist_param_requires_auth(monkeypatch):
    """?persist=1 writes a snapshot row — must be auth-gated like /refresh."""
    monkeypatch.setenv("CRON_SECRET", "prod-like")
    monkeypatch.setenv("GHOST_MCP_TOKEN", "secret")
    monkeypatch.setenv("GHOST_TEST_MODE", "1")  # bypass HTTPS
    calls = {"persisted": False}
    monkeypatch.setattr("core.super_ghost_data_brain.persist_data_brain",
                        lambda symbol: calls.__setitem__("persisted", True))
    client = TestClient(wolf_app.APP)
    r = client.get("/api/wolf/super-ghost/data-brain?symbol=WOLF&persist=1")
    assert r.status_code == 401
    assert calls["persisted"] is False
    r2 = client.get("/api/wolf/super-ghost/data-brain?symbol=WOLF&persist=1",
                    headers={"X-Ghost-Mcp-Token": "secret"})
    assert r2.status_code == 200


def test_super_ghost_merges_available_form4_into_insider_snapshot(monkeypatch):
    from core.super_ghost import build_super_ghost

    def fake_db(symbol):
        return {
            "ok": True,
            "symbol": symbol,
            "coverage": {},
            "derived": {"guidance_signal": "neutral"},
            "sources": {
                "form4_activity": {"available": True, "net_shares": 5000, "buys": 2, "sells": 0, "as_of_ts": 1},
                "news_quality": {"available": False},
                "options_context": {"available": False},
            },
        }

    snap = {
        "symbol": "WOLF",
        "data_brain": fake_db("WOLF"),
        "current_price": 10,
        "history": [{"ts": i, "close": 10+i*0.01, "high": 10+i*0.01, "low": 10+i*0.01, "volume": 1_000_000} for i in range(260)],
        "daily_loss_lock": {"locked": False},
    }
    report = build_super_ghost("WOLF", snapshot=snap)
    by_key = {x["key"]: x for x in report["checklist"]}
    assert by_key["insider_trading"]["available"] is True
    assert by_key["insider_trading"]["score"] > 0
