"""The screener could not see a decline.

The alert lane ranks discoveries by ABSOLUTE move -- it was written to surface
crashes as well as runs. But the screener allowlist held only day_gainers and
most_shorted_stocks, so a large decline could reach it only if the name also
happened to be heavily shorted. The ranking had never once been shown a plain
crash, and the allowlist is a frozenset in code, so no configuration could add
one.

Confirmed live on 2026-09-05 after the alert lane was unblocked: the top alert
was BRNX at -26.3%, and it arrived through most_shorted_stocks rather than
through any screen that tracks declines.
"""
from __future__ import annotations

import core.external_screener_ingest as esi


def test_declines_have_a_screen_of_their_own():
    assert "day_losers" in esi._DEFAULT_SCREENS


def test_the_default_configuration_fetches_it():
    """The allowlist is necessary but not sufficient -- _screens() is what the
    cycle actually iterates, and it used to truncate to the first two."""
    assert "day_losers" in esi._screens()


def test_every_allowlisted_screen_survives_the_cap(monkeypatch):
    """Truncating to a fixed 2 is how a screen would appear configured and
    never be fetched -- a dead lane that reads as a live one."""
    monkeypatch.setenv("EXTERNAL_SCREENER_SCREENS", ",".join(esi._DEFAULT_SCREENS))

    assert set(esi._screens()) == set(esi._DEFAULT_SCREENS)


def test_unknown_screens_are_still_rejected(monkeypatch):
    """Widening the allowlist must not widen it to anything a caller names."""
    monkeypatch.setenv("EXTERNAL_SCREENER_SCREENS", "day_losers,undervalued_growth_stocks")

    assert esi._screens() == ("day_losers",)


def test_a_loser_row_keeps_its_negative_move():
    """A decline must reach the ledger as a negative number; the alert lane
    ranks on absolute value and would happily rank a sign error first."""
    payload = {"finance": {"result": [{"quotes": [{
        "symbol": "CRSH", "regularMarketPrice": 3.10,
        "regularMarketTime": 1_788_600_000,
        "regularMarketChangePercent": -41.2,
        "regularMarketVolume": 9_000_000,
        "averageDailyVolume3Month": 1_200_000,
    }]}]}}

    rows = esi.parse_yahoo_screen(payload, screen="day_losers",
                                  received_ts=1_788_600_100)

    assert len(rows) == 1
    assert rows[0]["symbol"] == "CRSH"
    assert rows[0]["move_pct"] == -41.2
    assert rows[0]["external_score"] == -41.2, "score must not be the short float"


def test_a_loser_row_is_still_advisory_only():
    payload = {"finance": {"result": [{"quotes": [{
        "symbol": "CRSH", "regularMarketPrice": 3.10,
        "regularMarketTime": 1_788_600_000,
        "regularMarketChangePercent": -41.2,
    }]}]}}

    row = esi.parse_yahoo_screen(payload, screen="day_losers",
                                 received_ts=1_788_600_100)[0]

    assert row["advisory_only"] is True
    assert row["decision_eligible"] is False


def test_the_cycle_requests_each_screen_once(monkeypatch):
    """One bounded request per screen, and no screen silently skipped."""
    asked = []

    def fake_fetch(screen, *, count=25, timeout_s=8.0):
        asked.append(screen)
        return [], {"status": "available", "rows": 0}

    monkeypatch.setattr(esi, "fetch_yahoo_screen", fake_fetch)
    monkeypatch.setattr(esi, "prune_external_context", lambda **kw: {}, raising=False)
    import core.external_context_ledger as ledger
    monkeypatch.setattr(ledger, "prune_external_context", lambda **kw: {})

    esi.run_external_screener_cycle()

    assert asked == list(esi._DEFAULT_SCREENS)


# --------------------------------------------- configured versus fetched --

def test_the_cycle_reports_each_screen_separately(monkeypatch):
    """A total alone cannot distinguish "day_losers was fetched and returned
    rows" from "day_losers was never requested". Verified on 2026-09-05: the
    live log read `status=complete inserted=50` and there was no way to tell
    which screens those 50 rows came from."""
    def fake_fetch(screen, *, count=25, timeout_s=8.0):
        if screen == "day_losers":
            return [], {"status": "unavailable", "reason": "provider_request_failed"}
        return [{"validation_valid": True, "in_official_watchlist": False}], \
            {"status": "available", "rows": 1}

    monkeypatch.setattr(esi, "fetch_yahoo_screen", fake_fetch)
    monkeypatch.setattr(esi, "store_external_observation", lambda row, **kw: True)
    import core.external_context_ledger as ledger
    monkeypatch.setattr(ledger, "prune_external_context", lambda **kw: {})

    out = esi.run_external_screener_cycle()

    assert out["status"] == "partial", "one dead screen must not read as complete"
    assert out["screens"]["day_losers"]["status"] == "unavailable"
    assert out["screens"]["day_gainers"]["status"] == "available"


def test_the_log_line_names_every_screen():
    """The per-screen state is computed either way; the bug was discarding it
    into two numbers before anything could read it."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "wolf_app.py").read_text(encoding="utf-8")
    idx = src.index('"external screener status=%s')
    window = src[idx - 200:idx + 900]

    assert 'result.get("screens")' in window, "log still reports totals only"
    assert "UNAVAILABLE" in window, "a dead screen must be named, not silently absent"
