"""Full-market daily snapshot.

The discovery lane shipped in PR #182 read two hardcoded Yahoo saved screens at
50 rows each: ~100 symbols per cycle out of ~11,000 listed US tickers, with no
day_losers screen at all -- so its absolute-move ranking could never be shown a
crash. This module pulls every US ticker from one Polygon grouped-daily call.

These tests pin what makes that safe rather than merely bigger:

  * one move basis, or none -- an intraday (c-o)/o move is sitting right there
    in the same bar and must never be mixed into the close-to-close column
  * the scan is unfiltered and the STORE is bounded, and the two counts are
    reported separately so the filter cannot be mistaken for the coverage
  * rows stay advisory: no candidate, no universe change, no trade eligibility
  * a daily bar is not stale at 20 hours, which is what killed it the first
    time the intraday freshness bound was applied to it
"""
from __future__ import annotations

import time

import pytest

import core.market_wide_snapshot as mws


def _bar(ticker, close, volume=5_000_000, *, open_=None, ts_ms=None):
    return {
        "T": ticker, "c": close, "v": volume,
        "o": close if open_ is None else open_,
        "h": close * 1.02, "l": close * 0.98, "n": 4200, "vw": close,
        "t": ts_ms if ts_ms is not None else int(time.time() * 1000),
    }


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "testkey")
    monkeypatch.delenv("MARKET_WIDE_MIN_MOVE_PCT", raising=False)
    monkeypatch.delenv("MARKET_WIDE_MIN_PRICE", raising=False)
    monkeypatch.delenv("MARKET_WIDE_MIN_DOLLAR_VOLUME", raising=False)
    monkeypatch.delenv("MARKET_WIDE_MAX_ROWS", raising=False)


# ------------------------------------------------------------ the coverage --

def test_a_decline_is_captured():
    """The structural gap in the Yahoo lane: there is no day_losers screen, so
    a crash could not reach an alert ranker that sorts by absolute move."""
    built = mws.build_market_wide_rows(
        [_bar("CRSH", 40.0)], [_bar("CRSH", 100.0)], latest_day="2026-09-04")

    assert built["eligible"] == 1
    assert built["rows"][0]["symbol"] == "CRSH"
    assert built["rows"][0]["move_pct"] == pytest.approx(-60.0)


def test_a_symbol_outside_the_modelled_universe_is_still_produced():
    """GPRO is not in config/symbols.py. That is the reason to report it."""
    built = mws.build_market_wide_rows(
        [_bar("GPRO", 5.66)], [_bar("GPRO", 2.00)], latest_day="2026-09-04")

    row = built["rows"][0]
    assert row["symbol"] == "GPRO"
    assert row["in_official_watchlist"] is False
    assert row["quarantined"] is True, "ledger semantics changed"
    assert row["validation_valid"] is True, "a valid row must reach the alert lane"


# ---------------------------------------------------------- one move basis --

def test_a_new_listing_gets_no_move_rather_than_a_different_one():
    """The bar carries open and close, so an intraday move is free. It is a
    DIFFERENT measurement and must not land in the same column."""
    built = mws.build_market_wide_rows(
        [_bar("IPOX", 30.0, open_=10.0)], [], latest_day="2026-09-04")

    assert built["rows"] == []
    assert built["dropped"]["no_prior_close"] == 1


def test_move_basis_is_recorded_on_every_row():
    built = mws.build_market_wide_rows(
        [_bar("AAA", 150.0)], [_bar("AAA", 100.0)], latest_day="2026-09-04")

    assert built["move_basis"] == "close_to_close"
    assert built["rows"][0]["raw_payload"]["move_basis"] == "close_to_close"
    assert built["rows"][0]["raw_payload"]["prior_close"] == 100.0


def test_a_cycle_with_one_session_reports_instead_of_falling_back(monkeypatch):
    """One session means no close-to-close basis exists at all."""
    calls = []

    def fetcher(day):
        calls.append(day)
        return ([_bar("AAA", 10.0)], {"status": "available", "day": day}) if len(calls) == 1 \
            else ([], {"status": "available", "day": day})

    out = mws.run_market_wide_cycle(fetcher=fetcher)

    assert out["ok"] is False
    assert out["reason"] == "insufficient_sessions"
    assert out["inserted"] == 0


# --------------------------------------------- scan everything, store some --

def test_the_scan_count_is_the_full_universe_not_the_stored_count():
    """A filter that silently shrinks the reported coverage is how "watches the
    whole market" becomes false without anyone noticing."""
    latest = [_bar(f"S{i}", 100.0 + i) for i in range(50)]
    prior = [_bar(f"S{i}", 100.0) for i in range(50)]

    built = mws.build_market_wide_rows(latest, prior, latest_day="2026-09-04")

    assert built["scanned"] == 50
    assert built["eligible"] < 50, "threshold did nothing; test is not testing"
    assert built["with_prior_close"] == 50


def test_penny_stocks_and_illiquid_names_are_not_stored(monkeypatch):
    """A sub-dollar name doubling on $4k of volume is noise, not news."""
    built = mws.build_market_wide_rows(
        [_bar("PENNY", 0.40, volume=10_000), _bar("THIN", 50.0, volume=20)],
        [_bar("PENNY", 0.20), _bar("THIN", 25.0)],
        latest_day="2026-09-04")

    assert built["rows"] == []
    assert built["dropped"]["below_price"] == 1
    assert built["dropped"]["below_dollar_volume"] == 1


def test_stored_rows_are_capped_and_the_truncation_is_reported(monkeypatch):
    monkeypatch.setenv("MARKET_WIDE_MAX_ROWS", "3")
    latest = [_bar(f"S{i}", 200.0 + i * 10) for i in range(10)]
    prior = [_bar(f"S{i}", 100.0) for i in range(10)]

    built = mws.build_market_wide_rows(latest, prior, latest_day="2026-09-04")

    assert len(built["rows"]) == 3
    assert built["truncated"] == 7
    assert built["eligible"] == 10


def test_biggest_movers_survive_the_cap():
    latest = [_bar("SMALL", 111.0), _bar("HUGE", 400.0), _bar("MID", 150.0)]
    prior = [_bar(s, 100.0) for s in ("SMALL", "HUGE", "MID")]

    built = mws.build_market_wide_rows(latest, prior, latest_day="2026-09-04")

    assert [r["symbol"] for r in built["rows"]] == ["HUGE", "MID", "SMALL"]


def test_max_move_seen_is_measured_before_the_store_filter():
    """Why-zero diagnostics: a quiet tape and a filter nothing can pass must
    not produce the same empty result."""
    built = mws.build_market_wide_rows(
        [_bar("AAA", 101.0)], [_bar("AAA", 100.0)], latest_day="2026-09-04")

    assert built["rows"] == []
    assert built["max_abs_move_seen_pct"] == pytest.approx(1.0)
    assert built["dropped"]["below_move"] == 1


# ------------------------------------------------------------- freshness --

def test_a_daily_bar_is_not_stale():
    """The bar is stamped at the START of its session, so the freshest possible
    row is already ~16h old. The intraday screener's 30-minute bound would set
    validation_valid=FALSE on all of them and the lane would die silently."""
    day_old = int((time.time() - 20 * 3600) * 1000)
    built = mws.build_market_wide_rows(
        [_bar("AAA", 150.0, ts_ms=day_old)], [_bar("AAA", 100.0)],
        latest_day="2026-09-04")

    row = built["rows"][0]
    assert row["validation_valid"] is True
    assert "stale_source_timestamp" not in row["validation_reasons"]


def test_a_week_old_bar_is_still_rejected(monkeypatch):
    """The bound is generous, not absent."""
    week_old = int((time.time() - 8 * 86400) * 1000)
    built = mws.build_market_wide_rows(
        [_bar("AAA", 150.0, ts_ms=week_old)], [_bar("AAA", 100.0)],
        latest_day="2026-09-04")

    assert "stale_source_timestamp" in built["rows"][0]["validation_reasons"]
    assert built["rows"][0]["validation_valid"] is False


# ------------------------------------------------------------- invariants --

def test_rows_are_advisory_and_never_trade_eligible():
    built = mws.build_market_wide_rows(
        [_bar("GPRO", 5.66)], [_bar("GPRO", 2.00)], latest_day="2026-09-04")

    assert all(r["advisory_only"] is True for r in built["rows"])
    assert all(r["decision_eligible"] is False for r in built["rows"])


def test_the_cycle_never_touches_the_symbol_universe(monkeypatch):
    import config.symbols as symbols

    before = tuple(symbols.OFFICIAL_WATCHLIST)
    monkeypatch.setattr(mws, "_store_rows", lambda rows: (len(rows), 0))
    mws.run_market_wide_cycle(fetcher=lambda day: (
        [_bar("GPRO", 5.66)], {"status": "available", "day": day}))

    assert tuple(symbols.OFFICIAL_WATCHLIST) == before


def test_a_missing_api_key_degrades_instead_of_raising(monkeypatch):
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)

    out = mws.run_market_wide_cycle()

    assert out["ok"] is False
    assert out["reason"] == "polygon_api_key_missing"


def test_a_provider_failure_stops_instead_of_walking_the_whole_lookback():
    """A dead provider must cost a fixed number of requests, not one per day of
    lookback on every cycle."""
    calls = []

    def fetcher(day):
        calls.append(day)
        return [], {"status": "unavailable", "reason": "provider_request_failed"}

    out = mws.run_market_wide_cycle(fetcher=fetcher)

    assert len(calls) == 1
    assert out["ok"] is False


def test_holidays_are_walked_past_without_being_treated_as_failures():
    """A non-trading day returns an empty result set, not an error."""
    empty_days, calls = 3, []

    def fetcher(day):
        calls.append(day)
        if len(calls) <= empty_days:
            return [], {"status": "available", "day": day, "trading_day": False}
        return [_bar("AAA", 100.0 + len(calls))], {"status": "available", "day": day}

    sessions, _ = mws._recent_trading_days(fetcher=fetcher)

    assert len(sessions) == 2
    assert len(calls) == empty_days + 2


def test_rows_are_written_over_one_connection(monkeypatch):
    """store_external_observation opens its own connection when passed no
    cursor; the naive loop would open several hundred per cycle."""
    opened = []

    class _Conn:
        def cursor(self):
            return "CUR"

        def __enter__(self):
            opened.append(1)
            return self

        def __exit__(self, *a):
            return False

    import core.db as db
    monkeypatch.setattr(db, "db_conn", lambda *a, **k: _Conn())
    monkeypatch.setattr(mws, "store_external_observation",
                        lambda row, cur=None: cur == "CUR")

    rows = [{"validation_valid": True, "symbol": f"S{i}"} for i in range(200)]
    inserted, invalid = mws._store_rows(rows)

    assert inserted == 200
    assert invalid == 0
    assert len(opened) == 1


def test_invalid_rows_are_counted_not_written(monkeypatch):
    monkeypatch.setattr(mws, "store_external_observation",
                        lambda row, cur=None: pytest.fail("must not write"))

    inserted, invalid = mws._store_rows(
        [{"validation_valid": False, "symbol": "BAD"}])

    assert (inserted, invalid) == (0, 1)


def test_job_registers_with_an_explicit_initial_delay():
    """register() defers by a full interval otherwise -- at a 6h interval that
    is the PR #178 trap on a smaller clock."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "wolf_app.py").read_text(encoding="utf-8")
    idx = src.index('"market_wide_snapshot",')

    assert "initial_delay_s=" in src[idx:idx + 400]


# ----------------------------------------------- permanent vs transient --

def test_a_rejected_key_is_reported_as_permanent_not_a_flake(monkeypatch):
    """A plan that excludes grouped-daily answers 403 forever. Under a circuit
    breaker that is indistinguishable from a flaky provider, which is how a
    lane stays 'temporarily' down for months."""
    def fetcher(day):
        return [], {"status": "unavailable", "reason": "provider_not_authorized",
                    "http_status": 403, "day": day, "permanent": True}

    out = mws.run_market_wide_cycle(fetcher=fetcher)

    assert out["ok"] is False
    assert out["reason"] == "provider_not_authorized"
    assert out["permanent_failure"] is True


def test_a_transient_failure_is_not_reported_as_permanent():
    def fetcher(day):
        return [], {"status": "unavailable", "reason": "provider_request_failed",
                    "day": day}

    out = mws.run_market_wide_cycle(fetcher=fetcher)

    assert out["permanent_failure"] is False
    assert out["reason"] == "insufficient_sessions"


def test_an_http_403_is_translated_before_the_breaker_hides_it(monkeypatch):
    class _Resp:
        status_code = 403

        def json(self):
            return {"status": "NOT_AUTHORIZED"}

        def raise_for_status(self):
            raise AssertionError("403 must be handled before raise_for_status")

    monkeypatch.setattr(mws.requests, "get", lambda *a, **k: _Resp())

    rows, status = mws.fetch_grouped_day("2026-09-04")

    assert rows == []
    assert status["reason"] == "provider_not_authorized"
    assert status["permanent"] is True


def test_a_permanent_rejection_is_not_overwritten_by_the_breaker(monkeypatch):
    """Confirmed live 2026-09-05: Polygon answers 403 for grouped-daily on this
    plan. After five cycles the circuit breaker opens, and every later cycle
    would report the generic provider_breaker_open -- turning "your plan does
    not cover this endpoint" back into "something is flaky"."""
    monkeypatch.setattr(mws, "_NOT_AUTHORIZED", {}, raising=False)

    class _Resp:
        status_code = 403

        def json(self):
            return {"status": "NOT_AUTHORIZED"}

        def raise_for_status(self):
            raise AssertionError("unreachable")

    monkeypatch.setattr(mws.requests, "get", lambda *a, **k: _Resp())
    mws.fetch_grouped_day("2026-09-04")

    # Now make the breaker refuse, as it would after repeated failures.
    import core.circuit_breaker as cb
    monkeypatch.setattr(cb._polygon_cb, "allow", lambda: False)

    _rows, status = mws.fetch_grouped_day("2026-09-05")

    assert status["reason"] == "provider_not_authorized"
    assert status["permanent"] is True
    mws._NOT_AUTHORIZED.clear()


def test_nothing_is_remembered_until_a_rejection_happens(monkeypatch):
    """The sticky flag must not be set by a timeout or a bad gateway."""
    monkeypatch.setattr(mws, "_NOT_AUTHORIZED", {}, raising=False)

    def boom(*a, **k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(mws.requests, "get", boom)
    mws.fetch_grouped_day("2026-09-04")

    assert mws._NOT_AUTHORIZED == {}
