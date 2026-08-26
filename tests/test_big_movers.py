"""Big Movers contract tests: immutable official forecasts only."""
from __future__ import annotations

from typing import Any

import core.big_movers as bm

NOW = 2_000_000_000


class _Cursor:
    description = [
        ("id",), ("symbol",), ("entry_price",), ("target_price",),
        ("predicted_at",), ("expires_at",),
    ]

    def __init__(self, rows: list[tuple[Any, ...]]):
        self.rows = rows
        self.sql = ""
        self.params: tuple[Any, ...] = ()

    def execute(self, sql: str, params: tuple[Any, ...]):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, cursor: _Cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _row(
    symbol: str = "WOLF",
    entry: float = 100.0,
    target: float = 106.0,
    predicted_at: int = NOW - 60,
    expires_at: int = NOW + 3 * 86_400,
):
    return (7, symbol, entry, target, predicted_at, expires_at)


def _snapshot(rows, session_loader=None, floor=5.0):
    cursor = _Cursor(rows)
    calls: list[list[str]] = []

    def default_loader(symbols):
        calls.append(list(symbols))
        return {
            "as_of_ts": NOW,
            "sessions": {
                symbol: {
                    "ok": True,
                    "price": 99.0,
                    "provider_state": "live",
                    "price_source": "trade",
                    "freshness_seconds": 4,
                }
                for symbol in symbols
            },
        }

    result = bm.big_movers_snapshot(
        floor,
        now_ts=NOW,
        db_conn_factory=lambda: _Connection(cursor),
        session_loader=session_loader or default_loader,
    )
    return result, cursor, calls


def test_includes_genuine_active_official_five_percent_forecast():
    result, cursor, calls = _snapshot([_row(target=105.0)])
    assert result["ok"] is True
    assert result["status"] == "active"
    assert result["market_wide"] is False
    assert result["scope"] == "official_watchlist"
    assert result["universe_size"] == len(bm.OFFICIAL_WATCHLIST)
    assert result["gain_basis"] == "issued_target_vs_issued_entry"
    assert calls == [["WOLF"]]  # one bounded batch enrichment call
    assert "direction IN ('UP', 'BUY')" in cursor.sql
    assert "outcome IS NULL" in cursor.sql
    assert "expires_at > %s" in cursor.sql
    assert "expires_at - predicted_at >= %s" in cursor.sql
    assert "scores->>'research_pick'" in cursor.sql
    assert cursor.params[3] == list(bm.OFFICIAL_WATCHLIST)
    item = result["items"][0]
    assert item["forecast_gain_pct"] == 5.0
    assert item["forecast_target_price"] == 105.0
    assert item["issued_entry_price"] == 100.0
    assert item["current_price"] == 99.0
    assert item["official_live_prediction"] is True
    assert item["research_pick"] is False


def test_gain_uses_issued_entry_not_fallen_current_price():
    result, _, _ = _snapshot([_row(target=102.0)])
    # A later $99 quote would make target/current > 3%, but immutable issued
    # target/entry remains only 2%; the defensive serializer rejects it.
    assert result["items"] == []
    assert result["status"] == "empty"


def test_defensive_filter_rejects_outside_one_day_to_two_week_horizon():
    rows = [
        _row(symbol="NOTOFFICIAL", target=130.0),
        _row(target=110.0, expires_at=NOW + 23 * 60 * 60),
        _row(target=110.0, expires_at=NOW + 15 * 86_400),
    ]
    result, _, calls = _snapshot(rows)
    assert result["items"] == []
    # Poison rows are rejected before the bounded market-session call.
    assert calls == []


def test_missing_current_price_is_explicit_not_fabricated():
    def unavailable(symbols):
        return {
            "as_of_ts": NOW,
            "sessions": {
                symbol: {
                    "ok": False,
                    "price": None,
                    "provider_state": "breaker_open",
                    "freshness_seconds": None,
                }
                for symbol in symbols
            },
        }

    result, _, _ = _snapshot([_row()], session_loader=unavailable)
    item = result["items"][0]
    assert item["current_price"] is None
    assert item["current_price_state"] == "breaker_open"
    assert item["forecast_gain_pct"] == 6.0


def test_empty_result_avoids_provider_call_and_is_truthful():
    provider_called = False

    def provider(_symbols):
        nonlocal provider_called
        provider_called = True
        return {}

    result, cursor, _ = _snapshot([], session_loader=provider)
    assert provider_called is False
    assert result["ok"] is True
    assert result["status"] == "empty"
    assert "No active official Ghost forecast" in result["empty_reason"]
    assert result["date_semantics"] == "target_window_deadline_not_exact_hit_date"
    assert cursor.params[4] == 5.0


def test_requested_floor_cannot_weaken_five_percent_contract():
    result, cursor, _ = _snapshot([], floor=1.0)
    assert result["min_forecast_gain_pct"] == 5.0
    assert cursor.params[4] == 5.0
