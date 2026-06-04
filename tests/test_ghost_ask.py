"""Tests for Ghost Ask (Claude Q&A grounded in live state)."""
import json

from fastapi.testclient import TestClient

import wolf_app


class FakeCursor:
    def __init__(self, rows=None, fetchall_rows=None):
        self.rows = rows or []
        self.fetchall_rows = fetchall_rows or []
        self.last_sql = ""

    def execute(self, sql, params=None):
        self.last_sql = sql

    def fetchone(self):
        if "ghost_ask_day" in self.last_sql:
            return self.rows[0] if self.rows else None
        if "ghost_ask_count" in self.last_sql:
            return self.rows[1] if len(self.rows) > 1 else None
        return None

    def fetchall(self):
        return self.fetchall_rows


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class FakeDbCtx:
    def __init__(self, cursor):
        self._conn = FakeConn(cursor)

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        return False


def _client(monkeypatch):
    monkeypatch.setenv("GHOST_TEST_MODE", "1")
    return TestClient(wolf_app.APP)


def test_ask_requires_question(monkeypatch):
    with _client(monkeypatch) as client:
        r = client.post("/api/wolf/ask", json={})
    assert r.status_code == 400
    assert r.json()["ok"] is False
    assert "question" in r.json()["error"].lower()


def test_ask_no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        "core.ghost_ask.ANTHROPIC_KEY",
        "",
    )
    with _client(monkeypatch) as client:
        r = client.post("/api/wolf/ask", json={"question": "Why silent?"})
    assert r.status_code == 400
    assert "ANTHROPIC" in r.json()["error"]


def test_ask_success_mock_claude(monkeypatch):
    monkeypatch.setattr("core.ghost_ask.ANTHROPIC_KEY", "test-key")
    monkeypatch.setattr(
        "core.ghost_ask.build_ask_context",
        lambda: {
            "ghost_score": {"trade_action": "NO TRADE", "bias_label": "mild bullish bias"},
            "engine_pause": {"paused": True},
            "portfolio": {"positions": []},
        },
    )
    monkeypatch.setattr(
        "core.ghost_ask._check_daily_limit",
        lambda: None,
    )

    def fake_post(url, **kwargs):
        class Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"content": [{"type": "text", "text": "Ghost is in cooldown — no official buy."}]}

        return Resp()

    monkeypatch.setattr("core.ghost_ask.requests.post", fake_post)

    with _client(monkeypatch) as client:
        r = client.post("/api/wolf/ask", json={"question": "Should I buy WOLF now?"})
    body = r.json()
    assert r.status_code == 200
    assert body["ok"] is True
    assert "cooldown" in body["answer"].lower()
    assert body["context_summary"]["trade_action"] == "NO TRADE"


def test_ask_context_route(monkeypatch):
    monkeypatch.setattr(
        "core.ghost_ask.build_ask_context",
        lambda: {"ts": 1, "product_note": "WOLF-only"},
    )
    with _client(monkeypatch) as client:
        r = client.get("/api/wolf/ask/context")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["context"]["product_note"] == "WOLF-only"


def test_daily_limit_blocks(monkeypatch):
    monkeypatch.setattr("core.ghost_ask.ANTHROPIC_KEY", "test-key")
    monkeypatch.setattr(
        "core.ghost_ask._check_daily_limit",
        lambda: "Daily ask limit (40) reached — try again tomorrow CT.",
    )
    with _client(monkeypatch) as client:
        r = client.post("/api/wolf/ask", json={"question": "hello"})
    assert r.status_code == 400
    assert "limit" in r.json()["error"].lower()
