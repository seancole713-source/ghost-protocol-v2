"""External discovery and broad-market advisory boundary tests."""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.broad_market_context import (
    MARKET_INSTRUMENTS,
    build_market_snapshot,
    normalize_market_observation,
)
from core.external_context_ledger import (
    _current_freshness,
    normalize_external_observation,
    prune_external_context,
)
from core.external_screener_ingest import parse_yahoo_screen


def test_external_observation_preserves_missing_source_timestamp():
    row = normalize_external_observation(
        provider="provider", provider_family="test", screen="squeeze",
        raw_symbol="ARCT", source_ts=None, payload={"symbol": "ARCT"},
        price=20.0, received_ts=2_000,
    )

    assert row["source_ts"] is None
    assert row["freshness"] == "unknown"
    assert row["validation_valid"] is False
    assert "missing_source_timestamp" in row["validation_reasons"]
    assert row["advisory_only"] is True
    assert row["decision_eligible"] is False


def test_external_symbol_is_quarantined_without_expanding_watchlist():
    from config.symbols import OFFICIAL_WATCHLIST

    before = tuple(OFFICIAL_WATCHLIST)
    row = normalize_external_observation(
        provider="provider", provider_family="test", screen="squeeze",
        raw_symbol="ZZZZ", source_ts=1_900, payload={"symbol": "ZZZZ"},
        price=20.0, received_ts=2_000,
    )

    assert row["validation_valid"] is True
    assert row["in_official_watchlist"] is False
    assert row["quarantined"] is True
    assert tuple(OFFICIAL_WATCHLIST) == before


def test_yahoo_parser_keeps_matched_price_timestamp():
    payload = {"finance": {"result": [{"quotes": [{
        "symbol": "ARCT", "marketState": "POST",
        "regularMarketPrice": 20.0, "regularMarketTime": 1_800,
        "postMarketPrice": 23.0, "postMarketTime": 1_950,
        "regularMarketChangePercent": 15.0,
        "regularMarketVolume": 2_000_000,
        "averageDailyVolume3Month": 500_000,
    }]}]}}

    rows = parse_yahoo_screen(
        payload, screen="day_gainers", received_ts=2_000,
    )

    assert len(rows) == 1
    assert rows[0]["price"] == 23.0
    assert rows[0]["source_ts"] == 1_950
    assert rows[0]["validation_valid"] is True
    assert rows[0]["in_official_watchlist"] is True


def test_market_context_is_display_only_and_labels_proxies():
    now = int(time.time())
    future = normalize_market_observation(
        MARKET_INSTRUMENTS[0], price=5_200, previous_close=5_150,
        source_ts=now - 30, received_at=now,
    )
    proxy = normalize_market_observation(
        MARKET_INSTRUMENTS[1], price=520, previous_close=515,
        source_ts=now - 30, received_at=now,
    )
    snapshot = build_market_snapshot([future, proxy], received_at=now)

    assert future["kind"] == "future"
    assert proxy["kind"] == "etf_proxy"
    assert snapshot["status"] == "partial"
    assert snapshot["display_only"] is True
    assert snapshot["decision_eligible"] is False
    assert snapshot["scoring_version"] is None


def test_market_context_missing_timestamp_is_unknown_not_receipt_time():
    row = normalize_market_observation(
        MARKET_INSTRUMENTS[0], price=5_200, previous_close=5_150,
        source_ts=None, received_at=9_999,
    )

    assert row["source_ts"] is None
    assert row["source_age_s"] is None
    assert row["stale"] is True
    assert row["valid"] is False
    assert "missing_source_timestamp" in row["validation_reasons"]


def test_market_snapshot_requires_fresh_observations_for_availability():
    stale = normalize_market_observation(
        MARKET_INSTRUMENTS[0], price=5_200, previous_close=5_150,
        source_ts=1_000, received_at=4_000, max_age_s=300,
    )

    snapshot = build_market_snapshot([stale], received_at=4_000)

    assert stale["valid"] is True
    assert stale["stale"] is True
    assert snapshot["ok"] is False
    assert snapshot["status"] == "stale"
    assert snapshot["valid_count"] == 1
    assert snapshot["fresh_count"] == 0


def test_external_freshness_is_recomputed_at_read_time():
    age, freshness = _current_freshness(1_000, now_ts=4_000, max_age_s=300)

    assert age == 3_000
    assert freshness == "stale"


def test_external_context_retention_is_bounded():
    class Cursor:
        def __init__(self):
            self.calls = []
            self.rowcount = 0

        def execute(self, sql, params):
            self.calls.append((sql, params))
            self.rowcount = 3 if "ghost_external_observations" in sql else 2

    cursor = Cursor()
    result = prune_external_context(now_ts=10_000_000, cur=cursor)

    assert result == {"observations_deleted": 3, "snapshots_deleted": 2}
    assert len(cursor.calls) == 2
    assert all("DELETE FROM" in sql for sql, _ in cursor.calls)


def test_snapshot_routes_never_poll_providers(monkeypatch):
    from api.routes_ghost_system import router

    monkeypatch.setattr(
        "core.external_context_ledger.recent_external_discoveries",
        lambda limit=50: {"ok": True, "items": [], "count": 0,
                          "advisory_only": True, "decision_eligible": False},
    )
    monkeypatch.setattr(
        "core.broad_market_context.get_broad_market_context",
        lambda: {"ok": True, "status": "partial", "observations": [],
                 "display_only": True, "decision_eligible": False},
    )
    monkeypatch.setattr(
        "core.external_screener_ingest.run_external_screener_cycle",
        lambda: (_ for _ in ()).throw(AssertionError("provider polled")),
    )
    monkeypatch.setattr(
        "core.broad_market_context.refresh_broad_market_context",
        lambda: (_ for _ in ()).throw(AssertionError("provider polled")),
    )
    app = FastAPI()
    app.include_router(router)

    with TestClient(app) as client:
        discovery = client.get("/api/intelligence/external-discovery")
        market = client.get("/api/intelligence/broad-market")

    assert discovery.status_code == 200
    assert discovery.json()["decision_eligible"] is False
    assert market.status_code == 200
    assert market.json()["display_only"] is True


def test_broad_market_route_returns_503_when_snapshot_is_unavailable(monkeypatch):
    from api.routes_ghost_system import router

    monkeypatch.setattr(
        "core.broad_market_context.get_broad_market_context",
        lambda: {"ok": False, "status": "stale", "observations": [],
                 "display_only": True, "decision_eligible": False},
    )
    app = FastAPI()
    app.include_router(router)

    with TestClient(app) as client:
        response = client.get("/api/intelligence/broad-market")

    assert response.status_code == 503
    assert response.json()["status"] == "stale"
    assert response.json()["decision_eligible"] is False


def test_advisory_jobs_have_dedicated_breakers():
    from core.circuit_breaker import (
        _yahoo_screener_cb,
        _yfinance_cb,
        _yfinance_market_context_cb,
    )

    assert _yahoo_screener_cb is not _yfinance_cb
    assert _yfinance_market_context_cb is not _yfinance_cb
    assert _yahoo_screener_cb is not _yfinance_market_context_cb


def test_leader_scheduler_registers_external_snapshot_jobs():
    source = (Path(__file__).resolve().parents[1] / "wolf_app.py").read_text(
        encoding="utf-8"
    )
    assert '"external_screener",' in source
    assert '"broad_market_context",' in source
