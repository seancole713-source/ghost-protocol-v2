import json
import time

import wolf_app


class QueueCursor:
    def __init__(self, fetchall_values=None, fetchone_values=None):
        self.fetchall_values = list(fetchall_values or [])
        self.fetchone_values = list(fetchone_values or [])
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        if not self.fetchall_values:
            return []
        return self.fetchall_values.pop(0)

    def fetchone(self):
        if not self.fetchone_values:
            return None
        return self.fetchone_values.pop(0)


class RoutingCursor:
    def __init__(self, sticky_ts, metas, hist_min):
        self.sticky_ts = sticky_ts
        self.metas = metas
        self.hist_min = hist_min
        self.last_sql = ""
        self.executed = []

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.executed.append((sql, params))

    def fetchone(self):
        if "SELECT val FROM ghost_state WHERE key='v32_stats_start_ts'" in self.last_sql:
            return (str(self.sticky_ts),) if self.sticky_ts else None
        if "SELECT MIN(predicted_at) FROM predictions" in self.last_sql:
            return (self.hist_min,) if self.hist_min else (None,)
        return None

    def fetchall(self):
        if "SELECT key, value FROM ghost_v3_model WHERE key LIKE 'meta_%'" in self.last_sql:
            return [
                (f"meta_{sym}", json.dumps(meta))
                for sym, meta in self.metas.items()
            ]
        return []


def test_v32_stats_start_ts_prefers_env_override(monkeypatch):
    monkeypatch.setenv("V3_STATS_START_TS", "1775347200")
    cur = QueueCursor()
    assert wolf_app._v32_stats_start_ts(cur) == 1775347200
    assert cur.executed == []


def test_v32_stats_start_ts_allows_backward_correction(monkeypatch):
    monkeypatch.delenv("V3_STATS_START_TS", raising=False)
    cur = RoutingCursor(
        sticky_ts=1775606400,
        metas={
            "WOLF": {"label_type": "tp_sl_daily", "trained_at": 1775347200},
        },
        hist_min=1775380000,
    )
    out = wolf_app._v32_stats_start_ts(cur)
    assert out == 1775347200
    assert any("INSERT INTO ghost_state(key,val) VALUES('v32_stats_start_ts'" in sql for sql, _ in cur.executed)


def test_compute_get_stats_uses_v32_breakdowns(monkeypatch):
    monkeypatch.setenv("V3_STATS_START_TS", "1775347200")
    monkeypatch.setenv("STOCK_SYMBOLS", "WOLF")
    cur = QueueCursor(
        fetchall_values=[
            [("WIN", 10), ("LOSS", 5)],
            [("WIN", 2), ("LOSS", 1)],
            [("WIN", 1), ("LOSS", 2)],
        ],
        fetchone_values=[(3,)],
    )
    payload = wolf_app._compute_get_stats(cur)
    assert payload["wins"] == 10
    assert payload["losses"] == 5
    assert payload["open_positions"] == 3
    assert payload["post_v32"]["start_ts"] == 1775347200
    assert payload["post_v32"]["wins"] == 2
    assert payload["post_v32"]["losses"] == 1
    assert payload["post_v32_resolved"]["wins"] == 1
    assert payload["post_v32_resolved"]["losses"] == 2
    assert payload["scan_symbols"]["stocks"] == ["WOLF"]


def test_api_health_alias_calls_health(monkeypatch):
    expected = {"status": "healthy", "score": 100}
    monkeypatch.setattr(wolf_app, "health", lambda: expected)
    assert wolf_app.api_health() == expected


def test_api_health_route_registered_once():
    paths = [getattr(r, "path", "") for r in wolf_app.APP.routes]
    assert paths.count("/api/health") == 1


def test_health_audit_endpoint_returns_wrapped_report(monkeypatch):
    class _Cur:
        def execute(self, sql, params=None):
            return None

        def fetchall(self):
            return []

        def fetchone(self):
            return None

    class _Conn:
        def cursor(self):
            return _Cur()

    class _DbCtx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, exc_type, exc, tb):
            return False

    async def _diag():
        return {"checks_passed": 1, "warnings": 0, "errors": 0}

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _DbCtx())
    monkeypatch.setattr(wolf_app, "health", lambda: {"status": "healthy", "score": 100})
    monkeypatch.setattr(wolf_app, "diagnostics", _diag)
    monkeypatch.setattr(wolf_app, "_compute_get_stats", lambda cur: {"wins": 2, "losses": 1})
    monkeypatch.setattr(wolf_app, "cockpit_context", lambda: {"stats": {"wins": 2, "losses": 1}})
    monkeypatch.setenv("CRON_SECRET", "")
    monkeypatch.setattr(
        "core.health_audit.run_health_audit",
        lambda **kwargs: {"status": "PASS", "summary": {"total_checks": 1}, "findings": []},
    )

    out = wolf_app.health_audit(x_cron_secret="", auto_fix=True)
    assert out["ok"] is True
    assert out["audit"]["status"] == "PASS"


def test_clean_garbage_sql_filter_targets_impossible_combos(monkeypatch):
    """Regression test: /api/clean-garbage must filter on the absurd
    entry/target combo (entry > 50, target < 1), NOT the legacy buggy
    range (entry BETWEEN 0.49 AND 0.51) that would delete legitimate
    sub-$1 picks. Locks in the post-PR#3.0 SQL string.
    """
    executed = []

    class _Cur:
        rowcount = 0

        def execute(self, sql, params=None):
            executed.append(sql)

        def fetchone(self):
            return (0,)

        def fetchall(self):
            return []

    class _Conn:
        def cursor(self):
            return _Cur()

    class _DbCtx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _DbCtx())
    monkeypatch.setattr(wolf_app, "CRON_SECRET", "")  # dev mode allows non-strict guard

    out = wolf_app.clean_garbage(x_cron_secret="")

    assert out["ok"] is True
    select_and_delete = [s for s in executed if "entry_price" in s and ("SELECT" in s or "DELETE" in s)]
    assert len(select_and_delete) == 2, f"expected 2 entry_price queries, got: {select_and_delete}"
    for sql in select_and_delete:
        # Correct filter — predictions with impossible entry/target combinations
        assert "entry_price > 50" in sql
        assert "target_price < 1" in sql
        # Regression: must NOT contain the legacy buggy range filter
        assert "BETWEEN 0.49 AND 0.51" not in sql
        assert "0.50" not in sql


def test_cron_ok_rejects_wrong_secret_when_env_set(monkeypatch):
    """monkeypatch.setenv must now gate _cron_ok — env is read at call time."""
    monkeypatch.setenv("CRON_SECRET", "correct-secret")
    assert wolf_app._cron_ok("correct-secret") is True
    assert wolf_app._cron_ok("wrong-secret") is False
    assert wolf_app._cron_ok("") is False


def test_cron_ok_dev_mode_when_env_unset(monkeypatch):
    """When CRON_SECRET is absent: non-strict allows, strict rejects."""
    monkeypatch.delenv("CRON_SECRET", raising=False)
    assert wolf_app._cron_ok("") is True          # non-strict: dev-mode allow
    assert wolf_app._cron_ok("", strict=True) is False  # strict: always reject


def _patch_db_conn_with_cursor(monkeypatch, cur):
    class _Conn:
        def cursor(self):
            return cur

    class _DbCtx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _DbCtx())


def test_confidence_buckets_empty_db_returns_zeroed_shape(monkeypatch):
    """No resolved picks → ok=True, 5 buckets, all zeros, labels in order."""
    monkeypatch.setattr(wolf_app, "_v32_stats_start_ts", lambda cur: 0)
    _patch_db_conn_with_cursor(monkeypatch, QueueCursor())
    out = wolf_app.get_stats_confidence_buckets()
    assert out["ok"] is True
    assert out["start_ts"] == 0
    assert len(out["buckets"]) == 5
    assert [b["label"] for b in out["buckets"]] == ["<60", "60-70", "70-80", "80-90", "90+"]
    for b in out["buckets"]:
        assert b["wins"] == 0
        assert b["losses"] == 0
        assert b["total"] == 0
        assert b["win_rate_pct"] == 0.0


def test_confidence_buckets_computes_per_bucket_winrate(monkeypatch):
    """Distinct W/L per bucket → win_rate_pct computed independently per band."""
    monkeypatch.setattr(wolf_app, "_v32_stats_start_ts", lambda cur: 1775347200)
    # One fetchall per bucket, in declared order: <60, 60-70, 70-80, 80-90, 90+
    cur = QueueCursor(fetchall_values=[
        [("WIN", 1), ("LOSS", 3)],   # <60:    1W/3L  = 25%
        [("WIN", 5), ("LOSS", 5)],   # 60-70:  5W/5L  = 50%
        [("WIN", 7), ("LOSS", 3)],   # 70-80:  7W/3L  = 70%
        [("WIN", 8), ("LOSS", 2)],   # 80-90:  8W/2L  = 80%
        [("WIN", 9), ("LOSS", 1)],   # 90+:    9W/1L  = 90%
    ])
    _patch_db_conn_with_cursor(monkeypatch, cur)
    out = wolf_app.get_stats_confidence_buckets()
    assert out["ok"] is True
    assert out["start_ts"] == 1775347200
    rates = {b["label"]: b["win_rate_pct"] for b in out["buckets"]}
    assert rates == {"<60": 25.0, "60-70": 50.0, "70-80": 70.0, "80-90": 80.0, "90+": 90.0}
    totals = {b["label"]: b["total"] for b in out["buckets"]}
    assert totals == {"<60": 4, "60-70": 10, "70-80": 10, "80-90": 10, "90+": 10}


# ════════════════════════════════════════════════════════════════════════
# PR #8 — WOLF command center
# ════════════════════════════════════════════════════════════════════════

def test_scan_symbols_drops_non_wolf_even_if_env_dirty(monkeypatch):
    """PR #8 hardening: STOCK_SYMBOLS=TSLA,META,WOLF,AMZN must reduce to ['WOLF']."""
    monkeypatch.setenv("STOCK_SYMBOLS", "TSLA,META,WOLF,AMZN,T")
    monkeypatch.setenv("V3_STATS_START_TS", "0")
    monkeypatch.delenv("V3_STATS_START_TS", raising=False)
    monkeypatch.setattr(wolf_app, "_v32_stats_start_ts", lambda cur: 0)
    cur = QueueCursor(fetchall_values=[[]], fetchone_values=[(0,)])
    payload = wolf_app._compute_get_stats(cur)
    assert payload["scan_symbols"]["stocks"] == ["WOLF"]


def test_scan_symbols_falls_back_to_wolf_when_env_empty(monkeypatch):
    monkeypatch.setenv("STOCK_SYMBOLS", "")
    monkeypatch.setattr(wolf_app, "_v32_stats_start_ts", lambda cur: 0)
    cur = QueueCursor(fetchall_values=[[]], fetchone_values=[(0,)])
    payload = wolf_app._compute_get_stats(cur)
    assert payload["scan_symbols"]["stocks"] == ["WOLF"]


def test_v3_status_strips_non_wolf_models(monkeypatch):
    """PR #8 hardening: stale BCH/SOL/UNI rows in ghost_v3_model must not surface."""
    import core.signal_engine as _se
    fake = {
        "trained": True,
        "models": 4,
        "symbols": {
            "WOLF": {"engine": "v3.2", "label_type": "tp_sl_daily", "accuracy": 71.0},
            "BCH": {"engine": "v3.0", "label_type": "tp_sl_4h", "accuracy": 52.0},
            "SOL": {"engine": "v3.0", "label_type": "tp_sl_4h", "accuracy": 49.0},
            "UNI": {"engine": "v3.0", "label_type": "tp_sl_4h", "accuracy": 50.0},
        },
    }
    monkeypatch.setattr(_se, "get_model_status", lambda: fake)
    out = wolf_app.v3_status()
    assert out["trained"] is True
    assert out["models"] == 1
    assert list(out["symbols"].keys()) == ["WOLF"]


def test_v3_status_flips_to_untrained_when_no_wolf_model(monkeypatch):
    import core.signal_engine as _se
    fake = {"trained": True, "models": 1, "symbols": {"BCH": {"engine": "v3.0"}}}
    monkeypatch.setattr(_se, "get_model_status", lambda: fake)
    out = wolf_app.v3_status()
    assert out["trained"] is False
    assert "WOLF" in str(out.get("reason", ""))


# ── /api/wolf/signal-alert/check — Telegram alert throttling ───────────

class _SignalAlertCursor:
    """Cursor that scripts the SQL execution path of wolf_signal_alert_check.

    Expected execution order:
      1. CREATE TABLE IF NOT EXISTS …  → no-op
      2. SELECT COUNT(*) FROM wolf_signal_alerts WHERE sent_at >= %s
      3. SELECT … FROM predictions … LEFT JOIN wolf_signal_alerts …
      4. INSERT INTO wolf_signal_alerts … (per candidate)
    """

    def __init__(self, sent_today=0, candidates=None):
        self.sent_today = sent_today
        self.candidates = list(candidates or [])
        self.executed = []
        self.last_sql = ""

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.executed.append((sql, params))

    def fetchone(self):
        s = self.last_sql
        if "SELECT COUNT(*) FROM wolf_signal_alerts" in s:
            return (self.sent_today,)
        return None

    def fetchall(self):
        if "FROM predictions" in self.last_sql and "LEFT JOIN wolf_signal_alerts" in self.last_sql:
            return list(self.candidates)
        return []


def _patch_signal_alert(monkeypatch, cur, sent_messages):
    class _Conn:
        def cursor(self):
            return cur

    class _DbCtx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _DbCtx())

    import core.telegram as _tg
    monkeypatch.setattr(_tg, "_send", lambda text: sent_messages.append(text))


def test_signal_alert_check_skips_when_daily_cap_reached(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "")
    monkeypatch.setenv("WOLF_ALERT_DAILY_CAP", "2")
    sent = []
    cur = _SignalAlertCursor(sent_today=2, candidates=[])
    _patch_signal_alert(monkeypatch, cur, sent)
    out = wolf_app.wolf_signal_alert_check(x_cron_secret="")
    assert out["ok"] is True
    assert out["sent"] == []
    assert "daily cap" in (out.get("skipped_reason") or "")
    assert sent == []  # no telegram sent


def test_signal_alert_check_sends_high_conf_and_records(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "")
    monkeypatch.setenv("WOLF_ALERT_DAILY_CAP", "2")
    monkeypatch.setenv("WOLF_ALERT_CONFIDENCE_FLOOR", "0.80")
    sent = []
    # Two unalerted high-conf BUY picks
    candidates = [
        (101, "BUY", 0.92, 58.5, 72.0, 54.0, int(time.time()) + 86400, int(time.time())),
        (102, "BUY", 0.88, 60.0, 70.0, 56.0, int(time.time()) + 86400, int(time.time())),
    ]
    cur = _SignalAlertCursor(sent_today=0, candidates=candidates)
    _patch_signal_alert(monkeypatch, cur, sent)
    out = wolf_app.wolf_signal_alert_check(x_cron_secret="")
    assert out["ok"] is True
    assert len(out["sent"]) == 2
    assert out["sent_today"] == 2
    # Telegram was called per candidate with the right structure
    assert len(sent) == 2
    for msg in sent:
        assert "WOLF" in msg
        assert "Confidence" in msg
        assert "BUY SIGNAL" in msg
    # An INSERT must have been executed for each alert
    inserts = [s for s, _ in cur.executed if "INSERT INTO wolf_signal_alerts" in s]
    assert len(inserts) == 2


def test_signal_alert_check_requires_cron_secret_when_set(monkeypatch):
    """When CRON_SECRET is configured, missing/wrong header → 403."""
    monkeypatch.setenv("CRON_SECRET", "supersecret")
    from fastapi import HTTPException
    try:
        wolf_app.wolf_signal_alert_check(x_cron_secret="wrong")
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 403


# ── /api/wolf/predictions — buy_target / sell_target derivation ─────────

# ── /api/cron/signal-check — wraps signal-alert + records state ─────────

def test_cron_signal_check_delegates_and_records_state(monkeypatch):
    """cron_signal_check must call wolf_signal_alert_check and write
    last_signal_cron_ts + last_signal_cron_sent to ghost_state."""
    monkeypatch.setenv("CRON_SECRET", "")
    sent_messages = []

    # The underlying alert check pulls from db_conn + sends Telegram.
    # We patch both so the wrapper exercises its real code path.
    candidates = [
        (501, "BUY", 0.90, 60.0, 72.0, 56.0, int(time.time()) + 86400, int(time.time())),
    ]
    cur = _SignalAlertCursor(sent_today=0, candidates=candidates)
    _patch_signal_alert(monkeypatch, cur, sent_messages)

    out = wolf_app.cron_signal_check(x_cron_secret="")
    assert out["ok"] is True
    assert out["cron"] == "signal-check"
    assert out["ran_at"] > 0
    inner = out["alert_result"]
    assert inner["ok"] is True
    assert len(inner["sent"]) == 1
    assert len(sent_messages) == 1
    # The ghost_state writes happened (one for ts, one for sent count)
    state_writes = [s for s, _ in cur.executed if "ghost_state" in s and "INSERT" in s]
    assert len(state_writes) >= 2


def test_cron_signal_check_requires_cron_secret_when_set(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "supersecret")
    from fastapi import HTTPException
    try:
        wolf_app.cron_signal_check(x_cron_secret="wrong")
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 403


# ── /api/wolf/ghost-score — composite scoring ───────────────────────────

def test_ghost_score_pure_compute_strong_buy():
    """All maxed bullish inputs → score in STRONG_BUY band, signal label set."""
    import api.wolf_endpoints as we
    now = int(time.time())
    out = we.compute_ghost_score(
        latest_pick={"direction": "BUY", "confidence": 0.95, "predicted_at": now - 60},
        volume_ratio=2.5,
        sector={"signal": "wolf_lagging_up"},
        current_price=70.0,
        sma_5d=65.0,
        now_ts=now,
    )
    # model: 0.95 * 40 = 38; volume: min(20, 25) = 20; sector: 15; momentum: (5/65)=7.7% → 15; freshness: 10
    # total = 38 + 20 + 15 + 15 + 10 = 98 → STRONG_BUY
    assert out["score"] >= 95
    assert out["signal"] == "STRONG_BUY"
    assert set(out["components"]) == {"model", "volume", "sector", "momentum", "freshness"}


def test_ghost_score_pure_compute_strong_sell():
    """All maxed bearish inputs → score in STRONG_SELL band."""
    import api.wolf_endpoints as we
    now = int(time.time())
    out = we.compute_ghost_score(
        latest_pick={"direction": "SELL", "confidence": 0.95, "predicted_at": now - 60},
        volume_ratio=0.1,
        sector={"signal": None},
        current_price=60.0,
        sma_5d=65.0,
        now_ts=now,
    )
    # model: (1-0.95)*40 = 2; volume: 0.1*10 = 1; sector: 7.5; momentum: -7.7% → 0; freshness: 10
    # total = 2 + 1 + 7.5 + 0 + 10 = 20.5 → SELL (borderline; <20 is STRONG_SELL)
    assert out["score"] < 30
    assert out["signal"] in ("SELL", "STRONG_SELL")


def test_ghost_score_pure_compute_hold_when_no_inputs():
    """No data at all → neutral midpoint, HOLD signal."""
    import api.wolf_endpoints as we
    now = int(time.time())
    out = we.compute_ghost_score(
        latest_pick=None, volume_ratio=None, sector=None,
        current_price=None, sma_5d=None, now_ts=now,
    )
    # model: 20 (neutral); volume: 10; sector: 7.5; momentum: 7.5; freshness: 0
    # total = 45 → HOLD
    assert out["signal"] == "HOLD"
    assert 40 <= out["score"] <= 60


def test_ghost_score_freshness_decays_to_zero(monkeypatch):
    """A pick from > 48h ago contributes 0 freshness points."""
    import api.wolf_endpoints as we
    now = int(time.time())
    stale = we.compute_ghost_score(
        latest_pick={"direction": "BUY", "confidence": 0.9, "predicted_at": now - 72 * 3600},
        volume_ratio=2.0,
        sector={"signal": "wolf_lagging_up"},
        current_price=70.0,
        sma_5d=65.0,
        now_ts=now,
    )
    assert stale["components"]["freshness"] == 0.0


def test_ghost_score_signal_label_bands():
    """Signal label boundaries: 80/60/40/20 thresholds."""
    import api.wolf_endpoints as we
    assert we._signal_label(85) == "STRONG_BUY"
    assert we._signal_label(80) == "STRONG_BUY"
    assert we._signal_label(79.9) == "BUY"
    assert we._signal_label(60) == "BUY"
    assert we._signal_label(59.9) == "HOLD"
    assert we._signal_label(40) == "HOLD"
    assert we._signal_label(39.9) == "SELL"
    assert we._signal_label(20) == "SELL"
    assert we._signal_label(19.9) == "STRONG_SELL"
    assert we._signal_label(0) == "STRONG_SELL"


def test_wolf_predictions_buy_sell_target_derivation(monkeypatch):
    """BUY pick → buy_target=entry, sell_target=target. SELL pick → inverted."""
    import api.wolf_endpoints as we
    we._CACHE.clear()  # bypass the in-process cache between test runs

    now = int(time.time())
    rows = [
        # id, predicted_at, expires_at, resolved_at, direction, confidence,
        # entry_price, target_price, stop_price, outcome, pnl_pct
        (1, now - 3600, now + 82800, None, "BUY", 0.85, 58.5, 72.0, 54.0, None, None),
        (2, now - 7200, now + 79200, None, "SELL", 0.80, 70.0, 60.0, 75.0, None, None),
    ]
    cur = QueueCursor(fetchall_values=[rows])

    class _Conn:
        def cursor(self):
            return cur

    class _DbCtx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    import core.db as _db
    monkeypatch.setattr(_db, "db_conn", lambda: _DbCtx())

    import asyncio
    resp = asyncio.run(we.get_wolf_predictions(days=30, limit=100))
    import json
    body = json.loads(resp.body)
    assert body["ok"] is True
    preds = {p["id"]: p for p in body["predictions"]}
    # BUY pick: buy_target = entry (58.5), sell_target = target (72.0)
    assert preds[1]["buy_target"] == 58.5
    assert preds[1]["sell_target"] == 72.0
    # SELL pick: buy_target = target (60.0), sell_target = entry (70.0)
    assert preds[2]["buy_target"] == 60.0
    assert preds[2]["sell_target"] == 70.0


# ── _fetch_ohlcv — feed selection + period plumbing ─────────────────────

class _MockBarsResponse:
    def __init__(self, status_code=200, bars=None):
        self.status_code = status_code
        self._bars = bars or []

    def json(self):
        return {"bars": self._bars}


def test_fetch_ohlcv_uses_sip_first(monkeypatch):
    """Default feed must be SIP — IEX has no post-restructuring WOLF data."""
    import core.signal_engine as _se
    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    calls = []
    sip_bars = [{"t": "2026-01-02T00:00:00Z", "o": 60, "h": 62, "l": 59, "c": 61, "v": 1_000_000}]

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        if "feed=sip" in url:
            return _MockBarsResponse(200, sip_bars)
        return _MockBarsResponse(200, [])

    monkeypatch.setattr("requests.get", fake_get)
    rows = _se._fetch_ohlcv("WOLF", "stock")
    assert rows is not None
    assert len(rows) == 1
    assert rows[0]["close"] == 61.0
    # Only SIP should have been called (no fallback needed)
    assert len(calls) == 1 and "feed=sip" in calls[0]


def test_fetch_ohlcv_falls_back_to_iex_when_sip_empty(monkeypatch):
    """When SIP returns no rows (free-tier 403 or no data), retry on IEX."""
    import core.signal_engine as _se
    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    calls = []
    iex_bars = [{"t": "2026-01-02T00:00:00Z", "o": 5, "h": 6, "l": 4.5, "c": 5.5, "v": 500}]

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        if "feed=sip" in url:
            return _MockBarsResponse(200, [])   # SIP returns nothing
        if "feed=iex" in url:
            return _MockBarsResponse(200, iex_bars)
        return _MockBarsResponse(404, [])

    monkeypatch.setattr("requests.get", fake_get)
    rows = _se._fetch_ohlcv("AAPL", "stock")
    assert rows is not None
    assert len(rows) == 1
    assert rows[0]["close"] == 5.5
    # Both feeds tried, in correct order
    assert len(calls) == 2
    assert "feed=sip" in calls[0]
    assert "feed=iex" in calls[1]


def test_fetch_ohlcv_period_plumbed_into_start_date(monkeypatch):
    """period='1y' must send a start ~365 days ago; '2y' must send ~730."""
    import core.signal_engine as _se
    from datetime import datetime, timezone
    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    captured_urls = []

    def fake_get(url, headers=None, timeout=None):
        captured_urls.append(url)
        return _MockBarsResponse(200, [{"t": "2026-01-01T00:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}])

    monkeypatch.setattr("requests.get", fake_get)
    _se._fetch_ohlcv("WOLF", "stock", period="1y")
    _se._fetch_ohlcv("WOLF", "stock", period="2y")

    def _start_days_ago(url):
        import re
        m = re.search(r"start=(\d{4}-\d{2}-\d{2})T", url)
        assert m, f"no start in {url}"
        start = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - start).days

    # SIP succeeds on both calls → no IEX fallback → 2 URLs total
    assert len(captured_urls) == 2, f"expected 2 SIP calls, got: {captured_urls}"
    # ±2 day slack for clock drift / strftime rounding
    one_y = _start_days_ago(captured_urls[0])
    two_y = _start_days_ago(captured_urls[1])
    assert 363 <= one_y <= 367, f"1y should be ~365 days, got {one_y}"
    assert 728 <= two_y <= 732, f"2y should be ~730 days, got {two_y}"


# ── Training thresholds — env tunables ──────────────────────────────────

def test_min_train_rows_default_and_env_override(monkeypatch):
    """Default is 20; env override is honoured at call time."""
    import core.signal_engine as _se
    monkeypatch.delenv("MIN_TRAIN_ROWS", raising=False)
    assert _se._min_train_rows() == 20
    monkeypatch.setenv("MIN_TRAIN_ROWS", "5")
    assert _se._min_train_rows() == 5
    monkeypatch.setenv("MIN_TRAIN_ROWS", "0")  # floor at 1
    assert _se._min_train_rows() == 1


def test_min_backtest_bars_default_and_env_override(monkeypatch):
    """Default is 50; env override is honoured at call time."""
    import core.signal_engine as _se
    monkeypatch.delenv("MIN_BACKTEST_BARS", raising=False)
    assert _se._min_backtest_bars() == 50
    monkeypatch.setenv("MIN_BACKTEST_BARS", "30")
    assert _se._min_backtest_bars() == 30


def test_backtest_window_default_and_env_override(monkeypatch):
    """Default is 120; env override is honoured. Floor 20 prevents pathological values."""
    import core.signal_engine as _se
    monkeypatch.delenv("V3_BACKTEST_WINDOW", raising=False)
    assert _se._backtest_window() == 120
    monkeypatch.setenv("V3_BACKTEST_WINDOW", "60")
    assert _se._backtest_window() == 60
    monkeypatch.setenv("V3_BACKTEST_WINDOW", "5")  # floor at 20
    assert _se._backtest_window() == 20


def test_backtest_symbol_returns_empty_when_under_min_bars(monkeypatch):
    """backtest_symbol must bail out cleanly when feed returns fewer rows than the env floor."""
    import core.signal_engine as _se
    monkeypatch.setenv("MIN_BACKTEST_BARS", "100")
    monkeypatch.setattr(_se, "_fetch_ohlcv", lambda symbol, asset_type: [{"ts": "x", "close": 1.0}] * 90)
    out = _se.backtest_symbol("WOLF", "stock")
    assert out == []


def test_backtest_symbol_window_governs_sample_count(monkeypatch):
    """Smaller window must produce more labeled samples on the same input."""
    import core.signal_engine as _se
    # 200 deterministic bars — enough to label with either window
    rows = [{"ts": f"2026-01-{i:02d}", "open": 60.0 + i*0.1, "high": 61.0 + i*0.1,
             "low": 59.0 + i*0.1, "close": 60.0 + i*0.1, "volume": 100000} for i in range(1, 201)]
    monkeypatch.setattr(_se, "_fetch_ohlcv", lambda symbol, asset_type: rows)
    monkeypatch.setenv("MIN_BACKTEST_BARS", "10")
    monkeypatch.setenv("V3_BACKTEST_WINDOW", "120")
    out_120 = _se.backtest_symbol("WOLF", "stock")
    monkeypatch.setenv("V3_BACKTEST_WINDOW", "60")
    out_60 = _se.backtest_symbol("WOLF", "stock")
    # Smaller window → strictly more samples (60 vs 120 = +60 more iterations)
    assert len(out_60) > len(out_120) > 0
    assert len(out_60) - len(out_120) == 60


# ── _fetch_ohlcv yfinance fallback (PR fix/wolf-training-yfinance-fallback) ──

def test_fetch_ohlcv_falls_back_to_yfinance_when_both_alpaca_feeds_empty(monkeypatch):
    """Alpaca SIP empty + IEX empty → yfinance is the third tier. Without this
    fallback, post-restructure WOLF training is dead — Alpaca's IEX feed
    doesn't carry WOLF since it's NYSE-listed."""
    import core.signal_engine as _se
    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    alpaca_calls = []

    def fake_get(url, headers=None, timeout=None):
        alpaca_calls.append(url)
        return _MockBarsResponse(200, [])  # both SIP and IEX return empty

    monkeypatch.setattr("requests.get", fake_get)

    yfinance_called_with = {}

    def fake_yfinance(symbol, period):
        yfinance_called_with["symbol"] = symbol
        yfinance_called_with["period"] = period
        return [{"ts": "2025-12-01", "open": 60.0, "high": 62.0, "low": 59.0,
                 "close": 61.0, "volume": 1_000_000}]

    monkeypatch.setattr(_se, "_try_yfinance_ohlcv", fake_yfinance)
    rows = _se._fetch_ohlcv("WOLF", "stock")
    assert rows is not None
    assert len(rows) == 1
    assert rows[0]["close"] == 61.0
    # Both Alpaca feeds attempted before yfinance kicked in
    assert len(alpaca_calls) == 2 and "feed=sip" in alpaca_calls[0] and "feed=iex" in alpaca_calls[1]
    assert yfinance_called_with == {"symbol": "WOLF", "period": "1y"}


def test_try_yfinance_ohlcv_returns_none_when_yfinance_empty(monkeypatch):
    """yfinance .history() returning an empty DataFrame must produce None,
    not crash and not return a partial result. Guards against pandas
    quirks where an empty DataFrame might iterate zero rows but still
    have a truthy-ish presence."""
    import core.signal_engine as _se

    class _EmptyDF:
        empty = True

        def iterrows(self):
            return iter([])

    class _FakeTicker:
        def __init__(self, sym):
            pass

        def history(self, period=None, interval=None):
            return _EmptyDF()

    class _FakeYF:
        Ticker = _FakeTicker

    import sys
    monkeypatch.setitem(sys.modules, "yfinance", _FakeYF)
    out = _se._try_yfinance_ohlcv("WOLF", "1y")
    assert out is None


# ── v3_train WOLF-only filter (defense in depth like PR #7) ──────────────

def test_v3_train_collect_symbols_filters_to_wolf_only(monkeypatch):
    """Even with dirty STOCK_SYMBOLS env AND non-WOLF portfolio entries,
    training only ever runs on WOLF. Matches the WOLF-only hardening from
    PR #7 (scan_symbols and v3_status)."""
    monkeypatch.setenv("STOCK_SYMBOLS", "TSLA,META,WOLF,AMZN,T")
    # user_portfolio has 7 non-WOLF positions and 0 WOLF — should still resolve to WOLF only
    portfolio_rows = [("NVDA",), ("AAPL",), ("GOOG",), ("MSFT",), ("AMD",), ("INTC",), ("CRM",)]
    cur = QueueCursor(fetchall_values=[portfolio_rows])
    _patch_db_conn_with_cursor(monkeypatch, cur)
    out = wolf_app._v3_train_collect_symbols()
    assert out == [("WOLF", "stock")]


def test_v3_train_collect_symbols_falls_back_to_wolf_when_all_empty(monkeypatch):
    """Empty env + DB error → still returns [(WOLF, stock)] as final safety net."""
    monkeypatch.setenv("STOCK_SYMBOLS", "")

    class _BrokenCtx:
        def __enter__(self):
            raise RuntimeError("db down")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _BrokenCtx())
    out = wolf_app._v3_train_collect_symbols()
    assert out == [("WOLF", "stock")]


# ── Polygon fallback (PR fix/wolf-training-polygon-fallback) ─────────────

class _MockPolygonResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {"status": "OK", "results": []}

    def json(self):
        return self._body


def test_try_polygon_ohlcv_skipped_when_no_api_key(monkeypatch, caplog):
    """No POLYGON_API_KEY env → return None immediately, no HTTP call.

    Regression for PR #13: pre-fix the silent return left ops blind to
    why Polygon never appeared in production logs. Post-fix every code
    path must log so 'Polygon never ran' vs 'Polygon ran but returned
    None' is distinguishable.
    """
    import core.signal_engine as _se
    import logging
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    called = []
    monkeypatch.setattr("requests.get", lambda *a, **k: (called.append(a), _MockPolygonResponse())[1])
    with caplog.at_level(logging.INFO, logger="ghost.signal_v3"):
        assert _se._try_polygon_ohlcv("WOLF", "1y") is None
    assert called == []
    assert any("POLYGON_API_KEY not set" in r.message for r in caplog.records), \
        f"expected 'POLYGON_API_KEY not set' log, got: {[r.message for r in caplog.records]}"


def test_try_polygon_ohlcv_logs_when_status_ok_but_results_empty(monkeypatch, caplog):
    """status=OK with empty results array → log 'no bars in range' + return None."""
    import core.signal_engine as _se
    import logging
    monkeypatch.setenv("POLYGON_API_KEY", "polykey")
    monkeypatch.setattr("requests.get",
                        lambda *a, **k: _MockPolygonResponse(200, {"status": "OK", "results": []}))
    with caplog.at_level(logging.INFO, logger="ghost.signal_v3"):
        assert _se._try_polygon_ohlcv("WOLF", "1y") is None
    assert any("results=[]" in r.message or "no bars in range" in r.message
               for r in caplog.records), \
        f"expected empty-results log, got: {[r.message for r in caplog.records]}"


def test_try_polygon_ohlcv_parses_bars_into_standard_row_shape(monkeypatch):
    """Polygon response → list of {ts, open, high, low, close, volume}."""
    import core.signal_engine as _se
    monkeypatch.setenv("POLYGON_API_KEY", "polykey")
    polygon_body = {
        "status": "OK",
        "results": [
            {"t": 1_730_400_000_000, "o": 60.5, "h": 62.0, "l": 59.8, "c": 61.5, "v": 1_200_000},
            {"t": 1_730_486_400_000, "o": 61.5, "h": 63.0, "l": 61.0, "c": 62.5, "v": 1_100_000},
        ],
    }
    captured = {}

    def fake_get(url, timeout=None, **kwargs):
        captured["url"] = url
        return _MockPolygonResponse(200, polygon_body)

    monkeypatch.setattr("requests.get", fake_get)
    rows = _se._try_polygon_ohlcv("WOLF", "1y")
    assert rows is not None
    assert len(rows) == 2
    assert rows[0]["close"] == 61.5
    assert rows[1]["close"] == 62.5
    assert set(rows[0].keys()) == {"ts", "open", "high", "low", "close", "volume"}
    # URL is well-formed
    assert "api.polygon.io/v2/aggs/ticker/WOLF/range/1/day/" in captured["url"]
    assert "apiKey=polykey" in captured["url"]


def test_try_polygon_ohlcv_returns_none_on_non_ok_status(monkeypatch):
    """Polygon's 'NOT_AUTHORIZED' / 'ERROR' statuses → None, no rows."""
    import core.signal_engine as _se
    monkeypatch.setenv("POLYGON_API_KEY", "polykey")
    monkeypatch.setattr("requests.get",
                        lambda *a, **k: _MockPolygonResponse(200, {"status": "NOT_AUTHORIZED", "results": []}))
    assert _se._try_polygon_ohlcv("WOLF", "1y") is None


def test_fetch_ohlcv_uses_polygon_when_alpaca_feeds_empty(monkeypatch):
    """SIP empty + IEX empty → Polygon attempted before yfinance."""
    import core.signal_engine as _se
    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("POLYGON_API_KEY", "polykey")
    # Track call order
    order = []

    def fake_get(url, headers=None, timeout=None, **kwargs):
        if "alpaca.markets" in url:
            order.append("alpaca")
            return _MockBarsResponse(200, [])
        if "polygon.io" in url:
            order.append("polygon")
            return _MockPolygonResponse(200, {
                "status": "OK",
                "results": [{"t": 1_730_400_000_000, "o": 60, "h": 61, "l": 59, "c": 60.5, "v": 1_000_000}],
            })
        return _MockBarsResponse(404, [])

    monkeypatch.setattr("requests.get", fake_get)
    # yfinance must NOT be called when Polygon succeeds
    yfinance_called = []
    monkeypatch.setattr(_se, "_try_yfinance_ohlcv", lambda s, p: yfinance_called.append((s, p)) or None)

    rows = _se._fetch_ohlcv("WOLF", "stock")
    assert rows is not None and len(rows) == 1
    assert rows[0]["close"] == 60.5
    assert order == ["alpaca", "alpaca", "polygon"]  # SIP, IEX, then Polygon
    assert yfinance_called == []  # Polygon succeeded → yfinance skipped


# ── yfinance multi-strategy retry (PR fix/wolf-training-polygon-fallback) ──

class _DF:
    """Minimal DataFrame-ish object that exposes .empty and .iterrows()."""

    def __init__(self, rows):
        self._rows = rows
        self.empty = not rows

    def iterrows(self):
        for r in self._rows:
            yield r["ts"], r


def test_try_yfinance_ohlcv_retries_shorter_period_when_primary_empty(monkeypatch):
    """1y empty → falls through to 6mo, which has data → returns those rows."""
    import core.signal_engine as _se

    calls = []
    bar = {"ts": "2026-01-02", "Open": 60.0, "High": 62.0, "Low": 59.0, "Close": 61.0, "Volume": 100_000}

    class _Tk:
        def __init__(self, sym):
            pass

        def history(self, period=None, start=None, end=None, interval=None):
            calls.append(("period" if period else "explicit", period or (start, end)))
            if period == "1y":
                return _DF([])      # primary empty
            if period == "6mo":
                return _DF([bar])   # shorter period has data
            return _DF([])

    class _FakeYF:
        Ticker = _Tk

    import sys
    monkeypatch.setitem(sys.modules, "yfinance", _FakeYF)
    rows = _se._try_yfinance_ohlcv("WOLF", "1y")
    assert rows is not None and len(rows) == 1
    assert rows[0]["close"] == 61.0
    assert calls[0] == ("period", "1y")
    assert calls[1] == ("period", "6mo")


def test_try_yfinance_ohlcv_falls_through_to_explicit_dates(monkeypatch):
    """All period candidates empty → tries explicit start/end last."""
    import core.signal_engine as _se

    calls = []
    bar = {"ts": "2026-01-02", "Open": 60, "High": 62, "Low": 59, "Close": 61, "Volume": 100_000}

    class _Tk:
        def __init__(self, sym):
            pass

        def history(self, period=None, start=None, end=None, interval=None):
            calls.append(("period" if period else "explicit", period))
            if period:
                return _DF([])  # all periods empty
            return _DF([bar])   # start/end succeeds

    import sys
    monkeypatch.setitem(sys.modules, "yfinance", type("YF", (), {"Ticker": _Tk}))
    rows = _se._try_yfinance_ohlcv("WOLF", "1y")
    assert rows is not None and len(rows) == 1
    # Tried 1y, 6mo, 3mo, then explicit
    period_attempts = [c[1] for c in calls if c[0] == "period"]
    assert period_attempts == ["1y", "6mo", "3mo"]
    assert any(c[0] == "explicit" for c in calls)


def test_try_yfinance_ohlcv_returns_none_when_all_strategies_fail(monkeypatch):
    """Every strategy returns empty → final None, no crash."""
    import core.signal_engine as _se

    class _Tk:
        def __init__(self, sym):
            pass

        def history(self, period=None, start=None, end=None, interval=None):
            return _DF([])

    import sys
    monkeypatch.setitem(sys.modules, "yfinance", type("YF", (), {"Ticker": _Tk}))
    assert _se._try_yfinance_ohlcv("WOLF", "1y") is None


# ── Stooq fifth-tier fallback (PR fix/data-sources-stooq-and-diag) ──────

class _MockStooqResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


_STOOQ_HAPPY_CSV = (
    "Date,Open,High,Low,Close,Volume\n"
    "2026-05-19,71.20,72.50,70.80,71.85,1234567\n"
    "2026-05-20,71.85,73.10,71.50,72.95,1098765\n"
    "2026-05-21,72.95,74.20,72.40,73.80,1456789\n"
)


def test_try_stooq_ohlcv_parses_csv_into_standard_row_shape(monkeypatch):
    """Stooq CSV → list of {ts, open, high, low, close, volume}, in-window."""
    import core.signal_engine as _se
    captured = {}

    def fake_get(url, timeout=None, headers=None, **kwargs):
        captured["url"] = url
        captured["ua"] = (headers or {}).get("User-Agent")
        return _MockStooqResponse(200, _STOOQ_HAPPY_CSV)

    monkeypatch.setattr("requests.get", fake_get)
    rows = _se._try_stooq_ohlcv("WOLF", "1y")
    assert rows is not None
    assert len(rows) == 3
    assert rows[0]["close"] == 71.85
    assert rows[-1]["close"] == 73.80
    assert set(rows[0].keys()) == {"ts", "open", "high", "low", "close", "volume"}
    assert "stooq.com/q/d/l" in captured["url"]
    assert "s=wolf.us" in captured["url"]
    assert captured["ua"]  # set User-Agent (Stooq blocks default Python UA)


def test_try_stooq_ohlcv_returns_none_on_http_error(monkeypatch):
    import core.signal_engine as _se
    monkeypatch.setattr("requests.get",
                        lambda *a, **k: _MockStooqResponse(500, "Internal Server Error"))
    assert _se._try_stooq_ohlcv("WOLF", "1y") is None


def test_try_stooq_ohlcv_returns_none_when_no_data_body(monkeypatch):
    """Stooq returns text 'No data' for unknown tickers — must produce None."""
    import core.signal_engine as _se
    monkeypatch.setattr("requests.get",
                        lambda *a, **k: _MockStooqResponse(200, "No data\n"))
    assert _se._try_stooq_ohlcv("ZZZNOEXIST", "1y") is None


def test_try_stooq_ohlcv_skips_pre_cutoff_rows(monkeypatch):
    """Rows older than the lookback window must be filtered out."""
    import core.signal_engine as _se
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Mix of way-old and today rows; period='3m' = 90 days cutoff
    csv = (
        "Date,Open,High,Low,Close,Volume\n"
        "2015-01-02,10,11,9,10.5,1000\n"   # 11 years old — well before cutoff
        f"{today},71.20,72.50,70.80,71.85,1234567\n"
    )
    monkeypatch.setattr("requests.get",
                        lambda *a, **k: _MockStooqResponse(200, csv))
    rows = _se._try_stooq_ohlcv("WOLF", "3m")
    assert rows is not None
    assert len(rows) == 1   # only today's row passes cutoff
    assert rows[0]["close"] == 71.85


def test_fetch_ohlcv_chains_to_stooq_when_yfinance_empty(monkeypatch):
    """SIP empty + IEX empty + Polygon skipped + yfinance empty → Stooq called."""
    import core.signal_engine as _se
    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    # Alpaca always returns 200 OK with no bars
    monkeypatch.setattr("requests.get",
                        lambda url, **kwargs: _MockBarsResponse(200, [])
                        if "alpaca.markets" in str(url)
                        else _MockStooqResponse(200, _STOOQ_HAPPY_CSV))
    # yfinance returns nothing
    monkeypatch.setattr(_se, "_try_yfinance_ohlcv", lambda s, p: None)
    rows = _se._fetch_ohlcv("WOLF", "stock")
    assert rows is not None
    assert len(rows) == 3
    assert rows[0]["close"] == 71.85


# ── /api/diag/data-sources endpoint ─────────────────────────────────────

def test_diag_data_sources_requires_cron_secret_when_set(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "supersecret")
    from fastapi import HTTPException
    try:
        wolf_app.diag_data_sources(x_cron_secret="wrong")
        assert False, "expected 403"
    except HTTPException as e:
        assert e.status_code == 403


def test_diag_data_sources_reports_per_source_status(monkeypatch):
    """Each source is probed independently. Mix of success/failure surfaces
    in the results array with bar counts, errors, and latency."""
    monkeypatch.setenv("CRON_SECRET", "")
    import core.signal_engine as _se

    fake_bars = [{"ts": "2026-01-01T00:00:00Z", "open": 60, "high": 61,
                  "low": 59, "close": 60.5, "volume": 1000}]
    monkeypatch.setattr(_se, "_try_polygon_ohlcv", lambda s, p: None)
    monkeypatch.setattr(_se, "_try_yfinance_ohlcv", lambda s, p: fake_bars)
    monkeypatch.setattr(_se, "_try_stooq_ohlcv", lambda s, p: None)

    out = wolf_app.diag_data_sources(x_cron_secret="", symbol="WOLF", period="1y")
    assert out["ok"] is True
    assert out["symbol"] == "WOLF"
    sources = {r["source"]: r for r in out["results"]}
    assert sources["polygon"]["ok"] is False
    assert sources["yfinance"]["ok"] is True
    assert sources["yfinance"]["bars"] == 1
    assert sources["yfinance"]["first_ts"] == "2026-01-01T00:00:00Z"
    assert sources["stooq"]["ok"] is False
    assert out["summary"]["working"] == ["yfinance"]
    assert set(out["summary"]["broken"]) == {"polygon", "stooq"}
