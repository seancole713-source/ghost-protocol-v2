"""Feed/scheduler resilience — regression tests for the prod-log defects.

R3-A  Alpaca 200-with-null-bars must not raise 'NoneType' is not iterable.
R3-B  Shadow seeder cannot deadlock: advisory lock + deterministic order.
R3-C  _fetch_ohlcv: TTL cache, negative cache, and in-flight dedupe.
"""
import inspect
import threading

import core.signal_engine as se
import core.shadow_outcomes as so


# ── R3-A · null-bars guard ───────────────────────────────────────────────

def test_alpaca_null_bars_guarded():
    """Alpaca returns HTTP 200 with "bars": null for dead symbols; the parse
    must coerce to [] instead of iterating None."""
    src = inspect.getsource(se._fetch_ohlcv_once)
    assert ".get('bars') or []" in src


# ── R3-B · shadow seeder deadlock guards ─────────────────────────────────

def test_pick_daily_first_returns_deterministic_order():
    base = 1781000000
    evals = [
        {"symbol": "ZZZ", "eval_ts": base + 50},
        {"symbol": "AAA", "eval_ts": base + 10},
        {"symbol": "MMM", "eval_ts": base + 20},
    ]
    out = so.pick_daily_first(evals)
    assert [e["symbol"] for e in out] == ["AAA", "MMM", "ZZZ"]


def test_seed_skips_when_advisory_lock_held(monkeypatch):
    """If another seeder holds the advisory lock, seeding returns 0 without
    reading evals — concurrent seeders can no longer deadlock each other."""
    executed = []

    class _Cur:
        def execute(self, sql, params=None):
            executed.append(sql)

        def fetchone(self):
            return (False,)  # lock NOT acquired

        def fetchall(self):
            return []

    class _Conn:
        def cursor(self):
            return _Cur()

    class _Ctx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    import core.db as db
    monkeypatch.setattr(db, "db_conn", lambda: _Ctx())
    assert so.seed_shadow_rows(days_back=1) == 0
    assert any("pg_try_advisory_xact_lock" in s for s in executed)
    assert not any("ghost_perf_symbol_evals" in s for s in executed), (
        "seeder must bail before reading evals when the lock is held"
    )


# ── R3-C · OHLCV cache semantics ─────────────────────────────────────────

def _fresh_cache(monkeypatch):
    se.clear_ohlcv_cache()
    monkeypatch.setenv("V3_OHLCV_FETCH_RETRIES", "1")


def _valid_bar(close=1.0):
    return {
        "ts": "2026-06-03T00:00:00Z",
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1.0,
    }


def test_ohlcv_success_cached_within_ttl(monkeypatch):
    _fresh_cache(monkeypatch)
    monkeypatch.setenv("V3_OHLCV_CACHE_TTL_S", "900")
    calls = []
    monkeypatch.setattr(se, "_fetch_ohlcv_once",
                        lambda *a, **k: calls.append(1) or [_valid_bar()])
    r1 = se._fetch_ohlcv("STUB", "stock", period="3m")
    r2 = se._fetch_ohlcv("STUB", "stock", period="3m")
    assert r1 == r2 == [_valid_bar()]
    assert len(calls) == 1, "second call within TTL must hit the cache"

def test_ohlcv_cache_isolated_by_interval(monkeypatch):
    _fresh_cache(monkeypatch)
    monkeypatch.setenv("V3_OHLCV_CACHE_TTL_S", "900")
    calls = []

    def fetch(*args, **kwargs):
        calls.append(kwargs.get("interval", args[3] if len(args) > 3 else None))
        return [_valid_bar()]

    monkeypatch.setattr(se, "_fetch_ohlcv_once", fetch)
    se._fetch_ohlcv("STUB", "stock", period="5d", interval="1d")
    se._fetch_ohlcv("STUB", "stock", period="5d", interval="1h")

    assert calls == ["1d", "1h"]

def test_alpaca_fetch_honors_requested_timeframe(monkeypatch):
    monkeypatch.setenv("ALPACA_KEY_ID", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    urls = []

    class Response:
        status_code = 200

        def json(self):
            return {"bars": [{
                "t": "2026-06-03T15:00:00Z", "o": 1, "h": 1,
                "l": 1, "c": 1, "v": 1,
            }]}

    import requests
    monkeypatch.setattr(requests, "get", lambda url, **kwargs: urls.append(url) or Response())
    se._fetch_ohlcv_once("STUB", "stock", period="5d", interval="1h")

    assert "timeframe=1Hour" in urls[0]


def test_ohlcv_failure_negative_cached(monkeypatch):
    """A symbol failing every tier must not re-run the chain on every call —
    the permanent-429-storm bug (RDFN)."""
    _fresh_cache(monkeypatch)
    monkeypatch.setenv("V3_OHLCV_NEG_CACHE_TTL_S", "600")
    calls = []
    monkeypatch.setattr(se, "_fetch_ohlcv_once",
                        lambda *a, **k: calls.append(1) or None)
    assert se._fetch_ohlcv("DEAD", "stock", period="3m") is None
    assert se._fetch_ohlcv("DEAD", "stock", period="3m") is None
    assert len(calls) == 1, "failure must be negative-cached"


def test_ohlcv_ttl_zero_disables_success_cache(monkeypatch):
    _fresh_cache(monkeypatch)
    monkeypatch.setenv("V3_OHLCV_CACHE_TTL_S", "0")
    calls = []
    monkeypatch.setattr(se, "_fetch_ohlcv_once",
                        lambda *a, **k: calls.append(1) or [_valid_bar()])
    se._fetch_ohlcv("STUB", "stock", period="3m")
    se._fetch_ohlcv("STUB", "stock", period="3m")
    assert len(calls) == 2, "TTL=0 entries expire immediately"


def test_ohlcv_concurrent_callers_fetch_once(monkeypatch):
    """Two threads asking for the same symbol run the chain exactly once —
    kills the doubled fetch chains seen in prod logs."""
    _fresh_cache(monkeypatch)
    monkeypatch.setenv("V3_OHLCV_CACHE_TTL_S", "900")
    calls = []
    started = threading.Event()
    release = threading.Event()

    def slow_fetch(*a, **k):
        calls.append(1)
        started.set()
        release.wait(timeout=5)
        return [_valid_bar(2.0)]

    monkeypatch.setattr(se, "_fetch_ohlcv_once", slow_fetch)
    results = []

    def worker():
        results.append(se._fetch_ohlcv("STUB", "stock", period="3m"))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    started.wait(timeout=5)   # first thread is inside the fetch
    t2.start()                # second thread must block on the key lock
    release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert len(calls) == 1, "concurrent identical fetches must dedupe"
    assert results == [[_valid_bar(2.0)], [_valid_bar(2.0)]]
