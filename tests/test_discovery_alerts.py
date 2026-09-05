"""Market-wide discovery alerts.

GPRO announced a merger on 2026-09-01 and ran ~183% in five days. Ghost never
mentioned it -- not because it predicted badly, but because GPRO is not in
config/symbols.py, and that hardcoded 107-symbol list is the entire universe
Ghost scans, models and picks from. It was never looked at.

The external screener had been pulling market-wide movers hourly into
ghost_external_observations the whole time. That lane is walled off by design:
external_screener_ingest "never mutates Ghost's symbol universe, creates
candidates, sends alerts, changes confidence, or touches a wallet". The
boundary is right -- unvalidated third-party rows must never reach the fire
path -- but "must not become a pick" had been implemented as "must not be
seen", and those are different requirements.

These tests pin the two things that make closing that gap safe:

  * a discovery is surfaced even when Ghost cannot model the symbol (the GPRO
    case), and is explicitly flagged as such
  * nothing produced here is ever trade-eligible
"""
from __future__ import annotations

import time

import core.discovery_alerts as da


def _obs(symbol, move_pct, *, in_watchlist=False, validation_valid=True,
         age=600, screen="day_gainers"):
    """Mirror a real ledger row.

    `quarantined` is DERIVED here, never passed in. In production
    normalize_external_observation computes it as
    `symbol and not in_official_watchlist`, so in_watchlist=False always
    implies quarantined=True. The original fixture let a caller set the two
    independently, which allowed a combination the ledger cannot produce and
    hid the fact that this module dropped every row it existed to surface.
    """
    return {
        "provider": "yahoo", "screen": screen, "symbol": symbol,
        "source_ts": 1_788_000_000, "received_ts": 1_788_000_100,
        "source_age_s": age, "rank": 1, "price": 1.72,
        "move_pct": move_pct, "volume": 5_000_000, "avg_volume": 900_000,
        "external_score": None, "in_official_watchlist": in_watchlist,
        "quarantined": not in_watchlist, "delayed": False, "freshness": "fresh",
        "validation_valid": validation_valid,
        "advisory_only": True, "decision_eligible": False,
    }


def _patch(monkeypatch, items):
    import core.external_context_ledger as ledger
    monkeypatch.setattr(
        ledger, "recent_external_discoveries",
        lambda **kw: {"ok": True, "items": items, "count": len(items)},
    )


# --------------------------------------------------------------- the GPRO case --

def test_a_mover_outside_the_watchlist_is_surfaced(monkeypatch):
    """The whole point. GPRO is not modellable by Ghost, and that is exactly
    why a human needs to be told about it."""
    _patch(monkeypatch, [_obs("GPRO", 183.0, in_watchlist=False)])

    out = da.build_discovery_alerts()

    assert out["alert_count"] == 1
    alert = out["alerts"][0]
    assert alert["symbol"] == "GPRO"
    assert alert["in_watchlist"] is False
    assert alert["ghost_can_model_it"] is False
    assert out["outside_watchlist_count"] == 1


def test_alerts_are_never_trade_eligible(monkeypatch):
    """A discovery is a reason to look, never a reason to trade -- and a
    symbol that already ran is often the worst thing to buy."""
    _patch(monkeypatch, [_obs("GPRO", 183.0)])

    out = da.build_discovery_alerts()

    assert out["decision_eligible"] is False
    assert out["advisory_only"] is True
    assert all(a["decision_eligible"] is False for a in out["alerts"])


def test_this_module_never_writes(monkeypatch):
    """external_screener_ingest's invariant survives because the alerting is a
    consumer, not part of the ingest path."""
    import core.external_context_ledger as ledger

    def explode(*a, **kw):
        raise AssertionError("discovery alerts must not write")

    monkeypatch.setattr(ledger, "store_external_observation", explode, raising=False)
    monkeypatch.setattr(ledger, "store_external_radar_snapshot", explode, raising=False)

    _patch(monkeypatch, [_obs("GPRO", 183.0)])
    da.build_discovery_alerts()


# ------------------------------------------------------------------ filtering --

def test_small_moves_are_not_alerts(monkeypatch):
    _patch(monkeypatch, [_obs("AAPL", 1.2, in_watchlist=True)])

    assert da.build_discovery_alerts()["alert_count"] == 0


def test_stale_rows_are_dropped(monkeypatch):
    """A week-old screen row is history, not news."""
    _patch(monkeypatch, [_obs("GPRO", 183.0, age=7 * 86400)])

    assert da.build_discovery_alerts()["alert_count"] == 0


def test_rows_that_failed_validation_are_dropped(monkeypatch):
    """The real quality filter: bad symbol, missing/future timestamp, stale
    past the provider's own bound, non-positive price."""
    _patch(monkeypatch, [_obs("SCAM", 400.0, validation_valid=False)])

    out = da.build_discovery_alerts()

    assert out["alert_count"] == 0
    assert out["dropped"]["invalid"] == 1


def test_quarantine_is_not_a_reason_to_hide_a_mover(monkeypatch):
    """Regression pin for the bug that made PR #182 dead on arrival.

    In this ledger `quarantined` means "not in config/symbols.py", not "bad
    data" -- normalize_external_observation sets it as
    `symbol and not in_official_watchlist`. Dropping on it meant the alert
    list could only ever contain the 107 symbols Ghost already models, i.e.
    exactly the rows a discovery lane does not need to report."""
    row = _obs("GPRO", 183.0, in_watchlist=False)
    assert row["quarantined"] is True, "fixture no longer mirrors the ledger"

    _patch(monkeypatch, [row])

    assert da.build_discovery_alerts()["alert_count"] == 1


def test_the_ledger_really_does_quarantine_everything_off_watchlist():
    """Pins the upstream invariant the test above depends on, against the real
    normalizer rather than a fixture."""
    from core.external_context_ledger import normalize_external_observation

    row = normalize_external_observation(
        provider="yahoo_saved_screener", provider_family="yahoo",
        screen="day_gainers", raw_symbol="GPRO", source_ts=int(time.time()),
        payload={}, price=1.72, move_pct=183.0,
    )

    assert row["in_official_watchlist"] is False
    assert row["quarantined"] is True


def test_duplicate_symbols_keep_the_largest_move(monkeypatch):
    """Screens overlap; the same symbol appears under several of them."""
    _patch(monkeypatch, [
        _obs("GPRO", 40.0, screen="day_gainers"),
        _obs("GPRO", 183.0, screen="most_actives"),
    ])

    out = da.build_discovery_alerts()

    assert out["alert_count"] == 1
    assert out["alerts"][0]["move_pct"] == 183.0


def test_alerts_rank_by_absolute_move(monkeypatch):
    """Large declines matter as much as large gains."""
    _patch(monkeypatch, [
        _obs("A", 12.0), _obs("B", -95.0), _obs("C", 40.0),
    ])

    symbols = [a["symbol"] for a in da.build_discovery_alerts()["alerts"]]

    assert symbols == ["B", "C", "A"]


def test_a_ledger_failure_degrades_instead_of_raising(monkeypatch):
    import core.external_context_ledger as ledger
    monkeypatch.setattr(
        ledger, "recent_external_discoveries",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("db down")),
    )

    out = da.build_discovery_alerts()

    assert out["alerts"] == []
    assert "db down" in out["error"]


def test_job_registers_with_an_explicit_initial_delay():
    """register() defers by a full interval otherwise -- the PR #178 trap."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "wolf_app.py").read_text(encoding="utf-8")
    idx = src.index('"discovery_alerts", _discovery_alerts_job')

    assert "initial_delay_s=" in src[idx:idx + 200]


# ------------------------------------------------- why-zero diagnostics --

def test_zero_alerts_explains_itself(monkeypatch):
    """An empty list is ambiguous: a quiet tape and a filter that can never
    pass look identical, and that ambiguity is how a dead lane survives. The
    payload must say which it was."""
    _patch(monkeypatch, [
        _obs("AAPL", 2.1, in_watchlist=True),
        _obs("MSFT", -3.4, in_watchlist=True),
    ])

    out = da.build_discovery_alerts()

    assert out["alert_count"] == 0
    assert out["max_move_seen_pct"] == -3.4, "largest observed move not reported"
    assert out["dropped"]["below_threshold"] == 2


def test_rows_without_a_move_are_distinguished_from_small_moves(monkeypatch):
    """max_move_seen_pct of None means nothing usable is arriving at all --
    a different problem from a quiet market, and it must not look the same."""
    rows = [_obs("A", 5.0), _obs("B", None)]
    _patch(monkeypatch, rows)

    out = da.build_discovery_alerts()

    assert out["dropped"]["no_move"] == 1
    assert out["dropped"]["below_threshold"] == 1
    assert out["max_move_seen_pct"] == 5.0


def test_stale_and_invalid_drops_are_counted_separately(monkeypatch):
    _patch(monkeypatch, [
        _obs("A", 50.0, age=7 * 86400),
        _obs("B", 50.0, validation_valid=False),
    ])

    out = da.build_discovery_alerts()

    assert out["dropped"]["stale"] == 1
    assert out["dropped"]["invalid"] == 1
    assert out["alert_count"] == 0
