"""Ex-dividend awareness: a dividend is not a crash.

Frontline (FRO) detached $3.41/share on 2026-09-18. On a ~$60 stock that is a
>5% step down measured against an unadjusted previous close, and the discovery
lane -- which ranks by ABSOLUTE move and had no concept of a corporate action --
was structurally certain to report it as a selloff. A grep for `ex_dividend`,
`exDividend`, `dividendDate` or `corporate_action` across core/ returned nothing:
the entire category was invisible.

These tests pin the four properties that make the fix safe:

  * the observed move_pct is NEVER rewritten -- the tape said what it said
  * an explained decline is RECLASSIFIED, never deleted
  * "the feed is down" and "there was no dividend" stay different answers
  * a real crash on a dividend-paying day is still a crash
"""
from __future__ import annotations

import pytest

import core.corporate_actions as ca


@pytest.fixture(autouse=True)
def _clean_module_state(monkeypatch):
    ca._CACHE.clear()
    ca._NOT_AUTHORIZED.clear()
    monkeypatch.setenv("CORPORATE_ACTIONS_ENABLED", "1")
    monkeypatch.setenv("POLYGON_API_KEY", "test-key")
    from core.circuit_breaker import _polygon_corp_actions_cb
    _polygon_corp_actions_cb.reset()
    yield
    ca._CACHE.clear()
    ca._NOT_AUTHORIZED.clear()


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _stub_polygon(monkeypatch, payload, status_code=200, calls=None):
    def _get(url, params=None, headers=None, timeout=None):
        if calls is not None:
            calls.append({"url": url, "params": dict(params or {})})
        return _Response(payload, status_code)
    monkeypatch.setattr(ca.requests, "get", _get)


# ------------------------------------------------------------- the FRO case --

def test_an_ex_dividend_drop_is_labelled_not_reported_as_a_crash(monkeypatch):
    """$3.41 off a $63.41 close is -5.38%. Nobody sold."""
    _stub_polygon(monkeypatch, {"results": [
        {"ticker": "FRO", "cash_amount": 3.41, "currency": "USD",
         "ex_dividend_date": "2026-09-18", "dividend_type": "CD", "frequency": 4},
    ]})
    # 09:35 America/New_York on 2026-09-18, the ex-date session.
    out = ca.annotate_move("FRO", move_pct=-5.38, price=60.0, observed_ts=1789738500)

    assert out["corporate_action_coverage"] == "matched"
    assert out["corporate_action"]["kind"] == "ex_dividend"
    assert out["corporate_action"]["cash_amount"] == pytest.approx(3.41)
    # prev close = 60.0 / (1 - 0.0538) = 63.41; 3.41/63.41 = 5.378%
    assert out["mechanical_move_pct"] == pytest.approx(-5.378, abs=0.01)
    # The economic move is ~zero: the holder is exactly as wealthy as yesterday.
    assert out["economic_move_pct"] == pytest.approx(0.0, abs=0.02)


def test_a_real_crash_on_a_dividend_day_is_still_a_crash(monkeypatch):
    """The adjustment removes the dividend, not the selloff."""
    _stub_polygon(monkeypatch, {"results": [
        {"ticker": "FRO", "cash_amount": 3.41, "currency": "USD",
         "ex_dividend_date": "2026-09-18"},
    ]})
    out = ca.annotate_move("FRO", move_pct=-22.0, price=49.45, observed_ts=1789738500)

    assert out["corporate_action_coverage"] == "matched"
    # ~-22% observed, ~5.4% of it mechanical, so ~-16.6% of genuine decline
    # remains and this row keeps every right to be on the alert list.
    assert out["economic_move_pct"] < -16.0
    assert out["economic_move_pct"] > -17.5


def test_the_observed_move_is_never_rewritten(monkeypatch):
    """annotate_move ADDS a number. It has no authority to change the tape."""
    _stub_polygon(monkeypatch, {"results": [
        {"ticker": "FRO", "cash_amount": 3.41, "currency": "USD",
         "ex_dividend_date": "2026-09-18"},
    ]})
    out = ca.annotate_move("FRO", move_pct=-5.38, price=60.0, observed_ts=1789738500)

    assert "move_pct" not in out
    assert set(out) == {
        "corporate_action", "mechanical_move_pct", "economic_move_pct",
        "corporate_action_coverage", "session_date",
    }


# -------------------------------------------------- unknown stays unknown --

def test_a_dead_feed_is_not_reported_as_no_dividend(monkeypatch):
    """The single most dangerous confusion this module could make.

    If "provider down" collapsed into "no corporate action", every ex-dividend
    drop would silently return to being a crash the moment the feed blinked --
    and the output would look identical to a healthy day.
    """
    def _boom(*a, **kw):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(ca.requests, "get", _boom)

    out = ca.annotate_move("FRO", move_pct=-5.38, price=60.0, observed_ts=1789738500)

    assert out["corporate_action_coverage"] == "provider_request_failed"
    assert out["economic_move_pct"] is None   # NOT -5.38, and NOT 0.0
    assert out["corporate_action"] is None


def test_no_dividend_on_a_healthy_feed_reports_the_observed_move(monkeypatch):
    """Known-good feed plus no distribution: the observed move IS the economic one."""
    _stub_polygon(monkeypatch, {"results": []})

    out = ca.annotate_move("NVDA", move_pct=-7.25, price=170.0, observed_ts=1789738500)

    assert out["corporate_action_coverage"] == "no_action"
    assert out["economic_move_pct"] == pytest.approx(-7.25)
    assert out["corporate_action"] is None


def test_missing_api_key_yields_no_adjustment(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "")
    out = ca.annotate_move("FRO", move_pct=-5.38, price=60.0, observed_ts=1789738500)
    assert out["corporate_action_coverage"] == "no_api_key"
    assert out["economic_move_pct"] is None


def test_a_403_is_a_sticky_plan_verdict_not_a_flaky_provider(monkeypatch):
    """Same lesson as market_wide_snapshot: a plan refusal must not decay into
    the breaker's generic 'unavailable' after five cycles."""
    calls = []
    _stub_polygon(monkeypatch, {}, status_code=403, calls=calls)

    first = ca.ex_dividends_on("2026-09-18")
    ca._CACHE.clear()                      # defeat the cache, not the memory
    second = ca.ex_dividends_on("2026-09-19")

    assert first[1] == "provider_not_authorized"
    assert second[1] == "provider_not_authorized"
    assert ca._NOT_AUTHORIZED["status_code"] == 403
    # The second day never reached the wire: the diagnosis is remembered.
    assert len(calls) == 1


def test_a_403_does_not_spend_the_breaker_budget(monkeypatch):
    from core.circuit_breaker import _polygon_corp_actions_cb
    _stub_polygon(monkeypatch, {}, status_code=403)
    ca.ex_dividends_on("2026-09-18")
    assert _polygon_corp_actions_cb.state == "closed"


# ----------------------------------------------------------- matching rules --

def test_a_dividend_only_applies_on_its_own_ex_date(monkeypatch):
    """The step down happens once, against one close. The next session is normal."""
    def _get(url, params=None, headers=None, timeout=None):
        day = (params or {}).get("ex_dividend_date")
        rows = [{"ticker": "FRO", "cash_amount": 3.41, "currency": "USD",
                 "ex_dividend_date": "2026-09-18"}] if day == "2026-09-18" else []
        return _Response({"results": rows})
    monkeypatch.setattr(ca.requests, "get", _get)

    # 09:35 ET the following Monday -- no longer the ex-date.
    out = ca.annotate_move("FRO", move_pct=-5.38, price=60.0, observed_ts=1789997700)

    assert out["session_date"] == "2026-09-21"
    assert out["corporate_action_coverage"] == "no_action"
    assert out["economic_move_pct"] == pytest.approx(-5.38)


def test_ex_dates_resolve_in_the_exchange_timezone():
    """Ex-dividend dates are exchange dates. SESSION_TZ is America/Chicago and
    agrees only by luck; a premarket 04:10 ET observation must not be dated to
    the previous day."""
    assert ca.session_date(1789718400) == "2026-09-18"   # 04:00 ET, premarket
    assert ca.session_date(1789776000) == "2026-09-18"   # 20:00 ET = 00:00 UTC on the 19th
    assert ca.session_date(0) is None
    assert ca.session_date("not a timestamp") is None


def test_a_non_usd_distribution_is_ignored(monkeypatch):
    """A CAD amount subtracted from a USD price is a fabricated number."""
    _stub_polygon(monkeypatch, {"results": [
        {"ticker": "FRO", "cash_amount": 3.41, "currency": "CAD",
         "ex_dividend_date": "2026-09-18"},
    ]})
    out = ca.annotate_move("FRO", move_pct=-5.38, price=60.0, observed_ts=1789738500)
    assert out["corporate_action_coverage"] == "no_action"


def test_the_largest_distribution_wins_and_the_count_is_kept(monkeypatch):
    """A regular quarterly plus a special on one ex-date are two events. Summing
    them into one unnamed number would hide that."""
    _stub_polygon(monkeypatch, {"results": [
        {"ticker": "FRO", "cash_amount": 0.20, "currency": "USD",
         "ex_dividend_date": "2026-09-18", "dividend_type": "CD"},
        {"ticker": "FRO", "cash_amount": 3.41, "currency": "USD",
         "ex_dividend_date": "2026-09-18", "dividend_type": "SC"},
    ]})
    actions, status = ca.ex_dividends_on("2026-09-18")
    assert status == "available"
    assert actions["FRO"]["cash_amount"] == pytest.approx(3.41)
    assert actions["FRO"]["distributions"] == 2


def test_an_unrecoverable_previous_close_reports_the_action_without_a_number(monkeypatch):
    """A -100% move has no recoverable base. Say so rather than inventing one."""
    _stub_polygon(monkeypatch, {"results": [
        {"ticker": "FRO", "cash_amount": 3.41, "currency": "USD",
         "ex_dividend_date": "2026-09-18"},
    ]})
    out = ca.annotate_move("FRO", move_pct=-100.0, price=0.0, observed_ts=1789738500)
    assert out["corporate_action_coverage"] == "matched_unpriced"
    assert out["corporate_action"] is not None
    assert out["economic_move_pct"] is None


def test_previous_close_recovery():
    assert ca.previous_close_from(60.0, -5.38) == pytest.approx(63.413, abs=0.01)
    assert ca.previous_close_from(110.0, 10.0) == pytest.approx(100.0)
    assert ca.previous_close_from(0.0, -100.0) is None
    assert ca.previous_close_from(None, -5.0) is None
    assert ca.previous_close_from(10.0, float("nan")) is None


def test_results_are_cached_per_day(monkeypatch):
    calls = []
    _stub_polygon(monkeypatch, {"results": []}, calls=calls)
    ca.ex_dividends_on("2026-09-18")
    ca.ex_dividends_on("2026-09-18")
    ca.ex_dividends_on("2026-09-18")
    assert len(calls) == 1


def test_disabled_makes_no_request(monkeypatch):
    monkeypatch.setenv("CORPORATE_ACTIONS_ENABLED", "0")
    def _boom(*a, **kw):
        raise AssertionError("no request may be made while disabled")
    monkeypatch.setattr(ca.requests, "get", _boom)
    assert ca.ex_dividends_on("2026-09-18") == ({}, "disabled")
