"""Audit 2026-09-25, data and signal-input findings (U01, U02, U03, U14, U19,
U24, U28, U49, U57, U68).

Each test pins the corrected behavior against the failure the audit observed.
"""
from __future__ import annotations

import datetime as _dt
import time
from types import SimpleNamespace

import pytest


# ── U01: squeeze lifecycle stages 6-7 are reachable ─────────────────────────

def test_lifecycle_flags_from_peak_current_and_rvol():
    from core.squeeze_hunter import lifecycle_flags

    faded = lifecycle_flags(peak_move_pct=80.0, current_move_pct=30.0, rvol=6.0)
    assert faded == {"momentum_declining": True, "price_parabolic": True, "huge_volume": True}
    holding = lifecycle_flags(peak_move_pct=80.0, current_move_pct=75.0, rvol=6.0)
    assert holding["momentum_declining"] is False
    # A +3% blip that went red is not a squeeze fading: no run, no fade.
    assert lifecycle_flags(peak_move_pct=3.0, current_move_pct=-0.5, rvol=2.0)[
        "momentum_declining"] is False
    # Missing inputs can never set a flag.
    assert lifecycle_flags(None, None, None) == {
        "momentum_declining": False, "price_parabolic": False, "huge_volume": False,
    }


def test_classify_stage_emits_exhaustion_and_reversal():
    from core.squeeze_hunter import classify_stage

    base = dict(fuel=60.0, trigger=40.0, confirmation=60.0, rvol=6.0, breakout_pct=12.0)
    exhausted = classify_stage(move_pct=80.0, current_move_pct=30.0, momentum_declining=True,
                               price_parabolic=True, huge_volume=True, **base)
    assert exhausted["stage"] == "exhaustion"
    # Reversal reads where price is NOW: the peak was +80%, price is back below
    # the prior close. Before the fix the peak move was tested (> 0), so a
    # round-tripped name could never be labeled a reversal.
    reversed_ = classify_stage(move_pct=80.0, current_move_pct=-4.0, momentum_declining=True,
                               **base)
    assert reversed_["stage"] == "reversal"
    # Without flags the same numbers still read as expansion (unchanged).
    assert classify_stage(move_pct=80.0, **base)["stage"] == "expansion"


def _stub_hunter_sources(monkeypatch):
    import core.catalyst_scoring as cs
    import core.squeeze_monitor as sm

    monkeypatch.setattr(sm, "_cached_short_context", lambda s: {}, raising=False)
    monkeypatch.setattr(cs, "fetch_event_context", lambda s, **k: {"available": False})


def test_fetch_report_labels_a_faded_runner_late_stage_and_not_qualified(monkeypatch):
    import core.squeeze_hunter as sh

    _stub_hunter_sources(monkeypatch)
    metrics = {"rvol": 7.0, "peak_move_pct": 90.0, "current_move_pct": 20.0,
               "price": 12.0, "vwap": 13.0, "session": "rth", "market_date": "2026-09-25"}
    rep = sh.fetch_explosion_report("FADE", cached_only=True, market_metrics=metrics,
                                    market_regime=None)
    assert rep["stage"] == "exhaustion"
    assert rep["lifecycle_flags"]["momentum_declining"] is True
    assert rep["qualified"] is False          # shown on the watchlist, never a candidate

    metrics_rev = dict(metrics, current_move_pct=-6.0)
    rep_rev = sh.fetch_explosion_report("FADE", cached_only=True, market_metrics=metrics_rev,
                                        market_regime=None)
    assert rep_rev["stage"] == "reversal"
    assert rep_rev["qualified"] is False


def test_fetch_report_leaves_a_holding_runner_in_expansion(monkeypatch):
    import core.squeeze_hunter as sh

    _stub_hunter_sources(monkeypatch)
    metrics = {"rvol": 7.0, "peak_move_pct": 40.0, "current_move_pct": 38.0,
               "price": 14.0, "vwap": 13.0, "session": "rth", "market_date": "2026-09-25"}
    rep = sh.fetch_explosion_report("HOLD", cached_only=True, market_metrics=metrics,
                                    market_regime=None)
    assert rep["stage"] == "expansion"
    assert rep["qualified"] is True


# ── U02: one SIP 403 no longer sends daily history to IEX for 6 hours ───────

class _Resp:
    def __init__(self, code, bars=None):
        self.status_code = code
        self._bars = bars or []

    def json(self):
        return {"bars": self._bars}


def _daily_bar(close):
    return {"t": "2026-09-24T04:00:00Z", "o": close, "h": close, "l": close, "c": close, "v": 1e6}


def test_recent_sip_403_retries_daily_bars_on_delayed_sip(monkeypatch):
    import core.signal_engine as se

    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    calls = []
    sip_calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        if "feed=sip" in url:
            sip_calls.append(url)
        if "feed=sip" in url and len(sip_calls) == 1:
            return _Resp(403)                  # "does not permit querying recent SIP data"
        if "feed=sip" in url:
            return _Resp(200, [_daily_bar(50.0)])
        return _Resp(200, [_daily_bar(49.0)])  # IEX must not be needed

    monkeypatch.setattr("requests.get", fake_get)
    rows = se._fetch_ohlcv_once("AAPL", "stock", period="1mo")
    assert rows and rows[0]["close"] == 50.0
    assert [("sip" if "feed=sip" in c else "iex") for c in calls] == ["sip", "sip"]
    # The retry's window ends at least 15 minutes ago.
    end_first = calls[0].split("&end=")[1].split("&")[0]
    end_retry = calls[1].split("&end=")[1].split("&")[0]
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    gap = (_dt.datetime.strptime(end_first, fmt) - _dt.datetime.strptime(end_retry, fmt)).total_seconds()
    assert gap >= 15 * 60

    # Inside the pause the next daily fetch goes straight to delayed SIP.
    calls.clear()
    se._fetch_ohlcv_once("MSFT", "stock", period="1mo")
    assert len(calls) == 1 and "feed=sip" in calls[0]


def test_sip_pause_is_rechecked_within_30_minutes(monkeypatch):
    import core.prices as px

    monkeypatch.delenv("SIP_FORBIDDEN_RECHECK_S", raising=False)
    before = time.time()
    px._note_alpaca_feed_status("sip", 403)
    assert px._alpaca_bar_feeds() == ("iex",)
    assert px._SIP_FORBIDDEN["until"] <= before + 1800 + 5
    px._SIP_FORBIDDEN["until"] = time.time() - 1
    assert px._alpaca_bar_feeds() == ("sip", "iex")


def test_intraday_timeframe_does_not_use_delayed_sip(monkeypatch):
    import core.signal_engine as se

    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        if "feed=sip" in url:
            return _Resp(403)
        return _Resp(200, [_daily_bar(49.0)])

    monkeypatch.setattr("requests.get", fake_get)
    se._fetch_ohlcv_once("AAPL", "stock", period="5d", interval="1h")
    assert [("sip" if "feed=sip" in c else "iex") for c in calls] == ["sip", "iex"]


def test_delayed_sip_403_falls_back_to_iex_and_is_remembered(monkeypatch):
    import core.signal_engine as se

    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        if "feed=sip" in url:
            return _Resp(403)
        return _Resp(200, [_daily_bar(49.0)])

    monkeypatch.setattr("requests.get", fake_get)
    rows = se._fetch_ohlcv_once("AAPL", "stock", period="1mo")
    assert rows and rows[0]["close"] == 49.0
    assert [("sip" if "feed=sip" in c else "iex") for c in calls] == ["sip", "sip", "iex"]
    calls.clear()
    se._fetch_ohlcv_once("MSFT", "stock", period="1mo")
    assert [("sip" if "feed=sip" in c else "iex") for c in calls] == ["iex"]


# ── U03: split artifacts are not listed as movers ───────────────────────────

def _grouped(ticker, close, *, open_=None, volume=5_000_000):
    return {"T": ticker, "c": close, "v": volume, "o": close if open_ is None else open_,
            "h": close * 1.02, "l": close * 0.98, "n": 100, "vw": close,
            "t": int(time.time() * 1000)}


def test_bcpc_and_tpc_class_moves_are_suspected_splits(monkeypatch):
    import core.market_wide_snapshot as mws

    for name in ("MARKET_WIDE_MIN_MOVE_PCT", "MARKET_WIDE_MIN_PRICE",
                 "MARKET_WIDE_MIN_DOLLAR_VOLUME", "MARKET_WIDE_MAX_ROWS"):
        monkeypatch.delenv(name, raising=False)
    prior = [_grouped("BCPC", 150.0, volume=700_000), _grouped("TPC", 20.0, volume=1_000_000),
             _grouped("RUNR", 4.0, volume=1_000_000), _grouped("GAPR", 4.0, volume=1_000_000)]
    latest = [
        # +606.69%: whole session at 7x, share volume re-based (~1/7): same dollars.
        _grouped("BCPC", 150.0 * 7.0669, volume=110_000),
        _grouped("TPC", 20.0 * 4.9982, volume=210_000),       # +399.82%
        _grouped("RUNR", 20.0, open_=4.2, volume=30_000_000),  # real runner: opened near prior
        # Real gap-and-hold at exactly 5x -- but on 40x the prior day's dollars.
        _grouped("GAPR", 20.0, volume=8_000_000),
    ]
    built = mws.build_market_wide_rows(latest, prior, latest_day="2026-09-25")
    stored = {r["symbol"] for r in built["rows"]}
    assert "BCPC" not in stored and "TPC" not in stored
    assert "RUNR" in stored and "GAPR" in stored
    assert built["dropped"]["suspected_split"] == 2
    assert {s["ticker"]: s["factor"] for s in built["suspected_splits"]} == {"BCPC": 7, "TPC": 5}
    assert built["max_abs_move_seen_pct"] == pytest.approx(400.0)


def test_split_factor_ignores_ordinary_moves():
    from core.market_wide_snapshot import split_artifact_factor as f

    assert f(130.0, 100.0, 130.0) is None            # +30%
    assert f(310.0, 100.0, 305.0) is None            # +210%: 3.1x is not within 1.5% of 3
    assert f(49.4, 100.0, 99.0) is None              # -50.6% crash that opened flat
    assert f(50.0, 100.0, 50.0) == 2                 # 1-for-2 forward split artifact
    # Same prices, but twice the prior day's dollars changed hands: a real move.
    assert f(50.0, 100.0, 50.0, volume=4_000_000, prior_volume=1_000_000) is None
    assert f(50.0, 100.0, 50.0, volume=2_000_000, prior_volume=1_000_000) == 2


# ── U14: "non-dilutive" is not a dilution event ─────────────────────────────

@pytest.mark.parametrize("headline", [
    "Acme secures $40M non-dilutive financing from BARDA",
    "Acme non dilutive grant extends runway",
    "Acme warrants carry anti-dilution protection",
])
def test_non_dilutive_is_not_dilution(headline):
    from core.news_events import classify_text

    assert "dilution_or_offering" not in {e["event_type"] for e in classify_text(headline)}


def test_real_dilution_still_classified():
    from core.news_events import classify_text

    for headline in ("Acme announces dilutive equity raise", "Holders diluted by conversion"):
        assert "dilution_or_offering" in {e["event_type"] for e in classify_text(headline)}


# ── U19: earnings surprise needs a dated, recent report ─────────────────────

class _Frame:
    """Minimal DataFrame stand-in for yfinance frames."""

    def __init__(self, rows):
        self._rows = rows          # list of (index, dict)
        self.empty = not rows
        self.index = [r[0] for r in rows]

    @property
    def iloc(self):
        rows = self._rows

        class _I:
            def __getitem__(self, i):
                return rows[i][1]
        return _I()

    def iterrows(self):
        return iter(self._rows)


def _ts(day):
    return _dt.datetime.fromisoformat(day).replace(tzinfo=_dt.timezone.utc)


def test_report_ts_comes_from_the_calendar_not_the_quarter_end(monkeypatch):
    import core.earnings_surprise as es

    history = _Frame([(_ts("2026-06-30"), {"epsEstimate": 0.5, "epsActual": 0.6})])
    calendar = _Frame([
        (_ts("2026-10-29"), {"Reported EPS": float("nan")}),   # next, not yet reported
        (_ts("2026-07-24"), {"Reported EPS": 0.6}),            # this quarter's report
        (_ts("2026-04-25"), {"Reported EPS": 0.4}),            # prior quarter
    ])
    tk = SimpleNamespace(earnings_history=history, quarterly_income_stmt=None,
                         quarterly_financials=None, get_earnings_dates=lambda limit=12: calendar)
    import yfinance
    monkeypatch.setattr(yfinance, "Ticker", lambda s: tk)
    out = es._latest_quarter_earnings("ACME")
    assert out["quarter_end_ts"] == int(_ts("2026-06-30").timestamp())
    assert out["report_ts"] == int(_ts("2026-07-24").timestamp())
    assert out["report_ts_basis"] == es.REPORT_TS_BASIS_CALENDAR


def test_report_ts_without_calendar_is_a_conservative_upper_bound(monkeypatch):
    import core.earnings_surprise as es

    history = _Frame([(_ts("2026-06-30"), {"epsEstimate": 0.5, "epsActual": 0.6})])

    def no_calendar(limit=12):
        raise RuntimeError("calendar scrape failed")

    tk = SimpleNamespace(earnings_history=history, quarterly_income_stmt=None,
                         quarterly_financials=None, get_earnings_dates=no_calendar)
    import yfinance
    monkeypatch.setattr(yfinance, "Ticker", lambda s: tk)
    out = es._latest_quarter_earnings("ACME")
    q_end = int(_ts("2026-06-30").timestamp())
    assert out["report_ts"] == q_end + 90 * 86400
    assert out["report_ts_basis"] == es.REPORT_TS_BASIS_BOUND
    # A decision a month after the quarter end must NOT see this result.
    es._cache.clear()
    monkeypatch.setattr(es, "_latest_quarter_earnings", lambda s: dict(out))
    assert es.get_earnings_surprise("ACME", asof_ts=q_end + 30 * 86400)["available"] is False


def _surprise(report_ts, basis="earnings_calendar"):
    return {"available": True, "eps_actual": 0.6, "eps_expected": 0.5, "revenue_actual": None,
            "quarter": "2026-06-30", "report_ts": report_ts, "report_ts_basis": basis}


def test_trigger_rejects_a_stale_report(monkeypatch):
    import core.earnings_surprise as es

    monkeypatch.setattr(es, "get_earnings_surprise",
                        lambda s: _surprise(int(time.time()) - 60 * 86400))
    out = es.earnings_surprise_to_trigger("ACME")
    assert out["earnings_available"] is False
    assert out["earnings_surprise"] == 0.0
    assert out["reason"] == "report_not_recent"


def test_trigger_rejects_an_undated_report(monkeypatch):
    import core.earnings_surprise as es

    monkeypatch.setattr(es, "get_earnings_surprise",
                        lambda s: _surprise(int(time.time()) - 86400,
                                            basis=es.REPORT_TS_BASIS_BOUND))
    out = es.earnings_surprise_to_trigger("ACME")
    assert out["earnings_available"] is False
    assert out["reason"] == "report_date_unknown"


def test_trigger_accepts_a_fresh_dated_report(monkeypatch):
    import core.earnings_surprise as es

    monkeypatch.setattr(es, "get_earnings_surprise",
                        lambda s: _surprise(int(time.time()) - 3 * 86400))
    out = es.earnings_surprise_to_trigger("ACME")
    assert out["earnings_available"] is True
    assert out["eps_surprise_pct"] == 20.0
    assert out["revenue_surprise_pct"] is None      # no free consensus: stays unknown


# ── U24: rows long past their horizon become visible UNRESOLVED ─────────────

def test_unresolved_reason_only_after_grace():
    from core.shadow_outcomes import unresolved_reason

    exp = 1_780_000_000
    assert unresolved_reason(now=exp + 3600, expires_at=exp, has_bars=False, grace_s=864000) is None
    assert unresolved_reason(now=exp + 864001, expires_at=exp, has_bars=False,
                             grace_s=864000) == "no_daily_bars"
    assert unresolved_reason(now=exp + 864001, expires_at=exp, has_bars=True,
                             grace_s=864000) == "incomplete_forward_bars"
    assert unresolved_reason(now=exp * 2, expires_at=None, has_bars=False) is None


def _shadow_db(monkeypatch, pending_row, updates):
    from core import shadow_outcomes as so

    class _Cur:
        def __init__(self):
            self.last_sql = ""

        def execute(self, sql, params=None):
            self.last_sql = sql
            if "UPDATE ghost_shadow_outcomes" in sql:
                updates.append((sql, params))

        def fetchall(self):
            if "outcome IS NULL" in self.last_sql:
                return [pending_row]
            return []

    class _Conn:
        def cursor(self):
            return _Cur()

    class _Ctx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("core.db.db_conn", lambda: _Ctx())
    monkeypatch.setattr(so, "ensure_shadow_table", lambda cur: None)


def test_resolver_closes_a_barless_row_long_past_horizon_as_unresolved(monkeypatch):
    from core import shadow_outcomes as so
    import core.signal_engine as se

    entry = int(_dt.datetime(2026, 8, 10, 15, tzinfo=_dt.timezone.utc).timestamp())
    expires = int(_dt.datetime(2026, 8, 13, 21, tzinfo=_dt.timezone.utc).timestamp())
    updates = []
    _shadow_db(monkeypatch, (7, "DEAD", entry, 10.0, 10.2, 9.9, expires, "UP", 3), updates)
    monkeypatch.setattr(so.time, "time", lambda: expires + 39 * 86400)
    monkeypatch.setattr(se, "_fetch_ohlcv", lambda *a, **k: [])
    assert so.resolve_shadow_rows(max_symbols=5) == 0     # not counted as resolved
    assert len(updates) == 1
    sql, params = updates[0]
    assert "unresolved_reason" in sql
    assert params[0] == "UNRESOLVED" and params[1] == "no_daily_bars" and params[3] == 7


def test_unresolved_rows_are_counted_but_never_scored():
    from core.shadow_outcomes import aggregate_shadow_stats

    rows = [
        {"symbol": "A", "outcome": "WIN", "eval_ts": 1, "up_prob": 0.6},
        {"symbol": "A", "outcome": "LOSS", "eval_ts": 2, "up_prob": 0.6},
        {"symbol": "A", "outcome": "UNRESOLVED", "eval_ts": 3, "up_prob": 0.6},
        {"symbol": "A", "outcome": None, "eval_ts": 4, "up_prob": 0.6},
    ]
    out = aggregate_shadow_stats(rows)
    assert out["resolved"] == 2
    assert out["pending"] == 1
    assert out["unresolved"] == 1
    assert out["symbols"][0]["tp_rate_pct"] == 50.0
    assert out["symbols"][0]["expired"] == 0


# ── U28: keyword fallback matches whole words ───────────────────────────────

def test_keyword_fallback_uses_word_boundaries():
    from core.news import _keyword_hits, _keyword_score

    # "Bank" used to count as "ban", "commission" as "miss".
    assert _keyword_hits("Bank of America commission probe widens") == (0, 0)
    assert _keyword_score("Startup flower shop opens") == 0.0
    assert _keyword_hits("Shares fall after earnings miss") == (0, 2)
    assert _keyword_hits("Stock jumps after upgrade") == (2, 0)


# ── U49: broad-market previous close is the prior session ───────────────────

def test_previous_close_before_skips_the_current_session():
    pd = pytest.importorskip("pandas")
    from core.broad_market_context import previous_close_before

    idx = pd.to_datetime(["2026-09-22", "2026-09-23", "2026-09-24"]).tz_localize("America/New_York")
    closes = pd.Series([100.0, 101.0, 102.0], index=idx)
    # Premarket 9/25: there is no 9/25 daily bar yet -> 9/24 close (was 9/23).
    assert previous_close_before(closes, _dt.date(2026, 9, 25)) == 102.0
    # RTH 9/24: today's partial bar is present -> 9/23 close.
    assert previous_close_before(closes, _dt.date(2026, 9, 24)) == 101.0
    assert previous_close_before(closes, _dt.date(2026, 9, 22)) is None


# ── U57: syndicated copies count once ───────────────────────────────────────

def test_syndicated_headlines_count_once():
    from core.news_sentiment import score_articles
    from core.news_store import dedupe_syndicated

    rows = [
        {"symbol": "ACME", "title": "Acme beats estimates, raises outlook", "url": "https://a/1"},
        {"symbol": "ACME", "title": "ACME beats estimates -- raises outlook!", "url": "https://b/2"},
        {"symbol": "ACME", "title": "Acme cut to underweight", "url": "https://c/3"},
        {"symbol": "OTHR", "title": "Acme beats estimates, raises outlook", "url": "https://d/4"},
    ]
    kept = dedupe_syndicated(rows)
    assert [r["url"] for r in kept] == ["https://a/1", "https://c/3", "https://d/4"]
    assert score_articles(rows[:3], symbol="ACME")["count"] == 2


# ── U68: no derived catalysts for delisted peers ────────────────────────────

def test_peer_map_has_no_delisted_tickers():
    from core.catalyst_graph import SECTOR_GROUPS, peers_of, propagate_catalyst

    members = {m for group in SECTOR_GROUPS.values() for m in group}
    assert not members & {"BBBY", "EXPR", "NOVA"}
    assert "BBBY" not in peers_of("GME")
    derived = propagate_catalyst("FSLR", "guidance_cut", materiality=0.85, asof_ts=1)
    assert "NOVA" not in {d["symbol"] for d in derived}


def test_retired_symbols_never_receive_derived_catalysts(monkeypatch):
    import config.symbols as cfg
    from core.catalyst_graph import peers_of

    monkeypatch.setitem(cfg.RETIRED_SYMBOLS, "KOSS", "test: no longer trades")
    assert "KOSS" not in peers_of("GME")
    assert "AMC" in peers_of("GME")
