"""PR #136: batch market sessions — cache-first, budget-bounded, freshness truth.

The live-market audit tripped breakers by sweeping 43 symbols one-by-one; this
layer must make that impossible: at most `max_fresh` provider hits per call,
everything else served from cache with an honest provider_state label.
"""
import time

import core.market_sessions as ms
import core.prices as prices
import core.circuit_breaker as cb


class _FakeBreaker:
    def __init__(self, allow=True):
        self._allow = allow

    def allow(self):
        return self._allow


def _setup(monkeypatch, cache=None, allow=True, fetch_result=None, fetch_calls=None):
    monkeypatch.setattr(prices, "_intraday_cache", cache if cache is not None else {})
    monkeypatch.setattr(cb, "_alpaca_cb", _FakeBreaker(allow))

    def fake_fetch(sym):
        if fetch_calls is not None:
            fetch_calls.append(sym)
        row = dict(fetch_result or {"price": 10.0, "feed": "alpaca_iex", "session": "rth"})
        prices._intraday_cache[sym] = (time.time(), row)
        return dict(row)

    monkeypatch.setattr(prices, "get_intraday_session", fake_fetch)


def test_fresh_fetch_budget_is_enforced(monkeypatch):
    calls = []
    _setup(monkeypatch, cache={}, fetch_calls=calls)
    syms = [f"S{i}" for i in range(20)]
    out = ms.get_market_sessions(syms, max_fresh=5)
    assert out["fresh_fetches"] == 5
    assert len(calls) == 5
    assert out["count"] == 20  # every symbol still gets a row (partial results)
    states = {r["provider_state"] for r in out["sessions"].values()}
    assert "unavailable" in states  # uncached beyond budget, honestly labeled


def test_young_cache_serves_as_live_without_fetching(monkeypatch):
    calls = []
    cache = {"WOLF": (time.time() - 10, {"price": 41.3, "feed": "alpaca_iex"})}
    _setup(monkeypatch, cache=cache, fetch_calls=calls)
    out = ms.get_market_sessions(["WOLF"], max_fresh=5)
    row = out["sessions"]["WOLF"]
    assert row["provider_state"] == "live"
    assert row["freshness_seconds"] <= 11
    assert calls == []  # no provider hit


def test_stale_cache_labeled_stale_not_hidden(monkeypatch):
    cache = {"OLD": (time.time() - 5000, {"price": 5.0, "feed": "alpaca_iex"})}
    _setup(monkeypatch, cache=cache)
    out = ms.get_market_sessions(["OLD"], max_fresh=0)
    row = out["sessions"]["OLD"]
    assert row["provider_state"] == "stale"
    assert row["freshness_seconds"] >= 4999
    assert row["ok"] is True  # price still returned, just honestly old


def test_breaker_open_labeled(monkeypatch):
    _setup(monkeypatch, cache={}, allow=False)
    out = ms.get_market_sessions(["NEW"], max_fresh=5)
    assert out["sessions"]["NEW"]["provider_state"] == "breaker_open"
    assert out["sessions"]["NEW"]["ok"] is False


def test_fetch_failure_falls_back_to_cache_as_stale(monkeypatch):
    cache = {"X": (time.time() - 2000, {"price": 3.0, "feed": "alpaca_iex"})}
    monkeypatch.setattr(prices, "_intraday_cache", cache)
    monkeypatch.setattr(cb, "_alpaca_cb", _FakeBreaker(True))

    def boom(sym):
        raise RuntimeError("provider down")
    monkeypatch.setattr(prices, "get_intraday_session", boom)
    out = ms.get_market_sessions(["X"], max_fresh=5)
    row = out["sessions"]["X"]
    assert row["provider_state"] == "stale"
    assert row["price"] == 3.0


def test_fleet_summary_counts(monkeypatch):
    import json
    import core.signal_engine as se

    metas = {
        "GOOD_up": {"accuracy": 0.7, "natural_rate": 0.6, "edge": 0.1, "wf_edge_mean": 0.06,
                    "wf_acc_mean": 0.65, "wf_fold_count": 5, "wf_acc_min": 0.5, "wf_edge_min": 0.0,
                    "n_samples": 300, "engine_version": "v3.2", "label_type": "tp_sl_daily",
                    "label_schema": "x", "feature_schema": "x",
                    "precision_gate": {"ok": True, "threshold": 0.6}},
        "RIDER_down": {"accuracy": 0.745, "natural_rate": 0.745, "edge": 0.0, "wf_edge_mean": -0.03,
                       "wf_acc_mean": 0.6, "wf_fold_count": 5, "wf_acc_min": 0.5, "wf_edge_min": -0.1,
                       "n_samples": 300, "engine_version": "v3.2", "label_type": "tp_sl_daily",
                       "label_schema": "x", "feature_schema": "x",
                       "precision_gate": {"ok": True, "threshold": 0.6}},
    }

    class _Cur:
        def execute(self, sql, *a):
            self._last = sql
        def fetchall(self):
            if "meta_%" in self._last:
                return [(f"meta_{k}", json.dumps(v)) for k, v in metas.items()]
            return [(f"model_{k}",) for k in metas]
        def fetchone(self):
            return None

    class _Conn:
        def cursor(self):
            return _Cur()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    import core.db as db
    monkeypatch.setattr(db, "db_conn", lambda: _Conn())
    monkeypatch.setattr(se, "model_serve_guard", lambda m: None)
    monkeypatch.setattr(se, "get_last_train_gate_summary", lambda: {})
    monkeypatch.setenv("V3_DOWN_SIGNALS_ENABLED", "0")
    monkeypatch.setenv("V3_PROVEN_SKILL_GATE", "0")  # isolate fleet-summary legacy counts
    st = se.get_model_status()
    fs = st["fleet_summary"]
    assert fs["fireable_now"] == 1 and fs["fireable_models"] == ["GOOD_up"]
    assert fs["base_rate_riders"] == 1
    assert fs["precision_ok"] == 2  # display flag still overstates — that's the point
    assert "missing_v3" in st  # every unserveable watchlist symbol gets a reason


def test_null_price_cache_falls_back_to_rth_close(monkeypatch):
    # PR #137 audit bug: cache rows written during a failed trade fetch have
    # price=null but valid RTH data — batch must patch like the single endpoint.
    cache = {"WOLF": (time.time() - 30, {"price": None, "rth_close": 41.21,
                                         "today_open": 39.62, "today_high": 42.31,
                                         "previous_close": 40.195,
                                         "feed": "alpaca_iex"})}
    _setup(monkeypatch, cache=cache)
    out = ms.get_market_sessions(["WOLF"], max_fresh=0)
    row = out["sessions"]["WOLF"]
    assert row["ok"] is True
    assert row["price"] == 41.21
    assert row["price_source"] == "rth_close_fallback"
    assert row["change_pct"] is not None  # recomputed from patched price


def test_ohlc_only_row_is_still_ok(monkeypatch):
    cache = {"X": (time.time() - 30, {"price": None, "rth_close": None,
                                      "today_open": 5.0, "today_high": 5.2,
                                      "feed": "alpaca_iex"})}
    _setup(monkeypatch, cache=cache)
    out = ms.get_market_sessions(["X"], max_fresh=0)
    row = out["sessions"]["X"]
    assert row["ok"] is True            # has_ohlc — usable market truth exists
    assert row["price"] == 5.0          # today_open fallback
    assert row["price_source"] == "today_open_fallback"


def test_truly_empty_row_stays_not_ok(monkeypatch):
    cache = {"Y": (time.time() - 30, {"price": None, "feed": None})}
    _setup(monkeypatch, cache=cache)
    out = ms.get_market_sessions(["Y"], max_fresh=0)
    assert out["sessions"]["Y"]["ok"] is False
    assert out["sessions"]["Y"]["provider_state"] == "unavailable"
    assert "no usable price/OHLC" in out["sessions"]["Y"]["state_note"]


def test_truly_empty_row_with_open_breaker_is_not_labeled_live(monkeypatch):
    cache = {"Y": (time.time() - 30, {"price": None, "feed": None})}
    _setup(monkeypatch, cache=cache, allow=False)
    out = ms.get_market_sessions(["Y"], max_fresh=0)
    assert out["sessions"]["Y"]["ok"] is False
    assert out["sessions"]["Y"]["provider_state"] == "breaker_open"
