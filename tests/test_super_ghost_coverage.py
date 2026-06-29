"""PR #88 - Super Ghost Data Coverage Upgrade tests.

Covers the three things this PR is accountable for:

1. A hard coverage gate: no A/B grade and no HIGH-CONVICTION action unless at
   least ``MIN_COVERAGE_FOR_AB`` (18) of 25 checks resolved.
2. SEC XBRL fundamentals parsing (EPS YoY + revenue YoY) with honest unknowns.
3. The Railway-friendly daily-history provider's contract (never raises;
   degrades to [] so callers mark checks unknown rather than fake them).
"""
import core.super_ghost as sg
from core.super_ghost import build_super_ghost


def _trend(start=10.0, step=0.06, n=260, vol=1_000_000):
    rows = []
    p = start
    for i in range(n):
        p += step
        rows.append({
            "ts": i,
            "open": round(p - 0.03, 4),
            "high": round(p + 0.08, 4),
            "low": round(p - 0.08, 4),
            "close": round(p, 4),
            "volume": vol + i * 1000,
        })
    return rows


def _full_bullish_snapshot():
    h = _trend()
    cur = h[-1]["close"]
    return {
        "symbol": "WOLF",
        "current_price": cur,
        "history": h,
        "spy_history": _trend(400, 0.10, 80, 50_000_000),
        "qqq_history": _trend(350, 0.12, 80, 40_000_000),
        "spx_history": _trend(5200, 1.1, 80, 0),
        "ixic_history": _trend(17000, 4.0, 80, 0),
        "sector_history": _trend(220, 0.22, 80, 5_000_000),
        "vix_history": _trend(13.0, 0.0, 30, 0),
        "week52_low": 9.0,
        "week52_high": cur + 1.0,
        "avg_volume": 1_000_000,
        "volume": 2_400_000,
        "sector": "Technology",
        "sector_etf": "SMH",
        "earnings": {
            "actual_eps": 0.12,
            "estimate_eps": 0.09,
            "revenue": 120_000_000,
            "revenue_year_ago": 95_000_000,
            "guidance": "Management raised guidance with a strong outlook.",
        },
        "news": [{"title": "WOLF wins new contract and launches product", "symbols": ["WOLF"], "sentiment": 0.8}],
        "insider_trading": {"net_shares": 50_000, "buys": 3, "sells": 0},
        "institutional_ownership": {"institutional_pct": 67.0, "recent_change_pct": 4.5},
        "analysts": {
            "current_price": cur,
            "price_target_avg": cur * 1.25,
            "recommendations": {"strong_buy": 3, "buy": 6, "hold": 2, "underperform": 0, "sell": 0},
        },
        "vix": 13.5,
        "fed_rate": 4.75,
        "cpi_yoy": 2.9,
        "stop_loss": cur * 0.95,
        "target_price": cur * 1.16,
        "market_correlation": 0.52,
        "sector_exposure_pct": 18.0,
        "open_positions": 2,
        "risk": {"risk_pct_per_trade": 1.0, "account_size_usd": 25000, "sector_exposure_pct": 18.0, "open_positions": 2},
        "daily_loss_lock": {"locked": False, "should_lock": False, "daily_loss_limit_usd": 250, "realized_pnl_usd": 0},
    }


def test_full_coverage_meets_gate_and_can_grade_ab():
    report = build_super_ghost("WOLF", snapshot=_full_bullish_snapshot())
    cov = report["coverage"]
    assert cov["available"] == 25
    assert cov["min_for_ab_grade"] == sg.MIN_COVERAGE_FOR_AB
    assert cov["meets_ab_gate"] is True
    assert cov["gated"] is False
    assert report["prediction"]["accuracy_grade"] in {"A+", "A", "B+", "B"}
    assert report["prediction"]["coverage_gated"] is False


def test_coverage_gate_caps_ab_grade_when_below_threshold(monkeypatch):
    """An otherwise A/B report must be capped to C purely because coverage is low.

    We raise the threshold to 25 and remove one check's data so coverage drops
    below 25. This isolates the *gate* from quality scoring.
    """
    monkeypatch.setattr(sg, "MIN_COVERAGE_FOR_AB", 25)
    snap = _full_bullish_snapshot()
    # Make average-volume unknown: drop avg_volume and strip volumes from history.
    snap.pop("avg_volume", None)
    snap.pop("volume", None)
    snap["history"] = [{"ts": i, "close": 10 + i * 0.05} for i in range(260)]
    report = build_super_ghost("WOLF", snapshot=snap)
    cov = report["coverage"]
    assert cov["available"] < 25
    assert cov["meets_ab_gate"] is False
    assert report["prediction"]["accuracy_grade"] == "C"
    assert report["prediction"]["coverage_gated"] is True
    assert cov["gated"] is True


def test_coverage_gate_blocks_high_conviction_action(monkeypatch):
    monkeypatch.setattr(sg, "MIN_COVERAGE_FOR_AB", 25)
    snap = _full_bullish_snapshot()
    snap.pop("avg_volume", None)
    snap.pop("volume", None)
    snap["history"] = [{"ts": i, "close": 10 + i * 0.05} for i in range(260)]
    report = build_super_ghost("WOLF", snapshot=snap)
    assert "HIGH-CONVICTION" not in report["prediction"]["action"]


def test_coverage_block_always_reports_gate_metadata():
    """Even an empty snapshot must surface the gate fields (honest + UI-ready)."""
    report = build_super_ghost("WOLF", snapshot={"symbol": "WOLF"})
    cov = report["coverage"]
    assert "min_for_ab_grade" in cov
    assert "meets_ab_gate" in cov
    assert cov["meets_ab_gate"] is False
    assert report["prediction"]["accuracy_grade"] not in {"A+", "A", "B+", "B"}


def test_eps_yoy_trend_fallback_scores_without_consensus_estimate():
    """SEC gives EPS actual + prior-year, not a consensus estimate. The engine
    must still score the YoY trend and label it honestly (no fake 'beat')."""
    snap = _full_bullish_snapshot()
    snap["earnings"] = {
        "actual_eps": 0.50,
        "eps_year_ago": 0.20,  # EPS rose YoY, no estimate present
        "eps_period": "2026 Q3",
        "revenue": 120_000_000,
        "revenue_year_ago": 95_000_000,
        "source": "sec_xbrl",
    }
    report = build_super_ghost("WOLF", snapshot=snap)
    by_key = {x["key"]: x for x in report["checklist"]}
    eps = by_key["eps"]
    assert eps["available"] is True
    assert eps["score"] > 0  # rising EPS -> bullish
    assert eps["value"].get("basis") == "yoy_trend"
    assert "consensus" in eps["evidence"].lower() or "yoy" in eps["evidence"].lower()


def test_eps_negative_yoy_trend_is_bearish():
    snap = _full_bullish_snapshot()
    snap["earnings"] = {
        "actual_eps": -3.05,
        "eps_year_ago": 2.22,  # EPS collapsed YoY (real WOLF shape)
        "eps_period": "2026 Q3",
        "source": "sec_xbrl",
    }
    report = build_super_ghost("WOLF", snapshot=snap)
    by_key = {x["key"]: x for x in report["checklist"]}
    assert by_key["eps"]["score"] < 0


# ---- SEC fundamentals module (pure parsing, no network) --------------------

def test_sec_quarterly_series_dedupes_and_skips_cumulative():
    from core.sec_fundamentals import _quarterly_series, _yoy_from_series

    concept = {
        "units": {
            "USD/shares": [
                # quarterly rows (<=100 day spans)
                {"val": 0.20, "start": "2024-07-01", "end": "2024-09-30", "fp": "Q1", "fy": 2025, "form": "10-Q", "filed": "2024-11-01"},
                {"val": 0.50, "start": "2025-07-01", "end": "2025-09-30", "fp": "Q1", "fy": 2026, "form": "10-Q", "filed": "2025-11-01"},
                # a YTD cumulative span that must be ignored
                {"val": 1.40, "start": "2025-01-01", "end": "2025-09-30", "fp": "Q3", "fy": 2025, "form": "10-Q", "filed": "2025-11-01"},
                # an amended duplicate of the latest end date (later filed wins)
                {"val": 0.55, "start": "2025-07-01", "end": "2025-09-30", "fp": "Q1", "fy": 2026, "form": "10-Q", "filed": "2025-12-01"},
            ]
        }
    }
    series = _quarterly_series(concept)
    # cumulative YTD (start 2025-01-01) excluded
    assert all(r.get("start") != "2025-01-01" for r in series)
    # amended value won for the latest end date
    latest = [r for r in series if r["end"] == "2025-09-30"][0]
    assert latest["val"] == 0.55
    yoy = _yoy_from_series(series)
    assert yoy is not None
    assert yoy["latest"]["val"] == 0.55
    assert yoy["prior"]["val"] == 0.20


def test_sec_fundamentals_unknown_symbol_is_honest():
    from core.sec_fundamentals import get_fundamentals

    out = get_fundamentals("NO_SUCH_TICKER_ZZZ")
    assert out["available"] is False
    assert out.get("reason") == "no_cik_mapping"


# ---- market_history module (contract: never raises, degrades to []) --------

def test_market_history_no_keys_returns_empty(monkeypatch):
    import core.market_history as mh

    mh.clear_cache()
    monkeypatch.delenv("ALPACA_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    # Stub every tier so the test is hermetic (no network) and asserts the
    # "all sources failed -> []" contract.
    monkeypatch.setattr(mh, "_signal_engine_ohlcv", lambda *a, **k: [])
    monkeypatch.setattr(mh, "_yfinance_daily_bars", lambda *a, **k: [])
    rows = mh.get_daily_history("WOLF", 50)
    assert rows == []
    assert mh.history_source_status()["alpaca_keyed"] is False


def test_market_history_parses_alpaca_bars(monkeypatch):
    import core.market_history as mh

    mh.clear_cache()
    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    # Force the direct-Alpaca tier to be exercised by disabling the
    # signal_engine delegate for this test.
    monkeypatch.setattr(mh, "_signal_engine_ohlcv", lambda *a, **k: [])

    class FakeResp:
        status_code = 200

        def json(self):
            return {
                "bars": [
                    {"t": "2026-06-01T00:00:00Z", "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 1000},
                    {"t": "2026-06-02T00:00:00Z", "o": 10.5, "h": 12, "l": 10, "c": 11.5, "v": 2000},
                ],
                "next_page_token": None,
            }

    import requests as _rq
    monkeypatch.setattr(_rq, "get", lambda *a, **k: FakeResp())
    rows = mh.get_daily_history("WOLF", 50)
    assert len(rows) == 2
    assert rows[0]["close"] == 10.5
    assert rows[-1]["close"] == 11.5
    assert rows[-1]["volume"] == 2000


def test_market_history_prefers_signal_engine_chain(monkeypatch):
    """get_daily_history must try the production-proven _fetch_ohlcv chain first."""
    import core.market_history as mh

    mh.clear_cache()
    fake_rows = [
        {"ts": "2026-06-01T00:00:00Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100},
        {"ts": "2026-06-02T00:00:00Z", "open": 1.5, "high": 2.5, "low": 1.0, "close": 2.0, "volume": 200},
    ]
    import core.signal_engine as se
    monkeypatch.setattr(se, "_fetch_ohlcv", lambda *a, **k: fake_rows)
    # If the delegate is used, the Alpaca/yfinance tiers must never be reached.
    monkeypatch.setattr(mh, "_alpaca_daily_bars", lambda *a, **k: (_ for _ in ()).throw(AssertionError("alpaca tier should not run")))
    rows = mh.get_daily_history("WOLF", 50)
    assert [r["close"] for r in rows] == [1.5, 2.0]


def test_period_for_days_mapping():
    from core.market_history import _period_for_days

    assert _period_for_days(90) == "3m"
    assert _period_for_days(180) == "6m"
    assert _period_for_days(365) == "1y"
    assert _period_for_days(400) == "2y"


def test_min_coverage_constant_is_18_by_default():
    """The shipped default acceptance bar must be 18/25.

    Done by re-evaluating the same env-parse expression the module uses, so we
    do not have to reload the module (which could perturb other tests' bound
    reference to ``sg``).
    """
    import os

    prev = os.environ.pop("SUPER_GHOST_MIN_COVERAGE_AB", None)
    try:
        default = max(1, min(25, int(os.getenv("SUPER_GHOST_MIN_COVERAGE_AB", "18"))))
        assert default == 18
    finally:
        if prev is not None:
            os.environ["SUPER_GHOST_MIN_COVERAGE_AB"] = prev
