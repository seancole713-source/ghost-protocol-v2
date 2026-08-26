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
from core.external_radar import run_external_radar_cycle, select_external_radar_seeds
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
    assert row["advisory_only"] is True
    assert row["decision_eligible"] is False
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

    assert result == {
        "observations_deleted": 3,
        "radar_observations_deleted": 2,
        "radar_snapshots_deleted": 2,
        "snapshots_deleted": 2,
    }
    assert len(cursor.calls) == 4
    assert all("DELETE FROM" in sql for sql, _ in cursor.calls)


def _external_seed(symbol, screen, *, source_ts=1_990, received_ts=2_000, rank=1):
    return {
        "provider": "yahoo_saved_screener", "screen": screen, "symbol": symbol,
        "source_ts": source_ts, "received_ts": received_ts, "rank": rank,
        "price": 20.0, "move_pct": 12.0, "volume": 2_000_000,
        "avg_volume": 500_000, "observation_id": f"{screen}:{symbol}:{source_ts}",
        "validation_valid": True, "quarantined": True,
        "in_official_watchlist": False, "first_seen_ts": received_ts,
    }


def test_external_radar_selector_deduplicates_and_preserves_screen_provenance():
    rows = [
        _external_seed("KSS", "day_gainers", source_ts=1_980, rank=2),
        _external_seed("KSS", "day_gainers", source_ts=1_990, rank=1),
        _external_seed("KSS", "most_shorted_stocks", source_ts=1_985, rank=3),
        _external_seed("DY", "day_gainers", source_ts=1_000, rank=1),  # stale
        {**_external_seed("ANF", "day_gainers"), "in_official_watchlist": True,
         "quarantined": False},
    ]

    selected = select_external_radar_seeds(
        rows, now_ts=2_000, max_age_s=300, per_screen_cap=10, total_cap=20,
    )

    assert [item["symbol"] for item in selected] == ["KSS"]
    assert {origin["screen"] for origin in selected[0]["origins"]} == {
        "day_gainers", "most_shorted_stocks",
    }
    assert next(origin for origin in selected[0]["origins"] if origin["screen"] == "day_gainers")["source_ts"] == 1_990


def test_external_radar_cycle_uses_one_batch_and_never_calls_trade_paths(monkeypatch):
    import core.external_context_ledger as ledger
    import core.external_radar as radar
    import core.squeeze_monitor as monitor
    import core.squeeze_outcomes as outcomes

    seeds = [{"symbol": "KSS", "first_seen_ts": 1_990, "origins": [{
        "provider": "yahoo_saved_screener", "screen": "day_gainers",
        "observation_id": "obs-1", "source_ts": 1_990, "received_ts": 1_995,
        "rank": 1, "discovery_price": 20.0, "discovery_move_pct": 12.0,
    }]}]
    batch_calls = []
    stored = {}
    monkeypatch.setattr(radar, "load_external_radar_seeds", lambda now_ts=None: seeds)
    monkeypatch.setattr(monitor, "batched_market_metrics", lambda symbols: (
        batch_calls.append(list(symbols)) or {"KSS": {
            "price": 22.0, "prior_close": 20.0, "session_high": 23.0,
            "session_volume": 2_000_000, "avg_daily_volume": 500_000,
            "price_as_of_ts": "2026-01-01T15:00:00Z",
        }}
    ))
    monkeypatch.setattr(ledger, "store_external_radar_snapshot", lambda run, items: stored.update({"run": run, "items": items}) or True)
    monkeypatch.setattr(monitor, "candidate_to_pick", lambda *a, **k: (_ for _ in ()).throw(AssertionError("candidate called")))
    monkeypatch.setattr(monitor, "_maybe_alert", lambda *a, **k: (_ for _ in ()).throw(AssertionError("alert called")))
    monkeypatch.setattr(outcomes, "record_squeeze_prediction", lambda *a, **k: (_ for _ in ()).throw(AssertionError("outcome called")))

    result = run_external_radar_cycle(now_ts=2_000)

    assert batch_calls == [["KSS"]]
    assert result["status"] == "complete"
    assert result["items"][0]["observed_current_move_pct"] == 10.0
    assert result["items"][0]["observed_peak_move_pct"] == 15.0
    assert result["items"][0]["advisory_only"] is True
    assert result["items"][0]["decision_eligible"] is False
    assert "confidence" not in result["items"][0]
    assert "buy" not in result["items"][0]
    assert stored["run"]["run_id"] == "external-radar:2000"


def test_external_radar_batch_miss_does_not_fallback(monkeypatch):
    import core.external_context_ledger as ledger
    import core.external_radar as radar
    import core.squeeze_monitor as monitor

    monkeypatch.setattr(radar, "load_external_radar_seeds", lambda now_ts=None: [
        {"symbol": "DY", "first_seen_ts": 1_990, "origins": []},
    ])
    monkeypatch.setattr(monitor, "batched_market_metrics", lambda symbols: {})
    monkeypatch.setattr(monitor, "_sync_fetch_metrics", lambda *a, **k: (_ for _ in ()).throw(AssertionError("fallback called")))
    monkeypatch.setattr(ledger, "store_external_radar_snapshot", lambda run, items: True)

    result = run_external_radar_cycle(now_ts=2_000)

    assert result["status"] == "unavailable"
    assert result["items"][0]["missing_reason"] == "missing_batch_market_data"


def test_external_radar_snapshot_reages_stale_and_failed_runs(monkeypatch):
    import core.db as db
    import core.external_context_ledger as ledger

    class Cursor:
        def __init__(self, status):
            self.status = status
            self.call = 0

        def execute(self, _sql, _params=None):
            self.call += 1

        def fetchone(self):
            return ("run-1", 1_000, self.status, 1, 0, 1, self.status == "unavailable", "note")

        def fetchall(self):
            return []

    class Connection:
        def __init__(self, status):
            self.cursor_value = Cursor(status)

        def cursor(self):
            return self.cursor_value

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setenv("EXTERNAL_RADAR_SNAPSHOT_TTL_S", "300")
    monkeypatch.setattr(ledger.time, "time", lambda: 2_000)
    monkeypatch.setattr(db, "db_conn", lambda: Connection("complete"))
    stale = ledger.latest_external_radar_snapshot()
    assert stale["ok"] is False
    assert stale["status"] == "stale"
    assert stale["snapshot_age_s"] == 1_000

    monkeypatch.setattr(ledger.time, "time", lambda: 1_100)
    monkeypatch.setattr(db, "db_conn", lambda: Connection("unavailable"))
    unavailable = ledger.latest_external_radar_snapshot()
    assert unavailable["ok"] is False
    assert unavailable["status"] == "unavailable"


def test_snapshot_routes_never_poll_providers(monkeypatch):
    from api.routes_ghost_system import router

    monkeypatch.setattr(
        "core.external_context_ledger.recent_external_discoveries",
        lambda limit=50: {"ok": True, "items": [], "count": 0,
                          "advisory_only": True, "decision_eligible": False},
    )
    monkeypatch.setattr(
        "core.external_context_ledger.latest_external_radar_snapshot",
        lambda: {"ok": True, "status": "complete", "items": [{
            "symbol": "ANF", "observed_current_move_pct": 37.0,
            "advisory_only": True, "decision_eligible": False,
        }], "advisory_only": True, "decision_eligible": False},
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
        radar = client.get("/api/intelligence/external-radar")
        market = client.get("/api/intelligence/broad-market")

    assert discovery.status_code == 200
    assert discovery.json()["decision_eligible"] is False
    assert radar.status_code == 200
    assert radar.json()["items"][0]["advisory_only"] is True
    assert "confidence" not in radar.json()["items"][0]
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
