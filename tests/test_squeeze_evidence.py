"""End-to-end scanner data failures must not masquerade as a quiet market."""
import asyncio
from datetime import datetime, timedelta, timezone
import time

import pytest

import core.squeeze_monitor as sm

_REAL_SYNC_FETCH_METRICS = sm._sync_fetch_metrics
_REAL_SINGLE_SNAPSHOT = sm._single_market_snapshot


class Response:
    def __init__(self, status, bars=None, token=None):
        self.status_code = status
        self.body = {"bars": bars or {}, "next_page_token": token}

    def json(self):
        return self.body


@pytest.fixture
def scan(monkeypatch):
    monkeypatch.setattr("core.market_hours.is_us_extended_hours", lambda *a: True)
    monkeypatch.setattr("core.market_hours.is_us_premarket", lambda *a: False)
    monkeypatch.setattr("core.market_hours.is_us_rth", lambda *a: True)
    # Match the simulated RTH session instead of using the wall-clock pace.
    monkeypatch.setattr(sm, "rth_elapsed_fraction", lambda *a: 0.25)
    monkeypatch.setattr("config.symbols.get_edge_set", lambda: {"SPCE"})
    monkeypatch.setattr(sm, "_alpaca_headers", lambda: {"test": "not-a-secret"})
    monkeypatch.setattr("core.prices._alpaca_bar_feeds", lambda: ("iex",))
    monkeypatch.setattr("core.prices._note_alpaca_feed_status", lambda *a: None)
    monkeypatch.setattr(sm, "_batch_bars", {})
    monkeypatch.setattr(sm, "_persist_scan_report", lambda *a: None)
    monkeypatch.setattr(sm, "_reset_alert_history_if_new_session", lambda: None)
    monkeypatch.setattr(sm, "_enrich_watches_with_quorum", lambda *a: None)
    monkeypatch.setattr(sm, "_cached_short_context", lambda *a: {})
    monkeypatch.setattr(sm, "_single_market_snapshot", lambda *a: pytest.fail("batch failure caused per-symbol storm"))
    monkeypatch.setattr(sm, "_maybe_alert", lambda *a: pytest.fail("invalid quote alerted"))
    monkeypatch.setattr("core.squeeze_outcomes.record_squeeze_prediction", lambda *a, **kw: pytest.fail("invalid quote persisted"))
    from core.daily_bar_contract import previous_session
    now = datetime.now(timezone.utc)
    prior = previous_session(now.date()).isoformat()
    daily = {"SPCE": [{"t": prior + "T04:00:00Z", "c": 100, "v": 1000}]}
    bar = {"t": (now - timedelta(minutes=3)).isoformat(), "c": 120, "h": 120, "l": 110, "v": 1000}
    return daily, {"SPCE": [bar]}


def run_scan():
    asyncio.run(sm._run_watchlist_scan())
    return sm._last_scan_report


def test_intraday_429_is_failure_not_no_print(monkeypatch, scan):
    daily, _ = scan
    monkeypatch.setattr("requests.get", lambda url, **kw: Response(200, daily) if "1Day" in url else Response(429))
    report = run_scan()
    assert report["fetch_fail"] == 1
    assert report["no_intraday_print"] == 0
    assert report["candidates"] == []


def test_missing_prior_baseline_is_not_no_print(monkeypatch, scan):
    _, intraday = scan
    monkeypatch.setattr("requests.get", lambda url, **kw: Response(200, {} if "1Day" in url else intraday))
    report = run_scan()
    assert report["no_intraday_print"] == 0
    assert report["invalid_baseline"] == 1


def test_stale_same_day_bar_cannot_reach_signal_or_ledger(monkeypatch, scan):
    daily, intraday = scan
    intraday["SPCE"][0]["t"] = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    monkeypatch.setattr("requests.get", lambda url, **kw: Response(200, daily if "1Day" in url else intraday))
    monkeypatch.setattr(sm, "evaluate_squeeze_signal", lambda *a, **kw: pytest.fail("stale bar reached signal"))
    report = run_scan()
    assert report["fetch_ok"] == 0
    assert report["stale_quote"] == 1
    assert report["candidates"] == []


def test_batch_success_empty_is_no_print_not_failure(monkeypatch, scan):
    monkeypatch.setattr("requests.get", lambda *a, **kw: Response(200))
    report = run_scan()
    assert report["no_intraday_print"] == 1
    assert report["fetch_fail"] == 0


def test_scan_age_not_market_open_controls_staleness(monkeypatch):
    monkeypatch.setattr(sm, "_last_scan_report", {"ts": time.time() - 3600, "status": "complete", "ok": True})
    monkeypatch.setattr("core.market_hours.is_us_extended_hours", lambda *a: True)
    monkeypatch.setattr(sm, "_ensure_scan_cache_loaded", lambda: None)
    result = sm.get_squeeze_picks()
    assert result["snapshot_stale"] is True
    assert result["scan_ok"] is False


def test_candidate_rejects_missing_quote_clock():
    with pytest.raises(ValueError, match="evidence"):
        sm.candidate_to_pick("SPCE", "squeeze_active", {
            "price": 120, "prior_close": 100, "session_high": 120,
            "current_move_pct": 20, "peak_move_pct": 20,
        }, 4, {})


def test_fresh_complete_bars_reach_candidate_with_provenance(monkeypatch, scan):
    daily, intraday = scan
    monkeypatch.setattr("requests.get", lambda url, **kw: Response(200, daily if "1Day" in url else intraday))
    issued = []
    monkeypatch.setattr("core.squeeze_outcomes.record_squeeze_prediction", lambda pick, **kw: issued.append(pick))
    monkeypatch.setattr("core.explosion_benchmark.record_observation", lambda *a, **kw: None)
    monkeypatch.setattr(sm, "_maybe_alert", lambda *a: False)
    report = run_scan()
    assert report["fetch_ok"] == 1
    assert len(issued) == 1
    assert issued[0]["price_as_of_ts"] is not None
    assert issued[0]["price_feed"] == "iex"
    assert issued[0]["market_data_contract"] == "squeeze_bar_evidence_v1"
    assert issued[0]["price_timestamp_basis"] == "5Min_bar_start"
    assert issued[0]["bars_complete"] is True


@pytest.mark.parametrize("bad_time", [None, "2099-01-01T13:30:00Z", "not-a-clock"])
def test_unknown_or_future_bars_cannot_be_issued(monkeypatch, scan, bad_time):
    daily, intraday = scan
    intraday["SPCE"][0]["t"] = bad_time
    monkeypatch.setattr("requests.get", lambda url, **kw: Response(200, daily if "1Day" in url else intraday))
    report = run_scan()
    assert report["fetch_ok"] == 0
    assert report["no_intraday_print"] == 0
    assert report["candidates"] == []


def test_failed_second_page_cannot_prove_no_print(monkeypatch, scan):
    daily, _ = scan
    def get(url, **kw):
        if "1Day" in url:
            return Response(200, daily)
        if "page_token=" in url:
            return Response(500)
        return Response(200, {"AAPL": [{"v": 100}]}, "second")
    monkeypatch.setattr("requests.get", get)
    report = run_scan()
    assert report["fetch_fail"] == 1
    assert report["no_intraday_print"] == 0
    assert report["data_status_by_symbol"]["SPCE"]["intraday"]["complete"] is False


def test_mixed_feed_volumes_are_not_comparable(monkeypatch, scan):
    daily, intraday = scan
    from core.squeeze_evidence import BarFetch
    monkeypatch.setattr(sm, "_alpaca_multi_bars", lambda syms, **kw: BarFetch(
        daily if kw["timeframe"] == "1Day" else intraday, complete=True,
        feed="sip" if kw["timeframe"] == "1Day" else "iex"))
    report = run_scan()
    assert report["fetch_ok"] == 0
    assert report["invalid_baseline"] == 1


def test_rate_limit_stops_remaining_chunks_and_fallback(monkeypatch, scan):
    monkeypatch.setattr("config.symbols.get_edge_set", lambda: {"SPCE", "AAPL", "MSFT"})
    monkeypatch.setattr(sm, "_BATCH_SYMBOLS_PER_REQ", 1)
    calls = []
    monkeypatch.setattr("requests.get", lambda url, **kw: (calls.append(url), Response(429))[1])
    report = run_scan()
    assert len(calls) == 1
    assert report["fetch_fail"] == 1
    assert report["fetch_skipped"] == 2
    assert report["no_intraday_print"] == 0


def test_repeated_page_token_is_bounded_failure(monkeypatch, scan):
    calls = []
    monkeypatch.setattr("requests.get", lambda url, **kw: (calls.append(url), Response(200, {"SPCE": [{"v": 1}]}, "repeat"))[1])
    result = sm._alpaca_multi_bars(["SPCE"], timeframe="5Min", start="s", end="e")
    assert len(calls) == 2
    assert result.complete is False
    assert result.reason == "repeated_page_token"
    assert result.bars == {}


def test_expired_deadline_makes_no_request(monkeypatch, scan):
    monkeypatch.setattr("requests.get", lambda *a, **kw: pytest.fail("past deadline"))
    result = sm._alpaca_multi_bars(["SPCE"], timeframe="5Min", start="s", end="e", deadline=time.monotonic() - 1)
    assert result.reason == "deadline_exceeded"


def test_batch_worker_does_not_block_loop_or_duplicate_after_timeout(monkeypatch):
    import threading
    from core.squeeze_evidence import MarketSnapshot
    started, release = threading.Event(), threading.Event()
    calls = []
    def blocked(symbols):
        calls.append(symbols)
        started.set()
        release.wait(2)
        return MarketSnapshot()
    monkeypatch.setattr(sm, "batched_market_snapshot", blocked)
    monkeypatch.setattr(sm, "_batch_worker_future", None)
    monkeypatch.setattr(sm, "_BATCH_DEADLINE_S", -1.97)  # 30ms observation wait
    async def check():
        first = asyncio.create_task(sm._async_market_snapshot(["SPCE"]))
        await asyncio.sleep(0.01)
        assert started.is_set() and not first.done()  # loop remained responsive
        result = await first
        assert result.statuses["SPCE"]["reason"] == "batch_timeout"
        original = sm._batch_worker_future
        again = await sm._async_market_snapshot(["SPCE"])
        assert again.statuses["SPCE"]["reason"] == "batch_still_running"
        assert sm._batch_worker_future is original
        assert len(calls) == 1
    try:
        asyncio.run(check())
    finally:
        release.set()
        sm._batch_worker_future.result(timeout=2)


@pytest.mark.parametrize("field,value", [("c", float("nan")), ("h", float("inf")), ("v", -1)])
def test_invalid_raw_bar_values_cannot_generate_candidates(monkeypatch, scan, field, value):
    daily, intraday = scan
    intraday["SPCE"][0][field] = value
    monkeypatch.setattr("requests.get", lambda url, **kw: Response(200, daily if "1Day" in url else intraday))
    report = run_scan()
    assert report["fetch_ok"] == 0
    assert report["no_intraday_print"] == 0


def test_single_symbol_fallback_uses_identical_clock_and_baseline(monkeypatch, scan):
    daily, intraday = scan
    monkeypatch.setattr(sm, "SQUEEZE_BATCH_BARS", False)
    monkeypatch.setattr("requests.get", lambda url, **kw: Response(200, daily if "1Day" in url else intraday))
    monkeypatch.setattr(sm, "_sync_fetch_metrics", _REAL_SYNC_FETCH_METRICS)
    monkeypatch.setattr(sm, "_single_market_snapshot", _REAL_SINGLE_SNAPSHOT)
    monkeypatch.setattr("core.prices.get_intraday_session", lambda *a: pytest.fail("unverified mixed quote path"))
    metrics = sm._sync_fetch_metrics("SPCE")
    assert metrics["price"] == 120
    assert metrics["price_as_of_ts"] > 0
    assert metrics["reference_session_date"] == daily["SPCE"][0]["t"][:10]
    assert metrics["daily_feed"] == metrics["intraday_feed"] == "iex"


def test_persisted_scan_retains_rejection_reasons_and_contract(monkeypatch, tmp_path):
    import json
    monkeypatch.setattr(sm, "_scan_cache_path", str(tmp_path / "scan.json"))
    report = {"status": "complete", "ts": 123, "invalid_baseline": 2,
              "stale_quote": 1, "invalid_quote": 3, "data_contract": "squeeze_bar_evidence_v1",
              "data_status_by_symbol": {"SPCE": {"status": "stale_quote", "price_as_of_ts": 1}}}
    sm._persist_scan_report(report)
    saved = json.loads((tmp_path / "scan.json").read_text())
    for key, value in report.items():
        assert saved[key] == value


def test_disabled_batch_fallback_preserves_successful_empty_status(monkeypatch, scan):
    monkeypatch.setattr(sm, "SQUEEZE_BATCH_BARS", False)
    monkeypatch.setattr(sm, "_single_market_snapshot", _REAL_SINGLE_SNAPSHOT)
    monkeypatch.setenv("SQUEEZE_FETCH_DELAY_S", "0")
    monkeypatch.setattr("requests.get", lambda *a, **kw: Response(200))
    report = run_scan()
    assert report["no_intraday_print"] == 1
    assert report["fetch_fail"] == 0
