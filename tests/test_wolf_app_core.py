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
