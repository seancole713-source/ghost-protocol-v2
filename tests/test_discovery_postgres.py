"""Execute latest-observation selection against PostgreSQL, not mocked SQL."""
from contextlib import contextmanager
import time

import pytest

from core.external_context_ledger import (
    ensure_external_context_tables,
    normalize_external_observation,
    recent_external_discoveries,
    store_external_observation,
)
from tests.test_research_postgres import _isolated_schema


def _store(cur, symbol, *, stamp, received=None, move=6, provider="yahoo_saved_screener",
           screen="day_gainers", price=20):
    row = normalize_external_observation(
        provider=provider, provider_family="test", screen=screen,
        raw_symbol=symbol, source_ts=stamp,
        received_ts=received if received is not None else stamp,
        observation_id=f"{provider}:{screen}:{symbol}:{stamp}:{received}:{move}",
        payload={"move_basis": "premarket" if provider.startswith("yahoo") else "close_to_close"},
        price=price, move_pct=move, volume=1_000_000, avg_volume=900_000,
        max_age_s=4 * 86400 if provider.startswith("polygon") else 1800,
    )
    assert store_external_observation(row, cur=cur)


@contextmanager
def _ledger(monkeypatch):
    import core.db
    with _isolated_schema("discovery_latest") as (conn, _):
        ensure_external_context_tables(conn.cursor())
        @contextmanager
        def factory():
            yield conn
        monkeypatch.setattr(core.db, "db_conn", factory)
        yield conn.cursor()


@pytest.mark.integration
def test_repeated_symbols_do_not_use_up_the_display_budget(monkeypatch):
    now = int(time.time())
    with _ledger(monkeypatch) as cur:
        _store(cur, "BBNX", stamp=now-100)
        for delta in range(1, 70):
            _store(cur, "CRDO", stamp=now-delta)
        out = recent_external_discoveries(limit=2, per_screen=2)
        assert {r["symbol"] for r in out["items"]} == {"CRDO", "BBNX"}
        assert out["available_count"] == 2
        assert out["screen_truncated"] == out["limit_truncated"] == 0
        assert all(r["move_basis"] == "premarket" for r in out["items"])


@pytest.mark.integration
def test_new_invalid_observation_suppresses_old_valid_quote(monkeypatch):
    now = int(time.time())
    with _ledger(monkeypatch) as cur:
        _store(cur, "CRDO", stamp=now-50)
        _store(cur, "CRDO", stamp=now-10, price=None)
        _store(cur, "BAD", stamp=now-50)
        _store(cur, "BAD", stamp=None, received=now-10)
        out = recent_external_discoveries(limit=10)
        assert len(out["items"]) == 2
        assert all(r["validation_valid"] is False for r in out["items"])
        from core.discovery_alerts import build_discovery_alerts
        alerts = build_discovery_alerts()
        assert alerts["alerts"] == []
        assert alerts["dropped"]["invalid"] == 2


@pytest.mark.integration
def test_selection_preserves_each_screen_and_reports_cut_rows(monkeypatch):
    now = int(time.time())
    with _ledger(monkeypatch) as cur:
        _store(cur, "CRDO", stamp=now-10)
        _store(cur, "BBNX", stamp=now-20)
        _store(cur, "BRKR", stamp=now-30)
        _store(cur, "DAILY", stamp=now-86400, received=now-5,
               provider="polygon_grouped_daily", screen="market_wide_daily")
        out = recent_external_discoveries(limit=2, per_screen=2)
        assert out["available_count"] == 4
        assert out["screen_truncated"] == 1
        assert out["limit_truncated"] == 1
        out = recent_external_discoveries(limit=10, per_screen=2)
        assert {r["symbol"] for r in out["items"]} == {"CRDO", "BBNX", "DAILY"}
        daily = next(r for r in out["items"] if r["symbol"] == "DAILY")
        assert daily["freshness"] == "fresh"
        from core.discovery_alerts import build_discovery_alerts
        alerts = build_discovery_alerts()
        assert [r["symbol"] for r in alerts["historical_alerts"]] == ["DAILY"]
        assert "DAILY" not in [r["symbol"] for r in alerts["alerts"]]


@pytest.mark.integration
def test_long_expired_data_is_not_ranked(monkeypatch):
    now = int(time.time())
    with _ledger(monkeypatch) as cur:
        _store(cur, "OLD", stamp=now-10*86400)
        assert recent_external_discoveries()["items"] == []
