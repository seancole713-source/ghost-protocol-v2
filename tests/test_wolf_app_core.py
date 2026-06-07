import json
import time

import pytest

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
    # audit v2 #10: /api/health is now a SLIM liveness probe (status/score/ts),
    # not the full internals dict.
    monkeypatch.setattr(wolf_app, "health", lambda: {
        "status": "healthy", "score": 100, "telegram_configured": True, "tasks": [1]})
    out = wolf_app.api_health()
    assert out["status"] == "healthy" and out["score"] == 100
    assert "telegram_configured" not in out and "tasks" not in out


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

    ctx_factory = lambda: _DbCtx()
    monkeypatch.setattr(wolf_app, "db_conn", ctx_factory)
    try:
        import core.db as _cdb
        monkeypatch.setattr(_cdb, "db_conn", ctx_factory)
    except Exception:
        pass


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

def test_scan_symbols_reflects_configured_stocks(monkeypatch):
    """scan_symbols should expose configured STOCK_SYMBOLS for cockpit visibility."""
    monkeypatch.setenv("STOCK_SYMBOLS", "TSLA,META,WOLF,AMZN,T")
    monkeypatch.setenv("V3_STATS_START_TS", "0")
    monkeypatch.delenv("V3_STATS_START_TS", raising=False)
    monkeypatch.setattr(wolf_app, "_v32_stats_start_ts", lambda cur: 0)
    cur = QueueCursor(fetchall_values=[[]], fetchone_values=[(0,)])
    payload = wolf_app._compute_get_stats(cur)
    assert payload["scan_symbols"]["stocks"] == ["TSLA", "META", "WOLF", "AMZN", "T"]


def test_scan_symbols_falls_back_to_wolf_when_env_empty(monkeypatch):
    monkeypatch.setenv("STOCK_SYMBOLS", "")
    monkeypatch.setattr(wolf_app, "_v32_stats_start_ts", lambda cur: 0)
    cur = QueueCursor(fetchall_values=[[]], fetchone_values=[(0,)])
    payload = wolf_app._compute_get_stats(cur)
    assert payload["scan_symbols"]["stocks"] == ["WOLF"]


def test_v3_status_keeps_full_model_set(monkeypatch):
    """v3 status should expose all trained symbols, not only WOLF."""
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
    assert out["models"] == 4
    assert set(out["symbols"].keys()) == {"WOLF", "BCH", "SOL", "UNI"}


def test_v3_status_reports_missing_expected_models(monkeypatch):
    import core.signal_engine as _se
    fake = {"trained": True, "models": 1, "symbols": {"BCH": {"engine": "v3.0"}}}
    monkeypatch.setattr(_se, "get_model_status", lambda: fake)
    out = wolf_app.v3_status()
    assert out["trained"] is True
    assert "WOLF" in out.get("watchlist_missing_models", [])


def test_v3_status_system_health_block_healthy(monkeypatch):
    """The system block rolls up engine/kill/coverage/activity/pnl into a
    healthy snapshot when every signal is nominal."""
    import core.signal_engine as _se, core.prediction as _pred
    monkeypatch.setenv("STOCK_SYMBOLS", "WOLF")
    monkeypatch.setattr(_pred, "STOCK_SYMBOLS", ["WOLF"])
    monkeypatch.setattr(_se, "get_model_status",
                        lambda: {"trained": True, "models": 3, "symbols": {"WOLF": {"accuracy": 70.0}}})
    monkeypatch.setattr(_pred, "engine_pause_state", lambda: {"paused": False})
    monkeypatch.setattr(_pred, "evaluate_kill_conditions",
                        lambda: {"enabled": True, "any_triggered": False, "resolved_available": 4})
    monkeypatch.setenv("MODEL_COVERAGE_MIN_MODELS", "1")

    gs_rows = [("last_prediction_cycle_ts", str(int(time.time()) - 120)),
               ("last_prediction_cycle_saved", "0"),
               ("last_prediction_cycle_scanned", "1")]
    resolved = [(100, "WOLF", "WIN", 10.0, 10.0, 11.0),
                (200, "WOLF", "LOSS", -5.0, 11.0, 10.45)]

    class _Cur:
        last = ""
        def execute(self, sql, params=None): self.last = sql
        def fetchall(self):
            if "ghost_state" in self.last: return gs_rows
            if "ORDER BY resolved_at" in self.last: return resolved
            return []
        def fetchone(self):
            if "COUNT(*)" in self.last and "outcome IS NULL" in self.last: return (2,)
            if "COUNT(*)" in self.last and "resolved_at >=" in self.last: return (5,)
            return (0,)

    class _Conn:
        def cursor(self): return _Cur()

    class _DbCtx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _DbCtx())
    out = wolf_app.v3_status()
    assert out["trained"] is True
    sysh = out["system"]
    assert sysh["status"] == "healthy"
    assert sysh["db_ok"] is True
    assert sysh["issues"] == []
    assert sysh["engine"]["paused"] is False
    assert sysh["engine"]["last_cycle"]["scanned"] == 1
    assert sysh["kill"]["any_triggered"] is False
    assert sysh["coverage"]["below_floor"] is False
    assert sysh["activity"]["open"] == 2
    assert sysh["activity"]["resolved_24h"] == 5
    assert sysh["pnl"]["count"] == 2
    assert abs(sysh["pnl"]["realized_pnl_usd"] - 45.0) < 1e-6


def test_v3_status_system_health_degrades_on_pause(monkeypatch):
    """Engine pause + kill trip surface as issues and downgrade the roll-up."""
    import core.signal_engine as _se, core.prediction as _pred
    monkeypatch.setattr(_se, "get_model_status",
                        lambda: {"trained": True, "models": 3, "symbols": {"WOLF": {}}})
    monkeypatch.setattr(_pred, "engine_pause_state",
                        lambda: {"paused": True, "reason": "win_rate->auto_pause"})
    monkeypatch.setattr(_pred, "evaluate_kill_conditions",
                        lambda: {"enabled": True, "any_triggered": True, "resolved_available": 40})
    monkeypatch.setenv("MODEL_COVERAGE_MIN_MODELS", "1")

    class _Cur:
        last = ""
        def execute(self, sql, params=None): self.last = sql
        def fetchall(self): return []
        def fetchone(self): return (0,)

    class _Conn:
        def cursor(self): return _Cur()

    class _DbCtx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _DbCtx())
    sysh = wolf_app.v3_status()["system"]
    assert sysh["status"] == "degraded"
    assert "engine_paused" in sysh["issues"]
    assert "kill_condition_triggered" in sysh["issues"]
    assert sysh["engine"]["pause_reason"] == "win_rate->auto_pause"


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


def test_ghost_score_freshness_uses_scan_not_pick(monkeypatch):
    """Freshness keys to last scan, not last pick: a stale pick (>48h) with a
    recent scan cycle still scores full freshness — silence-by-design no longer
    drags the Ghost Score down."""
    import api.wolf_endpoints as we
    now = int(time.time())
    out = we.compute_ghost_score(
        latest_pick={"direction": "BUY", "confidence": 0.9, "predicted_at": now - 72 * 3600},
        volume_ratio=2.0,
        sector={"signal": "wolf_lagging_up"},
        current_price=70.0,
        sma_5d=65.0,
        now_ts=now,
        last_scan_ts=now - 14 * 60,   # scanned 14 min ago
    )
    assert out["components"]["freshness"] == 10.0


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


# ── audit §3: rule-based market regime + Ghost Score modifier ───────────

def test_classify_regime_rules():
    from core.regime import classify_regime
    # +7.7% over SMA, high volume => Strong Uptrend, boost modifier
    r = classify_regime(70.0, 65.0, volume_ratio=2.0)
    assert r["label"] == "Strong Uptrend"
    assert r["modifier"] == 1.10
    assert r["delta_pct"] == 7.69
    # +7.7% but normal volume => plain Uptrend
    assert classify_regime(70.0, 65.0, volume_ratio=1.0)["label"] == "Uptrend"
    # +1.5% => Uptrend
    assert classify_regime(101.5, 100.0)["label"] == "Uptrend"
    # within +/-1% => Choppy, neutral modifier
    flat = classify_regime(100.5, 100.0)
    assert flat["label"] == "Choppy" and flat["modifier"] == 1.0
    # -2% => Downtrend; -5% => Strong Downtrend
    assert classify_regime(98.0, 100.0)["label"] == "Downtrend"
    assert classify_regime(95.0, 100.0)["label"] == "Strong Downtrend"
    # missing data => Unknown, neutral
    u = classify_regime(None, None)
    assert u["label"] == "Unknown" and u["modifier"] == 1.0 and u["delta_pct"] is None


def test_ghost_score_applies_regime_modifier():
    """Same inputs, different regimes: bearish downgrades, bullish boosts, and
    raw_score stays the pre-modifier value."""
    import api.wolf_endpoints as we
    from core.regime import classify_regime
    now = int(time.time())
    base = dict(
        latest_pick={"direction": "BUY", "confidence": 0.80, "predicted_at": now - 60},
        volume_ratio=1.0, sector={"signal": None},
        current_price=50.0, sma_5d=50.0, now_ts=now,
    )
    # raw: model 32 + volume 10 + sector 7.5 + momentum 7.5 + freshness 10 = 67
    neutral = we.compute_ghost_score(**base, regime={"modifier": 1.0})
    assert neutral["raw_score"] == 67.0
    assert neutral["score"] == 67.0

    down = we.compute_ghost_score(**base, regime=classify_regime(49.0, 50.0))  # -2% => Downtrend ×0.90
    assert down["raw_score"] == 67.0
    assert abs(down["score"] - 60.3) < 1e-6        # 67 * 0.90
    assert down["regime"]["label"] == "Downtrend"

    up = we.compute_ghost_score(**base, regime=classify_regime(53.0, 50.0, volume_ratio=2.0))  # Strong Uptrend ×1.10
    assert abs(up["score"] - 73.7) < 1e-6          # 67 * 1.10

    # modifier never pushes the score past 100
    maxed = we.compute_ghost_score(
        latest_pick={"direction": "BUY", "confidence": 0.99, "predicted_at": now - 60},
        volume_ratio=3.0, sector={"signal": "wolf_lagging_up"},
        current_price=60.0, sma_5d=50.0, now_ts=now,
        regime=classify_regime(60.0, 50.0, volume_ratio=3.0),
    )
    assert maxed["score"] <= 100.0


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


# ── v3_train watchlist collection ──────────────────────────────────────────

def test_v3_train_collect_symbols_uses_env_watchlist_only(monkeypatch):
    """Training symbols match configured STOCK_SYMBOLS (portfolio does not expand watchlist)."""
    monkeypatch.setenv("STOCK_SYMBOLS", "TSLA,META,WOLF,AMZN,T")
    portfolio_rows = [("NVDA",), ("AAPL",)]
    cur = QueueCursor(fetchall_values=[portfolio_rows])
    _patch_db_conn_with_cursor(monkeypatch, cur)
    out = wolf_app._v3_train_collect_symbols()
    assert out == [
        ("TSLA", "stock"),
        ("META", "stock"),
        ("WOLF", "stock"),
        ("AMZN", "stock"),
        ("T", "stock"),
    ]


def test_v3_train_collect_symbols_falls_back_to_official_when_empty(monkeypatch):
    """Empty env uses the code-defined official watchlist."""
    monkeypatch.setenv("STOCK_SYMBOLS", "")

    class _BrokenCtx:
        def __enter__(self):
            raise RuntimeError("db down")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _BrokenCtx())
    out = wolf_app._v3_train_collect_symbols()
    from config.symbols import OFFICIAL_WATCHLIST
    assert out == [(sym, "stock") for sym in OFFICIAL_WATCHLIST]


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


# ── v3_train force param + state tracking + /api/v3/train/last ───────────

class _StateCursor:
    """Cursor that captures ghost_state INSERTs and serves SELECTs from them."""

    def __init__(self):
        self.state = {}
        self.executed = []
        self.last_sql = ""

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.executed.append((sql, params))
        # Handle the upsert INSERT pattern used by _record_v3_train_state
        if "INSERT INTO ghost_state" in sql and params and len(params) == 2:
            self.state[params[0]] = params[1]

    def fetchall(self):
        if "SELECT key, val FROM ghost_state WHERE key LIKE 'last_v3_train_%'" in self.last_sql:
            return [(k, v) for k, v in self.state.items() if k.startswith("last_v3_train_")]
        return []

    def fetchone(self):
        return None


def _patch_state_cursor(monkeypatch):
    cur = _StateCursor()

    class _Conn:
        def cursor(self):
            return cur

    class _DbCtx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _DbCtx())
    return cur


def test_record_v3_train_state_upserts_each_field(monkeypatch):
    """Each field becomes a separate ghost_state row keyed last_v3_train_<name>."""
    cur = _patch_state_cursor(monkeypatch)
    wolf_app._record_v3_train_state(ts=12345, state="started", accuracy=None)
    # 1 CREATE TABLE + 3 INSERTs (one per field)
    inserts = [e for e in cur.executed if e[0].startswith("INSERT INTO ghost_state")]
    assert len(inserts) == 3
    assert cur.state["last_v3_train_ts"] == "12345"
    assert cur.state["last_v3_train_state"] == "started"
    assert cur.state["last_v3_train_accuracy"] == ""  # None → ""


def test_v3_train_accepts_force_flag_and_starts_thread(monkeypatch):
    """v3_train with force=True returns ok=true, includes force in response,
    writes 'started' state immediately (before the bg thread runs)."""
    monkeypatch.setenv("CRON_SECRET", "")
    cur = _patch_state_cursor(monkeypatch)

    # Block the background thread from doing real work
    class _FakeThread:
        def __init__(self, target=None, daemon=None):
            self.target = target
        def start(self):
            pass  # never run the actual training
    import threading as _th
    monkeypatch.setattr(_th, "Thread", _FakeThread)

    out = wolf_app.v3_train(x_cron_secret="", force=True)
    assert out["ok"] is True
    assert out["force"] is True
    assert out["started_at"] > 0
    # The 'started' phase write happened synchronously before the thread
    assert cur.state["last_v3_train_state"] == "started"
    assert cur.state["last_v3_train_force"] == "true"


def test_v3_train_last_endpoint_returns_parsed_state(monkeypatch):
    """/api/v3/train/last surfaces all last_v3_train_* fields with
    numeric/boolean coercion. Reads from ghost_state."""
    cur = _patch_state_cursor(monkeypatch)
    cur.state.update({
        "last_v3_train_ts": "1779470000",
        "last_v3_train_state": "passed",
        "last_v3_train_accuracy": "0.6234",
        "last_v3_train_passed": "true",
        "last_v3_train_force": "true",
        "last_v3_train_models_before": "0",
        "last_v3_train_models_after": "1",
        "last_v3_train_finished_at": "1779470120",
    })
    out = wolf_app.v3_train_last()
    assert out["ok"] is True
    last = out["last"]
    assert last["state"] == "passed"
    assert last["accuracy"] == 0.6234        # coerced to float
    assert last["passed"] is True            # coerced to bool
    assert last["force"] is True
    assert last["ts"] == 1779470000          # coerced to int
    assert last["models_before"] == 0
    assert last["models_after"] == 1


def test_v3_train_last_endpoint_returns_none_when_no_history(monkeypatch):
    """No prior train invocations → last=None, no crash."""
    _patch_state_cursor(monkeypatch)
    out = wolf_app.v3_train_last()
    assert out["ok"] is True
    assert out["last"] is None


# ── /api/_version + /api/v3/train/sync (PR #19) ─────────────────────────

def test_deploy_version_exposes_pr_marker_and_endpoint_inventory():
    """/api/_version returns the running PR marker and the endpoint inventory.
    Lets the operator verify code freshness from a single curl."""
    out = wolf_app.deploy_version()
    assert out["ok"] is True
    assert out["_pr_version"] == 50
    assert isinstance(out["endpoints_present"], dict)
    # All of the recent endpoint flags must be present and true
    expected = {"v3_train_force_param", "v3_train_last", "v3_train_sync",
                "diag_data_sources", "wolf_signal_alert_check"}
    assert expected.issubset(set(out["endpoints_present"].keys()))
    for ep, present in out["endpoints_present"].items():
        assert present is True, f"{ep} marked False"


def test_v3_train_sync_returns_actual_result_with_pr_version(monkeypatch):
    """v3_train_sync calls train_and_validate inline and returns the result
    directly. _pr_version marker is included so the client can detect
    stale deploys at-a-glance."""
    monkeypatch.setenv("CRON_SECRET", "")
    cur = _patch_state_cursor(monkeypatch)

    # Mock train_and_validate to return a known-passing result
    import core.signal_engine as _se
    monkeypatch.setattr(_se, "train_and_validate", lambda stocks: (None, 0.6234, True))
    monkeypatch.setattr(_se, "get_model_status",
                        lambda: {"trained": True, "models": 1, "symbols": {"WOLF": {}}})
    monkeypatch.setattr(wolf_app, "_bump_cockpit_db_cache", lambda: None)
    monkeypatch.setattr(wolf_app, "_auto_purge_bad_models", lambda: 0)
    monkeypatch.setattr(wolf_app, "_purge_v3_stale_or_weak", lambda: 0)

    out = wolf_app.v3_train_sync(x_cron_secret="", force=True)
    assert out["ok"] is True
    assert out["_pr_version"] == 50
    assert out["passed"] is True
    assert out["accuracy"] == 62.34
    assert "stocks" in out
    assert out["models_after"] == 1
    # State was recorded for both 'started' AND 'passed' phases
    assert cur.state["last_v3_train_state"] == "passed"
    assert cur.state["last_v3_train_passed"] == "true"


def test_v3_train_sync_returns_500_with_error_on_exception(monkeypatch):
    """Exception inside train_and_validate → 500 response with the error
    string surfaced + state=exception recorded."""
    monkeypatch.setenv("CRON_SECRET", "")
    cur = _patch_state_cursor(monkeypatch)
    import core.signal_engine as _se
    monkeypatch.setattr(_se, "train_and_validate",
                        lambda stocks: (_ for _ in ()).throw(RuntimeError("xgboost died")))
    monkeypatch.setattr(_se, "get_model_status",
                        lambda: {"trained": False, "models": 0, "symbols": {}})

    resp = wolf_app.v3_train_sync(x_cron_secret="", force=False)
    # Returns a JSONResponse on error path; we need to inspect the body
    import json
    body = json.loads(resp.body)
    assert resp.status_code == 500
    assert body["ok"] is False
    assert "xgboost died" in body["error"]
    assert body["_pr_version"] == 50
    assert cur.state["last_v3_train_state"] == "exception"
    assert "xgboost died" in cur.state["last_v3_train_error"]


def test_v3_train_sync_requires_cron_secret_when_set(monkeypatch):
    """403 on missing/wrong header when CRON_SECRET configured."""
    monkeypatch.setenv("CRON_SECRET", "supersecret")
    resp = wolf_app.v3_train_sync(x_cron_secret="wrong", force=True)
    import json
    body = json.loads(resp.body)
    assert resp.status_code == 403
    assert body["ok"] is False


# ── PR #20: per-symbol gate detail surfacing ────────────────────────────

def test_persist_train_details_writes_json_to_ghost_state(monkeypatch):
    """_persist_train_details serialises the details list and upserts it
    into ghost_state.last_train_details so v3_train_sync can read it."""
    import core.signal_engine as _se
    executed = []

    class _Cur:
        def execute(self, sql, params=None):
            executed.append((sql, params))

        def fetchall(self): return []
        def fetchone(self): return None

    class _Conn:
        def cursor(self): return _Cur()

    class _DbCtx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    import core.db
    monkeypatch.setattr(core.db, "db_conn", lambda: _DbCtx())

    _se._persist_train_details([
        {"symbol": "WOLF", "passed": False, "fail_reason": "holdout_acc < 55.0% (52.0%)",
         "stage": "trained", "n_samples": 126, "holdout_acc": 0.52},
    ])
    # Find the INSERT — SQL literal contains 'last_train_details', params has the JSON
    insert_calls = [e for e in executed if "last_train_details" in e[0]]
    assert len(insert_calls) == 1
    insert_sql, insert_params = insert_calls[0]
    assert insert_params is not None and len(insert_params) >= 1
    import json as _json
    payload = _json.loads(insert_params[0])
    assert "ts" in payload
    assert len(payload["symbols"]) == 1
    assert payload["symbols"][0]["symbol"] == "WOLF"
    assert payload["symbols"][0]["fail_reason"] == "holdout_acc < 55.0% (52.0%)"


def test_v3_train_sync_includes_train_details_in_response(monkeypatch):
    """v3_train_sync reads ghost_state.last_train_details and surfaces it
    inside its response under 'train_details' so the cockpit can render
    per-symbol gate metrics without needing a separate fetch."""
    monkeypatch.setenv("CRON_SECRET", "")

    # Cursor that returns canned ghost_state.last_train_details row
    detail_payload = {
        "ts": 1779470000,
        "symbols": [
            {"symbol": "WOLF", "passed": False,
             "fail_reason": "holdout_acc < 55.0% (52.0%)",
             "stage": "trained", "n_samples": 126,
             "holdout_acc": 0.52, "edge": 0.04,
             "thresholds": {"min_holdout_acc": 0.55, "min_edge": 0.05}},
        ],
    }

    class _DetailCursor:
        def __init__(self):
            self.last_sql = ""
            self.state = {}

        def execute(self, sql, params=None):
            self.last_sql = sql
            if "INSERT INTO ghost_state" in sql and params and len(params) == 2:
                self.state[params[0]] = params[1]

        def fetchone(self):
            if "SELECT val FROM ghost_state WHERE key='last_train_details'" in self.last_sql:
                import json as _json
                return (_json.dumps(detail_payload),)
            return None

        def fetchall(self):
            return []

    cur = _DetailCursor()

    class _Conn:
        def cursor(self): return cur

    class _DbCtx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _DbCtx())

    import core.signal_engine as _se
    monkeypatch.setattr(_se, "train_and_validate", lambda stocks: (None, 0.0, False))
    monkeypatch.setattr(_se, "get_model_status",
                        lambda: {"trained": False, "models": 0, "symbols": {}})
    monkeypatch.setattr(wolf_app, "_bump_cockpit_db_cache", lambda: None)
    monkeypatch.setattr(wolf_app, "_auto_purge_bad_models", lambda: 0)
    monkeypatch.setattr(wolf_app, "_purge_v3_stale_or_weak", lambda: 0)

    out = wolf_app.v3_train_sync(x_cron_secret="", force=True)
    assert out["ok"] is True
    assert out["passed"] is False
    assert out["train_details"] is not None
    assert len(out["train_details"]["symbols"]) == 1
    wolf_detail = out["train_details"]["symbols"][0]
    assert wolf_detail["symbol"] == "WOLF"
    assert wolf_detail["fail_reason"] == "holdout_acc < 55.0% (52.0%)"
    assert wolf_detail["holdout_acc"] == 0.52


# ── PR #21: walk-forward fold floors are env-tunable ────────────────────

def test_v3_wf_min_train_floor_default_and_env(monkeypatch):
    """V3_WF_MIN_TRAIN default 60, env override honoured, floor 20."""
    import core.signal_engine as _se
    monkeypatch.delenv("V3_WF_MIN_TRAIN", raising=False)
    assert _se._v3_wf_min_train_floor() == 60
    monkeypatch.setenv("V3_WF_MIN_TRAIN", "80")
    assert _se._v3_wf_min_train_floor() == 80
    monkeypatch.setenv("V3_WF_MIN_TRAIN", "5")  # below absolute safety floor
    assert _se._v3_wf_min_train_floor() == 20


def test_v3_wf_test_size_floor_default_and_env(monkeypatch):
    """V3_WF_TEST_SIZE default 15, env override, floor 5."""
    import core.signal_engine as _se
    monkeypatch.delenv("V3_WF_TEST_SIZE", raising=False)
    assert _se._v3_wf_test_size_floor() == 15
    monkeypatch.setenv("V3_WF_TEST_SIZE", "25")
    assert _se._v3_wf_test_size_floor() == 25
    monkeypatch.setenv("V3_WF_TEST_SIZE", "1")
    assert _se._v3_wf_test_size_floor() == 5


def _install_fake_xgboost(monkeypatch):
    """Stub xgboost.XGBClassifier + sklearn.metrics.accuracy_score so WF
    tests run without the heavy ML deps installed in the sandbox."""
    import sys, types
    import numpy as np

    class _StubModel:
        def __init__(self, **k): pass
        def fit(self, X, y, sample_weight=None): return self   # mirror real XGB API
        def predict(self, X):
            return np.zeros(len(X))

    fake_xgb = types.ModuleType("xgboost")
    fake_xgb.XGBClassifier = _StubModel
    monkeypatch.setitem(sys.modules, "xgboost", fake_xgb)

    fake_sklearn = types.ModuleType("sklearn")
    fake_metrics = types.ModuleType("sklearn.metrics")
    fake_metrics.accuracy_score = lambda y_true, y_pred: float(np.mean(
        np.asarray(y_true) == np.asarray(y_pred)
    ))
    fake_sklearn.metrics = fake_metrics
    monkeypatch.setitem(sys.modules, "sklearn", fake_sklearn)
    monkeypatch.setitem(sys.modules, "sklearn.metrics", fake_metrics)


def test_walk_forward_produces_folds_for_127_sample_input(monkeypatch):
    """Regression for the WOLF case: n=127 must produce >=3 folds with the
    new defaults (60 / 15). Previously hardcoded 120/20 gave zero folds."""
    import core.signal_engine as _se
    import numpy as np
    for k in ("V3_WF_MIN_TRAIN", "V3_WF_TEST_SIZE",
              "V3_WF_MIN_TRAIN_FRAC", "V3_WF_TEST_FRAC"):
        monkeypatch.delenv(k, raising=False)
    _install_fake_xgboost(monkeypatch)

    X = np.zeros((127, 3))
    y = np.array([0, 1] * 63 + [0])
    out = _se._walk_forward_scores(X, y)
    assert out["fold_count"] >= 3, f"expected >=3 folds, got {out['fold_count']}"


def test_walk_forward_zero_folds_when_n_below_train_floor(monkeypatch):
    """Pathologically small input returns zero folds gracefully."""
    import core.signal_engine as _se
    import numpy as np
    monkeypatch.delenv("V3_WF_MIN_TRAIN", raising=False)
    _install_fake_xgboost(monkeypatch)
    out = _se._walk_forward_scores(np.zeros((10, 3)), np.zeros(10))
    assert out == {"fold_count": 0, "acc_mean": 0.0, "acc_min": 0.0,
                   "edge_mean": 0.0, "edge_min": 0.0}


def test_walk_forward_respects_env_min_train_override(monkeypatch):
    """Setting V3_WF_MIN_TRAIN higher than n means zero folds."""
    import core.signal_engine as _se
    import numpy as np
    monkeypatch.setenv("V3_WF_MIN_TRAIN", "200")
    _install_fake_xgboost(monkeypatch)
    X = np.zeros((127, 3))
    y = np.array([0, 1] * 63 + [0])
    out = _se._walk_forward_scores(X, y)
    assert out["fold_count"] == 0


def test_walk_forward_pools_peers_into_training_only(monkeypatch):
    """W1: peers expand each fold's TRAINING set (with a matching sample_weight
    vector); the test window stays target-only. Verifies the gate validates the
    deployed pooled model, not a WOLF-only one."""
    import core.signal_engine as _se
    import numpy as np
    import sys, types
    for k in ("V3_WF_MIN_TRAIN", "V3_WF_TEST_SIZE", "V3_WF_MIN_TRAIN_FRAC", "V3_WF_TEST_FRAC"):
        monkeypatch.delenv(k, raising=False)

    seen = []

    class _Capture:
        def __init__(self, **k): pass
        def fit(self, X, y, sample_weight=None):
            seen.append((len(X), None if sample_weight is None else len(sample_weight)))
            return self
        def predict(self, X):
            return np.zeros(len(X))

    fake_xgb = types.ModuleType("xgboost"); fake_xgb.XGBClassifier = _Capture
    monkeypatch.setitem(sys.modules, "xgboost", fake_xgb)
    fake_sklearn = types.ModuleType("sklearn"); fake_metrics = types.ModuleType("sklearn.metrics")
    fake_metrics.accuracy_score = lambda a, b: 0.6
    fake_sklearn.metrics = fake_metrics
    monkeypatch.setitem(sys.modules, "sklearn", fake_sklearn)
    monkeypatch.setitem(sys.modules, "sklearn.metrics", fake_metrics)

    X = np.zeros((127, 3)); y = np.array([0, 1] * 63 + [0])
    X_peer = np.ones((40, 3)); y_peer = np.array([0, 1] * 20)
    out = _se._walk_forward_scores(X, y, X_peer, y_peer, 3.0)
    assert out["fold_count"] >= 3
    assert seen, "model.fit was never called"
    for n_train, n_sw in seen:
        assert n_sw == n_train          # one weight per training row
        assert n_train >= 60 + 40       # WOLF prefix (>=min_train) + 40 pooled peers


# ── PR #22: ops polish (purge non-WOLF / telegram status / freshness) ───

def test_delete_model_non_wolf_only_purges_other_symbols(monkeypatch):
    """non_wolf_only=true deletes every model whose symbol isn't WOLF and
    keeps the WOLF row regardless of its accuracy."""
    import json as _j
    # delete_model uses strict=True cron-gate; empty secret rejects.
    monkeypatch.setenv("CRON_SECRET", "testsecret")

    rows = [
        ("meta_WOLF", _j.dumps({"accuracy": 0.65})),
        ("meta_BCH",  _j.dumps({"accuracy": 0.99})),  # would normally be kept on accuracy alone
        ("meta_SOL",  _j.dumps({"accuracy": 0.30})),
        ("meta_UNI",  _j.dumps({"accuracy": 0.30})),
    ]

    class _Cur:
        def __init__(self):
            self.executed = []
            self._rows_returned = False

        def execute(self, sql, params=None):
            self.executed.append((sql, params))

        def fetchall(self):
            # Return the meta rows on the SELECT call (first fetchall in delete_model)
            if not self._rows_returned:
                self._rows_returned = True
                return rows
            return []

        def fetchone(self):
            return None

    cur = _Cur()

    class _Conn:
        def cursor(self):
            return cur

    class _DbCtx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    import core.db
    monkeypatch.setattr(core.db, "db_conn", lambda: _DbCtx())
    monkeypatch.setattr(wolf_app, "db_conn", lambda: _DbCtx())

    import asyncio
    out = asyncio.run(wolf_app.delete_model(x_cron_secret="testsecret", non_wolf_only=True))
    assert out["ok"] is True
    assert out["mode"] == "non_wolf_only"
    assert "WOLF(WOLF)" in out["kept"]
    assert set(out["deleted"]) == {"BCH(non-WOLF)", "SOL(non-WOLF)", "UNI(non-WOLF)"}
    deletes = [e for e in cur.executed if "DELETE FROM ghost_v3_model" in e[0]]
    assert len(deletes) == 3


def test_delete_model_default_mode_still_uses_accuracy_floor(monkeypatch):
    """Without non_wolf_only the legacy accuracy-floor behaviour is preserved."""
    import json as _j
    monkeypatch.setenv("CRON_SECRET", "testsecret")
    monkeypatch.setenv("V3_MIN_HOLDOUT_ACC", "0.55")

    rows = [
        ("meta_WOLF", _j.dumps({"accuracy": 0.65})),  # kept
        ("meta_OLD",  _j.dumps({"accuracy": 0.30})),  # purged
    ]

    class _Cur:
        def __init__(self):
            self.executed = []
            self._rows_returned = False

        def execute(self, sql, params=None):
            self.executed.append((sql, params))

        def fetchall(self):
            if not self._rows_returned:
                self._rows_returned = True
                return rows
            return []

        def fetchone(self):
            return None

    cur = _Cur()

    class _Conn:
        def cursor(self):
            return cur

    class _DbCtx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    import core.db
    monkeypatch.setattr(core.db, "db_conn", lambda: _DbCtx())
    monkeypatch.setattr(wolf_app, "db_conn", lambda: _DbCtx())

    import asyncio
    out = asyncio.run(wolf_app.delete_model(x_cron_secret="testsecret"))
    assert out["ok"] is True
    assert out["mode"] == "low_accuracy"
    assert out["deleted"] == ["OLD(acc=30.0%)"]
    assert out["kept"] == ["WOLF(acc=65.0%)"]


def test_telegram_status_reports_state_and_recent_alerts(monkeypatch):
    """/api/telegram/status reads ghost_state.last_signal_cron_* and
    wolf_signal_alerts rows; surfaces a flat payload for the cockpit."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "cid")

    class _Cur:
        def __init__(self):
            self.last_sql = ""

        def execute(self, sql, params=None):
            self.last_sql = sql

        def fetchone(self):
            if "key='last_signal_cron_ts'" in self.last_sql:
                return ("1779470000",)
            if "key='last_signal_cron_sent'" in self.last_sql:
                return ("2",)
            return None

        def fetchall(self):
            if "FROM wolf_signal_alerts" in self.last_sql:
                return [
                    (101, 1779470000, "BUY",  58.5, 72.0, 0.92),
                    (102, 1779380000, "SELL", 72.0, 60.0, 0.85),
                ]
            return []

    class _Conn:
        def cursor(self):
            return _Cur()

    class _DbCtx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _DbCtx())
    out = wolf_app.telegram_status()
    assert out["ok"] is True
    assert out["configured"] is True
    assert out["last_cron_ts"] == 1779470000
    assert out["last_cron_sent"] == 2
    assert len(out["recent_alerts"]) == 2
    assert out["recent_alerts"][0]["prediction_id"] == 101
    assert out["recent_alerts"][0]["direction"] == "BUY"
    assert out["recent_alerts"][0]["confidence"] == 0.92


def test_telegram_status_handles_missing_table_gracefully(monkeypatch):
    """wolf_signal_alerts may not exist on a fresh deploy — must not crash."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    class _Cur:
        def __init__(self):
            self.last_sql = ""

        def execute(self, sql, params=None):
            self.last_sql = sql
            if "FROM wolf_signal_alerts" in sql:
                raise RuntimeError("relation does not exist")

        def fetchone(self):
            return None

        def fetchall(self):
            return []

    class _Conn:
        def cursor(self):
            return _Cur()

    class _DbCtx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _DbCtx())
    out = wolf_app.telegram_status()
    assert out["ok"] is True
    assert out["configured"] is False
    assert out["recent_alerts"] == []


# ── PR #23: ops cleanup + admin auth + news filter tightening ───────────

def test_purge_ghost_portfolio_dry_run_lists_without_deleting(monkeypatch):
    """dry_run=true reports matches without firing DELETE statements."""
    monkeypatch.setenv("CRON_SECRET", "testsecret")
    rows = [
        (1, "ZZE2E1779472312548"),
        (2, "STOCK GHOST"),
        (3, "WOLF"),
        (4, "ZZ_TEST"),
        (5, "TESTPOS"),
    ]
    executed = []

    class _Cur:
        def __init__(self):
            self._returned = False

        def execute(self, sql, params=None):
            executed.append((sql, params))

        def fetchall(self):
            if not self._returned:
                self._returned = True
                return rows
            return []

        def fetchone(self):
            return None

    class _Conn:
        def cursor(self): return _Cur()

    class _DbCtx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _DbCtx())
    import asyncio
    out = asyncio.run(wolf_app.purge_ghost_portfolio(x_cron_secret="testsecret", dry_run=True))
    assert out["ok"] is True
    assert out["dry_run"] is True
    assert {r["id"] for r in out["would_delete"]} == {1, 2, 4, 5}
    assert out["kept"] == 1
    assert not any("DELETE FROM user_portfolio" in e[0] for e in executed)


def test_purge_ghost_portfolio_real_run_deletes_matched_rows(monkeypatch):
    """dry_run=false issues one DELETE per matched row."""
    monkeypatch.setenv("CRON_SECRET", "testsecret")
    rows = [
        (10, "ZZE2EABCDEF"),
        (11, "GHOST123"),
        (12, "WOLF"),
    ]

    class _Cur:
        def __init__(self):
            self.executed = []
            self._returned = False

        def execute(self, sql, params=None):
            self.executed.append((sql, params))

        def fetchall(self):
            if not self._returned:
                self._returned = True
                return rows
            return []

        def fetchone(self):
            return None

    cur = _Cur()

    class _Conn:
        def cursor(self): return cur

    class _DbCtx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _DbCtx())
    import asyncio
    out = asyncio.run(wolf_app.purge_ghost_portfolio(x_cron_secret="testsecret", dry_run=False))
    assert out["ok"] is True
    assert out["dry_run"] is False
    assert {r["id"] for r in out["deleted"]} == {10, 11}
    assert out["kept"] == 1
    deletes = [e for e in cur.executed if "DELETE FROM user_portfolio" in e[0]]
    assert len(deletes) == 2


# ── audit: purge ZZE2E* test rows from the predictions table ────────────

def _purge_test_pred_db(grouped, monkeypatch):
    executed = []

    class _Cur:
        rowcount = 0
        def execute(self, sql, params=None):
            executed.append((sql, params))
            if sql.strip().upper().startswith("DELETE"):
                self.rowcount = sum(c for _, c in grouped)
        def fetchall(self): return grouped
        def fetchone(self): return None

    class _Conn:
        def cursor(self): return _Cur()

    class _DbCtx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _DbCtx())
    return executed


def test_purge_test_predictions_dry_run_reports_without_deleting(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "testsecret")
    executed = _purge_test_pred_db([("ZZE2E1779", 3), ("ZZ_PROBE", 1)], monkeypatch)
    import asyncio
    out = asyncio.run(wolf_app.purge_test_predictions(x_cron_secret="testsecret", dry_run=True))
    assert out["ok"] is True and out["dry_run"] is True
    assert out["total_matched"] == 4
    assert out["deleted"] == 0
    assert {m["symbol"] for m in out["matched"]} == {"ZZE2E1779", "ZZ_PROBE"}
    assert not any(e[0].strip().upper().startswith("DELETE") for e in executed)


def test_purge_test_predictions_real_run_deletes(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "testsecret")
    executed = _purge_test_pred_db([("ZZE2EABC", 5)], monkeypatch)
    import asyncio
    out = asyncio.run(wolf_app.purge_test_predictions(x_cron_secret="testsecret", dry_run=False))
    assert out["ok"] is True and out["dry_run"] is False
    assert out["deleted"] == 5
    deletes = [e for e in executed if e[0].strip().upper().startswith("DELETE")]
    assert len(deletes) == 1
    assert "ILIKE ANY" in deletes[0][0]


def test_purge_test_predictions_requires_secret(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "testsecret")
    _purge_test_pred_db([], monkeypatch)
    import asyncio
    from fastapi import HTTPException
    try:
        asyncio.run(wolf_app.purge_test_predictions(x_cron_secret="wrong", dry_run=True))
        assert False, "expected 403"
    except HTTPException as e:
        assert e.status_code == 403


def test_admin_cookie_token_roundtrip(monkeypatch):
    """A freshly minted token validates; a forged/tampered one does not.
    (PR #28 cookie login replaced HTTP Basic Auth, which blank-paged on prod.)"""
    monkeypatch.setenv("CRON_SECRET", "letmein")
    tok = wolf_app._admin_mint_token()
    assert wolf_app._admin_token_valid(tok) is True
    # Tampered signature
    exp, _sig = tok.rsplit(".", 1)
    assert wolf_app._admin_token_valid(exp + ".deadbeef") is False
    # Garbage / empty
    assert wolf_app._admin_token_valid("") is False
    assert wolf_app._admin_token_valid("nope") is False


def test_admin_cookie_token_expiry(monkeypatch):
    """An expired token (negative TTL) is rejected."""
    monkeypatch.setenv("CRON_SECRET", "letmein")
    expired = wolf_app._admin_mint_token(ttl_s=-10)
    assert wolf_app._admin_token_valid(expired) is False


def test_admin_token_valid_dev_mode_when_secret_unset(monkeypatch):
    """No CRON_SECRET → any token (even empty) is accepted (dev mode)."""
    monkeypatch.delenv("CRON_SECRET", raising=False)
    assert wolf_app._admin_token_valid("") is True
    assert wolf_app._admin_token_valid("anything") is True


def test_admin_page_serves_login_without_cookie(monkeypatch):
    """GET /admin with no valid cookie (secret set) serves the login page,
    NOT a 401 — this is the fix for the blank-page Basic-Auth issue."""
    monkeypatch.setenv("CRON_SECRET", "letmein")
    from fastapi.testclient import TestClient
    c = TestClient(wolf_app.APP)
    r = c.get("/admin")
    assert r.status_code == 200
    assert "Admin Login" in r.text or "Sign in" in r.text


def test_admin_login_sets_cookie_then_admin_serves_console(monkeypatch):
    """POST /admin/login with correct secret sets the cookie; subsequent
    GET /admin serves the real console (admin.html)."""
    monkeypatch.setenv("CRON_SECRET", "letmein")
    from fastapi.testclient import TestClient
    c = TestClient(wolf_app.APP)
    bad = c.post("/admin/login", json={"secret": "wrong"})
    assert bad.status_code == 401
    ok = c.post("/admin/login", json={"secret": "letmein"})
    assert ok.status_code == 200 and ok.json()["ok"] is True
    # TestClient persists the Set-Cookie → next /admin should serve console
    r = c.get("/admin")
    assert r.status_code == 200
    assert "Objective Gate Monitor" in r.text or "GHOST <span>ADMIN" in r.text


def test_news_filter_drops_articles_without_wolf_mention(monkeypatch):
    """Articles with no symbol tags AND no WOLF mention must be dropped.
    Pre-PR-#23 the empty-syms branch fell through and leaked Zoom / IBM /
    Ralph Lauren / etc. into the investor news feed."""
    import api.wolf_endpoints as we
    we._CACHE.clear()
    fake_articles = [
        {"title": "Zoom hits new highs after earnings", "symbols": []},
        {"title": "IBM announces partnership", "symbols": []},
        {"title": "Wolfspeed posts Q1 results", "symbols": []},
        {"title": "WOLF, AAPL, NVDA: today's movers", "symbols": []},
        {"title": "Ralph Lauren launches new collection", "symbols": []},
        {"title": "Random tech news", "symbols": ["TSLA"]},
        # PR #26: tagged ["WOLF"] by the Finnhub roundup but the TEXT is
        # about other names — this is exactly the leak case. Must be DROPPED
        # now that we require a textual mention, not just the symbols tag.
        {"title": "Stocks to watch: Ross Stores, Advance Auto Parts", "symbols": ["WOLF", "NVDA"]},
    ]
    import core.news as _news
    monkeypatch.setattr(_news, "get_recent_articles", lambda n: fake_articles)
    import sys, types
    fake_yf = types.ModuleType("yfinance")

    class _Tk:
        def __init__(self, sym): pass
        news = []

    fake_yf.Ticker = _Tk
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    import asyncio
    resp = asyncio.run(we.get_wolf_news(category="all"))
    import json
    body = json.loads(resp.body)
    titles = {a["title"] for a in body.get("articles", [])}
    assert "Wolfspeed posts Q1 results" in titles
    assert "WOLF, AAPL, NVDA: today's movers" in titles
    assert "Zoom hits new highs after earnings" not in titles
    assert "IBM announces partnership" not in titles
    assert "Ralph Lauren launches new collection" not in titles
    assert "Random tech news" not in titles
    # The key PR #26 assertion: WOLF-tagged but textually off-topic → DROPPED
    assert "Stocks to watch: Ross Stores, Advance Auto Parts" not in titles


def test_news_filter_replaces_finnhub_stock_source_label(monkeypatch):
    """Internal 'finnhub_stock' source label is sanitised to 'News'."""
    import api.wolf_endpoints as we
    we._CACHE.clear()
    fake_articles = [
        {"title": "WOLFSPEED inks deal", "symbols": [], "source": "finnhub_stock"},
    ]
    import core.news as _news
    monkeypatch.setattr(_news, "get_recent_articles", lambda n: fake_articles)
    import sys, types
    fake_yf = types.ModuleType("yfinance")

    class _Tk:
        def __init__(self, sym): pass
        news = []

    fake_yf.Ticker = _Tk
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    import asyncio
    resp = asyncio.run(we.get_wolf_news(category="all"))
    import json
    body = json.loads(resp.body)
    sources = [a["source"] for a in body["articles"]]
    assert "finnhub_stock" not in sources
    assert "News" in sources


def test_safe_float_rejects_dict_input():
    """Defensive scrub: newer yfinance can return dicts like
    {'raw': 70.5, 'fmt': '70.50'} for some fields — must coerce to None
    rather than raw-stringify the dict (currentTradingPeriod-leak repro)."""
    import api.wolf_endpoints as we
    assert we._safe_float({"raw": 70.5, "fmt": "70.50"}) is None
    assert we._safe_float([1, 2, 3]) is None
    assert we._safe_float(70.5) == 70.5
    assert we._safe_float("70.5") == 70.5
    assert we._safe_float(None) is None


def test_safe_int_rejects_dict_input():
    import api.wolf_endpoints as we
    assert we._safe_int({"raw": 1000}) is None
    assert we._safe_int([1, 2]) is None
    assert we._safe_int(1000) == 1000
    assert we._safe_int(None) is None


# ── PR #24 (items 8-15): investor-view polish ───────────────────────────

def test_check_feeds_probes_wolf_by_default(monkeypatch):
    """WOLF-only system: check_feeds probes the actual target (WOLF), not a
    proxy, and reports whether WOLF is priceable. (Reverses the PR #24 AAPL
    proxy — a single-ticker product should report WOLF availability honestly.)"""
    import core.prices as _prices
    probed = []
    monkeypatch.setattr(_prices, "_alpaca", lambda sym: (probed.append(("alpaca", sym)), None)[1])
    monkeypatch.setattr(_prices, "_yfinance", lambda sym: (probed.append(("yfinance", sym)), 42.0)[1])
    monkeypatch.delenv("HEALTH_PROBE_SYMBOL", raising=False)
    r = _prices.check_feeds()
    assert r["probe_symbol"] == "WOLF"
    assert r["alpaca_stock"] is False
    assert r["yfinance"] is True
    assert r["priceable"] is True
    assert "WOLF priceable" in r["summary"]
    assert "1/2" in r["summary"]
    assert all(sym == "WOLF" for (_kind, sym) in probed)


def test_check_feeds_respects_health_probe_symbol_env(monkeypatch):
    """Operators can override the probe symbol via HEALTH_PROBE_SYMBOL env."""
    import core.prices as _prices
    monkeypatch.setattr(_prices, "_alpaca", lambda sym: 1.0)
    monkeypatch.setattr(_prices, "_yfinance", lambda sym: None)
    monkeypatch.setenv("HEALTH_PROBE_SYMBOL", "MSFT")
    r = _prices.check_feeds()
    assert r["probe_symbol"] == "MSFT"
    assert r["alpaca_stock"] is True
    assert r["yfinance"] is False
    assert "1/2" in r["summary"]
    assert "MSFT" in r["summary"]


# ── PR #25: news yfinance leak + source extraction + polygon stats ──────

def test_news_yfinance_augmentation_applies_wolf_filter(monkeypatch):
    """yfinance.Ticker(WOLF).news returns related-ticker noise (IBM, Zoom,
    Ralph Lauren) tagged to WOLF. PR #25 closes the leak: every yfinance
    item must mention WOLFSPEED/WOLF/SiC/silicon carbide in title or
    summary to survive."""
    import api.wolf_endpoints as we
    we._CACHE.clear()
    import core.news as _news
    monkeypatch.setattr(_news, "get_recent_articles", lambda n: [])

    yf_items = [
        {"title": "Zoom hits new highs", "publisher": "Reuters", "link": "https://reuters.com/zoom"},
        {"title": "IBM announces partnership", "publisher": "Bloomberg", "link": "https://bloomberg.com/ibm"},
        {"title": "Wolfspeed (WOLF) earnings beat estimates", "publisher": "Benzinga", "link": "https://benzinga.com/wolf"},
        {"title": "SiC market grows 30% YoY", "publisher": "Seeking Alpha", "link": "https://seekingalpha.com/sic"},
        {"title": "Random tech news", "publisher": "TechCrunch", "summary": "About silicon carbide chips"},
        {"title": "Ralph Lauren launches collection", "publisher": "Reuters", "link": "https://reuters.com/rl"},
    ]
    import sys, types
    fake_yf = types.ModuleType("yfinance")

    class _Tk:
        def __init__(self, sym): pass

        @property
        def news(self): return yf_items

    fake_yf.Ticker = _Tk
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    import asyncio
    resp = asyncio.run(we.get_wolf_news(category="all"))
    import json
    body = json.loads(resp.body)
    titles = {a["title"] for a in body.get("articles", [])}
    assert "Wolfspeed (WOLF) earnings beat estimates" in titles
    assert "SiC market grows 30% YoY" in titles
    assert "Random tech news" in titles  # silicon carbide in summary
    assert "Zoom hits new highs" not in titles
    assert "IBM announces partnership" not in titles
    assert "Ralph Lauren launches collection" not in titles


def test_news_source_label_uses_publisher_not_generic_news(monkeypatch):
    """yfinance items expose publisher (e.g. 'Benzinga', 'Seeking Alpha').
    PR #25 keeps the real publisher name."""
    import api.wolf_endpoints as we
    we._CACHE.clear()
    import core.news as _news
    monkeypatch.setattr(_news, "get_recent_articles", lambda n: [])
    import sys, types
    fake_yf = types.ModuleType("yfinance")

    class _Tk:
        def __init__(self, sym): pass

        @property
        def news(self):
            return [
                {"title": "WOLFSPEED hits new high", "publisher": "Benzinga", "link": "https://benzinga.com/x"},
                {"title": "Wolfspeed quarterly results", "publisher": "Seeking Alpha", "link": "https://seekingalpha.com/y"},
            ]

    fake_yf.Ticker = _Tk
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    import asyncio
    resp = asyncio.run(we.get_wolf_news(category="all"))
    import json
    body = json.loads(resp.body)
    sources = {a["source"] for a in body["articles"]}
    assert "Benzinga" in sources
    assert "Seeking Alpha" in sources


def test_news_source_falls_back_to_hostname_when_only_finnhub_label(monkeypatch):
    """If the only source label is the internal 'finnhub_stock' and the
    article has a URL, derive publisher from hostname."""
    import api.wolf_endpoints as we
    we._CACHE.clear()
    import core.news as _news
    monkeypatch.setattr(_news, "get_recent_articles", lambda n: [
        {"title": "WOLFSPEED announces deal", "source": "finnhub_stock",
         "url": "https://www.reuters.com/article/wolf-deal", "symbols": []},
    ])
    import sys, types
    fake_yf = types.ModuleType("yfinance")

    class _Tk:
        def __init__(self, sym): pass
        news = []

    fake_yf.Ticker = _Tk
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    import asyncio
    resp = asyncio.run(we.get_wolf_news(category="all"))
    import json
    body = json.loads(resp.body)
    sources = {a["source"] for a in body["articles"]}
    assert "reuters.com" in sources
    assert "finnhub_stock" not in sources
    assert "News" not in sources


def test_polygon_stats_fallback_populates_missing_fields(monkeypatch):
    """When yfinance returns no market_cap / volume / OHLC, polygon fallback
    fills them from /v3/reference/tickers + /v2/aggs endpoints."""
    import api.wolf_endpoints as we
    monkeypatch.setenv("POLYGON_API_KEY", "polykey")

    out = {
        "open": None, "high": None, "low": None, "volume": None,
        "avg_volume": None, "market_cap": None, "week52_low": None, "week52_high": None,
    }

    class _Resp:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body

        def json(self): return self._body

    def fake_get(url, timeout=None, **kwargs):
        if "/v3/reference/tickers/WOLF" in url:
            return _Resp(200, {"results": {"market_cap": 12_500_000_000}})
        if "/v2/aggs/ticker/WOLF/prev" in url:
            return _Resp(200, {"results": [{"o": 70.5, "h": 73.1, "l": 69.8, "c": 72.4, "v": 5_200_000}]})
        if "/v2/aggs/ticker/WOLF/range" in url:
            return _Resp(200, {"results": [
                {"c": 50.0, "h": 51.0, "l": 49.0, "v": 1_000_000},
                {"c": 80.0, "h": 82.0, "l": 78.0, "v": 2_000_000},
            ]})
        return _Resp(404, {})

    monkeypatch.setattr("requests.get", fake_get)
    filled = we._try_polygon_stats_fallback(out)
    assert filled is True
    assert out["market_cap"] == 12_500_000_000
    assert out["open"] == 70.5
    assert out["high"] == 73.1
    assert out["volume"] == 5_200_000
    assert out["week52_high"] == 82.0
    assert out["week52_low"] == 49.0


def test_polygon_stats_fallback_skipped_without_key(monkeypatch):
    """No POLYGON_API_KEY → no HTTP call, no mutation, returns False."""
    import api.wolf_endpoints as we
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    called = []
    monkeypatch.setattr("requests.get", lambda *a, **k: called.append(1))
    out = {"market_cap": None, "open": None}
    filled = we._try_polygon_stats_fallback(out)
    assert filled is False
    assert called == []
    assert out == {"market_cap": None, "open": None}


# ── PR #27: /api/wolf/gate-status objective-gate monitor ────────────────

def test_gate_status_reports_config_and_live_prediction(monkeypatch):
    """gate-status surfaces objective config + floor + symbol phase + a live
    prediction with per-gate pass/fail. Signal clearing both gates →
    would_alert True."""
    import core.prediction as _pred
    import core.signal_engine as _se

    monkeypatch.setattr(_pred, "_objective_effective_config", lambda: {
        "mode": "aggressive", "target_wr": 0.62, "min_samples": 8,
        "bootstrap_min_conf": 0.78, "lookback_days": 120,
    })
    monkeypatch.setattr(_pred, "_objective_enforced", lambda: True)
    monkeypatch.setattr(_pred, "_objective_auto_enabled", lambda: False)
    monkeypatch.setattr(_pred, "CONFIDENCE_FLOOR", 0.75)
    monkeypatch.setattr(_pred, "_objective_symbol_stats",
                        lambda s, d: {"combined_total": 3, "combined_wins": 2, "combined_wr": 0.667})
    monkeypatch.setattr(_pred, "_objective_gate",
                        lambda s, d, c: (c >= 0.78, None if c >= 0.78 else "objective_bootstrap_conf", {}))

    def _ple(s, a, scores=None):
        if scores is not None:
            scores["up_prob"] = 0.62
            scores["regime"] = {"label": "Trend-up"}
            scores["model_meta"] = {
                "accuracy": 0.654, "min_win_proba": 0.55,
                "calibrated": True, "calibration_method": "sigmoid",
            }
        return (("UP", 0.81), None)
    monkeypatch.setattr(_se, "predict_live_ex", _ple)

    out = wolf_app.wolf_gate_status()
    assert out["ok"] is True
    assert out["objective"]["mode"] == "aggressive"
    assert out["objective"]["auto_mode_enabled"] is False
    assert out["confidence_floor"] == 0.75
    assert out["symbol_stats"]["phase"] == "bootstrap"
    lp = out["live_prediction"]
    assert lp["model_emitted"] is True
    assert lp["direction"] == "UP"
    assert lp["confidence"] == 0.81
    assert lp["passes_confidence_floor"] is True
    assert lp["passes_objective_gate"] is True
    assert lp["would_alert"] is True
    # up_prob + binding threshold surfaced
    assert lp["up_prob"] == 0.62
    assert lp["calibrated"] is True
    assert lp["calibration_method"] == "sigmoid"
    assert lp["bootstrap_min_conf"] == 0.78
    assert lp["binding_confidence_threshold"] == 0.78   # bootstrap: max(0.75, 0.78)
    # needed = 0.55 + (0.78 - 0.654)/4 = 0.5815
    assert lp["up_prob_needed_to_fire"] == 0.5815
    assert lp["up_prob_gap"] == 0.0385                  # 0.62 - 0.5815, cleared


def test_gate_status_shows_no_alert_when_below_bootstrap_conf(monkeypatch):
    """Signal clears the floor but not the bootstrap objective threshold →
    would_alert False with the skip reason surfaced."""
    import core.prediction as _pred
    import core.signal_engine as _se

    monkeypatch.setattr(_pred, "_objective_effective_config", lambda: {
        "mode": "aggressive", "target_wr": 0.62, "min_samples": 8,
        "bootstrap_min_conf": 0.78, "lookback_days": 120,
    })
    monkeypatch.setattr(_pred, "_objective_enforced", lambda: True)
    monkeypatch.setattr(_pred, "_objective_auto_enabled", lambda: False)
    monkeypatch.setattr(_pred, "CONFIDENCE_FLOOR", 0.75)
    monkeypatch.setattr(_pred, "_objective_symbol_stats",
                        lambda s, d: {"combined_total": 2, "combined_wins": 1, "combined_wr": 0.5})
    monkeypatch.setattr(_pred, "_objective_gate",
                        lambda s, d, c: (c >= 0.78, None if c >= 0.78 else "objective_bootstrap_conf", {}))
    monkeypatch.setattr(_se, "predict_live_ex", lambda s, a, scores=None: (("UP", 0.76), None))

    out = wolf_app.wolf_gate_status()
    lp = out["live_prediction"]
    assert lp["passes_confidence_floor"] is True
    assert lp["passes_objective_gate"] is False
    assert lp["objective_skip_reason"] == "objective_bootstrap_conf"
    assert lp["would_alert"] is False


def test_gate_status_handles_no_model_signal(monkeypatch):
    """When the model emits no signal (prob_low etc), gate-status reports
    model_emitted False + the reason, never crashes."""
    import core.prediction as _pred
    import core.signal_engine as _se
    monkeypatch.setattr(_pred, "_objective_effective_config", lambda: {
        "mode": "aggressive", "target_wr": 0.62, "min_samples": 8,
        "bootstrap_min_conf": 0.78, "lookback_days": 120,
    })
    monkeypatch.setattr(_pred, "_objective_enforced", lambda: True)
    monkeypatch.setattr(_pred, "_objective_auto_enabled", lambda: False)
    monkeypatch.setattr(_pred, "CONFIDENCE_FLOOR", 0.75)
    monkeypatch.setattr(_pred, "_objective_symbol_stats",
                        lambda s, d: {"combined_total": 0, "combined_wins": 0, "combined_wr": 0.0})

    def _ple(s, a, scores=None):
        # Model didn't fire (up_prob below min_p) but the score vector is still
        # captured — this is the Tuesday "how close did it come" case.
        if scores is not None:
            scores["up_prob"] = 0.54
            scores["regime"] = {"label": "Chop"}
            scores["model_meta"] = {"accuracy": 0.654, "min_win_proba": 0.55}
        return (None, "prob_low")
    monkeypatch.setattr(_se, "predict_live_ex", _ple)

    out = wolf_app.wolf_gate_status()
    lp = out["live_prediction"]
    assert lp["model_emitted"] is False
    assert lp["reason"] == "prob_low"
    assert lp["would_alert"] is False
    # up_prob surfaced even though the model didn't fire
    assert lp["up_prob"] == 0.54
    assert lp["up_prob_needed_to_fire"] == 0.5815
    assert lp["up_prob_gap"] == -0.0415   # negative — how far short it landed


# ── PR #29: per-cycle gate-outcome history ──────────────────────────────

def test_gate_history_parses_and_aggregates(monkeypatch):
    """gate-history reads ghost_state.gate_outcome_history, returns newest
    first, and aggregates fired_count + binding_gates across the window."""
    import json as _json
    history_json = _json.dumps([
        {"ts": 1000, "scanned": 1, "candidates": 0, "saved": 0, "dedup_blocked": 0,
         "would_fire": False, "top_skip": "v3_prob_low", "skip_counts": {"v3_prob_low": 1}},
        {"ts": 2000, "scanned": 1, "candidates": 0, "saved": 0, "dedup_blocked": 0,
         "would_fire": False, "top_skip": "below_confidence_floor", "skip_counts": {"below_confidence_floor": 1}},
        {"ts": 3000, "scanned": 1, "candidates": 1, "saved": 1, "dedup_blocked": 0,
         "would_fire": True, "top_skip": None, "skip_counts": {}},
        {"ts": 4000, "scanned": 1, "candidates": 0, "saved": 0, "dedup_blocked": 0,
         "would_fire": False, "top_skip": "v3_prob_low", "skip_counts": {"v3_prob_low": 1}},
    ])

    class _Cur:
        def __init__(self): self.last_sql = ""
        def execute(self, sql, params=None): self.last_sql = sql
        def fetchone(self):
            if "gate_outcome_history" in self.last_sql:
                return (history_json,)
            return None
        def fetchall(self): return []

    class _Conn:
        def cursor(self): return _Cur()

    class _DbCtx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _DbCtx())
    out = wolf_app.wolf_gate_history(limit=50)
    assert out["ok"] is True
    assert out["count"] == 4
    assert out["fired_count"] == 1
    assert out["history"][0]["ts"] == 4000     # newest first
    assert out["history"][-1]["ts"] == 1000
    assert out["binding_gates"]["v3_prob_low"] == 2
    assert out["binding_gates"]["below_confidence_floor"] == 1


def test_gate_history_empty_when_no_record(monkeypatch):
    """No history row yet → empty list, zero counts, no crash."""
    class _Cur:
        def execute(self, sql, params=None): pass
        def fetchone(self): return None
        def fetchall(self): return []

    class _Conn:
        def cursor(self): return _Cur()

    class _DbCtx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _DbCtx())
    out = wolf_app.wolf_gate_history()
    assert out["ok"] is True
    assert out["count"] == 0
    assert out["fired_count"] == 0
    assert out["history"] == []


# ── audit §3: silence-cycle "how close" logging ─────────────────────────

def test_predict_ex_captures_near_miss_on_floor_skip(monkeypatch):
    """A below-floor signal returns the skip code AND writes up_prob +
    confidence/floor into scores_out so the cycle can log how close it came."""
    import core.prediction as _pred, core.signal_engine as _se
    monkeypatch.setattr(_pred, "get_price", lambda s, a=None: 100.0)

    def _ple(s, a, scores=None):
        if scores is not None:
            scores["up_prob"] = 0.58
            scores["model_meta"] = {"min_win_proba": 0.55}
        return (("UP", 0.62), None)   # below a 0.80 floor
    monkeypatch.setattr(_se, "predict_live_ex", _ple)

    sv = {}
    pick, skip = _pred._predict_symbol_ex("WOLF", "stock",
                                          {"confidence_floor_override": 0.80}, scores_out=sv)
    assert pick is None
    assert skip == "below_confidence_floor"
    assert sv["up_prob"] == 0.58
    assert sv["confidence"] == 0.62
    assert sv["confidence_floor"] == 0.80


def test_gate_history_aggregates_closest_near_miss(monkeypatch):
    """closest_near_miss = the highest-up_prob near miss across the window,
    with prob_gap relative to its threshold surfaced."""
    import json as _json
    history_json = _json.dumps([
        {"ts": 1000, "would_fire": False, "top_skip": "v3_prob_low",
         "near_miss": {"symbol": "WOLF", "up_prob": 0.51, "min_win_proba": 0.55, "prob_gap": -0.04}},
        {"ts": 2000, "would_fire": False, "top_skip": "v3_prob_low",
         "near_miss": {"symbol": "WOLF", "up_prob": 0.54, "min_win_proba": 0.55, "prob_gap": -0.01}},
        {"ts": 3000, "would_fire": False, "top_skip": "v3_prob_low",
         "near_miss": {"symbol": "WOLF", "up_prob": 0.49, "min_win_proba": 0.55, "prob_gap": -0.06}},
    ])

    class _Cur:
        def __init__(self): self.last_sql = ""
        def execute(self, sql, params=None): self.last_sql = sql
        def fetchone(self):
            return (history_json,) if "gate_outcome_history" in self.last_sql else None
        def fetchall(self): return []

    class _Conn:
        def cursor(self): return _Cur()

    class _DbCtx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _DbCtx())
    out = wolf_app.wolf_gate_history(limit=50)
    assert out["ok"] is True
    cn = out["closest_near_miss"]
    assert cn["up_prob"] == 0.54        # highest across the window
    assert cn["prob_gap"] == -0.01
    assert cn["ts"] == 2000


def _pick_journal_db(total, listing_rows, resolved_rows, monkeypatch):
    """Wire wolf_app.db_conn to a fake cursor that answers the three queries
    the pick-journal endpoint issues (count / listing / resolved)."""
    class _Cur:
        def __init__(self): self.last = ""
        def execute(self, sql, params=None): self.last = sql
        def fetchone(self):
            if "COUNT(" in self.last:
                return (total,)
            return None
        def fetchall(self):
            if "ORDER BY" in self.last:
                return listing_rows
            if "outcome IS NOT NULL" in self.last:
                return resolved_rows
            return []

    class _Conn:
        def cursor(self): return _Cur()

    class _DbCtx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _DbCtx())


def test_pick_journal_kill_condition_triggers(monkeypatch):
    """N>=30, 50% win rate, 95% CI below 80% => falsification gate fires."""
    # listing row order matches the SELECT column list (15 cols incl. features/scores)
    listing = [(
        301, "WOLF", "UP", 0.80, 10.0, 10.6, 9.7, 4000, 4100, 4200,
        "WIN", 10.6, 6.0,
        {"hour_of_day": 9},
        {"regime": {"label": "Trend-up"}, "specialists": {"daily_swing": {"up_prob": 0.61}}},
    )]
    resolved = [(0.80, "WIN", 6.0)] * 15 + [(0.80, "LOSS", -3.0)] * 15
    _pick_journal_db(30, listing, resolved, monkeypatch)
    out = wolf_app.wolf_pick_journal(limit=25, offset=0)
    assert out["ok"] is True
    assert out["metrics"]["resolved"] == 30
    assert out["metrics"]["win_rate"] == 0.5
    assert abs(out["metrics"]["expectancy_pct"] - 1.5) < 1e-6
    assert abs(out["metrics"]["brier"] - 0.34) < 1e-6
    f = out["verdict"]["falsification"]
    assert f["falsified"] is True
    assert f["status"] == "ABANDON_80_CLAIM"
    assert f["ci95_high"] < 0.80
    # listing surfaces the captured score vector / regime-at-issuance
    assert out["picks"][0]["scores"]["regime"]["label"] == "Trend-up"


def test_pick_journal_insufficient_samples(monkeypatch):
    """Below min_samples => not falsified, status reports gathering evidence."""
    resolved = [(0.82, "WIN", 5.0)] * 4 + [(0.82, "LOSS", -3.0)] * 1
    _pick_journal_db(5, [], resolved, monkeypatch)
    out = wolf_app.wolf_pick_journal()
    assert out["ok"] is True
    assert out["metrics"]["resolved"] == 5
    f = out["verdict"]["falsification"]
    assert f["falsified"] is False
    assert f["status"] == "insufficient_samples"


# ── audit §4: pick journal full feature vector ──────────────────────────

def test_predict_live_ex_journals_full_feature_vector(monkeypatch):
    """predict_live_ex writes the complete indicator vector (RSI/MACD/Bollinger/
    ATR/volume/momentum/EMA/ADX) into scores['features'] via real
    _calculate_features, so the pick journal captures it."""
    import core.signal_engine as _se
    import numpy as _np
    # 220 steadily-rising 1h bars -> uptrend that clears the regime gates
    rows = []
    for i in range(220):
        px = 100.0 + i * 0.4
        rows.append({"ts": "2026-05-20T%02d:00:00Z" % (i % 24),
                     "open": px - 0.2, "high": px + 0.5, "low": px - 0.5,
                     "close": px, "volume": 1000 + i * 5})
    monkeypatch.setattr(_se, "_fetch_ohlcv",
                        lambda s, a, period="5d", interval="1h": rows)

    class _M:
        def predict_proba(self, X): return _np.array([[0.1, 0.9]])
    meta = {"edge": 0.3, "accuracy": 0.66, "wf_acc_mean": 0.64,
            "wf_edge_mean": 0.2, "wf_fold_count": 4, "trained_at": time.time()}
    monkeypatch.setattr(_se, "load_model", lambda s: (_M(), _se.FEATURE_COLS, meta))
    for k, v in {"V3_MIN_WIN_PROBA": "0.55", "V3_MIN_EDGE": "0.0",
                 "V3_MIN_HOLDOUT_ACC": "0.0", "V3_MIN_WF_ACC_MEAN": "0.0"}.items():
        monkeypatch.setenv(k, v)

    scores = {}
    sig, reason = _se.predict_live_ex("WOLF", "stock", scores=scores)
    assert sig is not None and sig[0] == "UP"   # cleared the gates and fired
    fv = scores.get("features")
    assert isinstance(fv, dict)
    for k in ("rsi", "macd_hist", "pct_b", "atr_pct", "volume_ratio",
              "mom_4h", "adx", "obv_slope", "stoch_k"):
        assert k in fv, "missing journaled feature: " + k


def test_pick_journal_flattens_indicators(monkeypatch):
    """The journal exposes a flat `indicators` block pulled from scores.features
    so the issuance-time vector is directly readable."""
    listing = [(
        302, "WOLF", "UP", 0.83, 10.0, 10.6, 9.7, 5000, 5100, 5200,
        "WIN", 10.6, 6.0,
        {"hour_of_day": 10},
        {"regime": {"label": "Trend-up"},
         "features": {"rsi": 58.2, "macd_hist": 0.12, "pct_b": 0.71,
                      "atr_pct": 0.021, "volume_ratio": 1.4, "mom_4h": 0.8, "adx": 27.5}},
    )]
    _pick_journal_db(1, listing, [(0.83, "WIN", 6.0)], monkeypatch)
    out = wolf_app.wolf_pick_journal(limit=25)
    ind = out["picks"][0]["indicators"]
    assert ind["rsi"] == 58.2
    assert ind["macd_hist"] == 0.12
    assert ind["atr_pct"] == 0.021
    assert ind["regime"] == "Trend-up"


# ── audit §5: realized P&L tracking ─────────────────────────────────────

def test_realized_pnl_compounds_and_aggregates():
    from core.pnl import realized_pnl
    trades = [
        {"resolved_at": 1, "symbol": "WOLF", "outcome": "WIN", "pnl_pct": 10.0,
         "entry_price": 10.0, "exit_price": 11.0},
        {"resolved_at": 2, "symbol": "WOLF", "outcome": "LOSS", "pnl_pct": -5.0,
         "entry_price": 11.0, "exit_price": 10.45},
        {"resolved_at": 3, "symbol": "WOLF", "outcome": "WIN", "pnl_pct": 20.0,
         "entry_price": 10.45, "exit_price": 12.54},
    ]
    out = realized_pnl(trades, bankroll=1000.0, stake_fraction=1.0)
    assert out["count"] == 3
    assert out["wins"] == 2 and out["losses"] == 1
    # 1000 * 1.10 * 0.95 * 1.20 = 1254.0
    assert abs(out["final_equity"] - 1254.0) < 1e-6
    assert abs(out["realized_pnl_usd"] - 254.0) < 1e-6
    assert abs(out["total_return_pct"] - 25.4) < 1e-6
    # profit factor = gross_win / gross_loss = (10+20)/5 = 6.0
    assert out["profit_factor"] == 6.0
    assert out["best_trade_pct"] == 20.0
    assert out["worst_trade_pct"] == -5.0
    # win rate over decided (WIN+LOSS) trades = 2/3
    assert out["win_rate"] == round(2 / 3, 4)
    assert len(out["curve"]) == 3
    assert out["curve"][-1]["equity"] == 1254.0


def test_realized_pnl_drawdown_and_empty():
    from core.pnl import realized_pnl
    # peak after +50% (1500), trough after -40% (900) => drawdown 40%
    trades = [
        {"resolved_at": 1, "outcome": "WIN", "pnl_pct": 50.0},
        {"resolved_at": 2, "outcome": "LOSS", "pnl_pct": -40.0},
    ]
    out = realized_pnl(trades, bankroll=1000.0, stake_fraction=1.0)
    assert abs(out["max_drawdown_pct"] - 40.0) < 1e-6
    empty = realized_pnl([], bankroll=500.0)
    assert empty["count"] == 0
    assert empty["final_equity"] == 500.0
    assert empty["realized_pnl_usd"] == 0.0
    assert empty["curve"] == []


def test_wolf_pnl_endpoint_reads_db(monkeypatch):
    rows = [
        (1000, "WOLF", "WIN", 10.0, 10.0, 11.0),
        (2000, "WOLF", "LOSS", -5.0, 11.0, 10.45),
    ]

    class _Cur:
        def execute(self, sql, params=None): self.last = sql
        def fetchall(self): return rows
        def fetchone(self): return None

    class _Conn:
        def cursor(self): return _Cur()

    class _DbCtx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _DbCtx())
    out = wolf_app.wolf_pnl(symbol="WOLF")
    assert out["ok"] is True
    assert out["symbol"] == "WOLF"
    assert out["count"] == 2
    # 1000 * 1.10 * 0.95 = 1045.0
    assert abs(out["final_equity"] - 1045.0) < 1e-6


# ── security: /api/v2/recent WOLF-only default ──────────────────────────

def test_v2_recent_defaults_to_wolf(monkeypatch):
    import core.portfolio_routes as pr
    captured = {}

    class _Cur:
        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params
        def fetchall(self): return []

    class _Conn:
        def cursor(self): return _Cur()

    class _Ctx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(pr, "db_conn", lambda: _Ctx())

    out = pr.v2_recent()   # default symbol => WOLF-only
    assert out["ok"] is True
    assert "symbol=%s" in captured["sql"]
    assert captured["params"] == ("WOLF",)

    # explicit symbol is upper-cased and filtered
    pr.v2_recent(symbol="tsla")
    assert captured["params"] == ("TSLA",)

    # ALL bypasses the filter (internal cross-symbol listing)
    pr.v2_recent(symbol="ALL")
    assert "symbol=%s" not in captured["sql"]
    assert captured["params"] is None


# ── audit §2: kill conditions ───────────────────────────────────────────

def _kill_db(rows, monkeypatch):
    """Wire core.prediction.db_conn so evaluate_kill_conditions sees `rows`
    (newest-first list of (confidence, outcome, pnl_pct))."""
    import core.prediction as _pred

    class _Cur:
        def execute(self, sql, params=None):
            self._limit = len(rows)
            if params:
                # (symbols, limit) for watchlist-aware kill query
                self._limit = params[1] if len(params) > 1 else params[0]
        def fetchall(self):
            return rows[: getattr(self, "_limit", len(rows))]

    class _Conn:
        def cursor(self): return _Cur()

    class _DbCtx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(_pred, "db_conn", lambda: _DbCtx())


def test_kill_conditions_inert_during_cold_start(monkeypatch):
    """Few resolved picks => every gated condition reads 'insufficient' and
    nothing trips. Silence-by-design must not raise a false kill alarm."""
    import core.prediction as _pred
    _kill_db([(0.8, "WIN", 4.0), (0.8, "LOSS", -3.0)], monkeypatch)
    out = _pred.evaluate_kill_conditions()
    assert out["ok"] is True
    assert out["any_triggered"] is False
    by = {c["name"]: c for c in out["conditions"]}
    assert by["win_rate"]["status"] == "insufficient"
    assert by["brier"]["status"] == "insufficient"
    assert by["expectancy"]["status"] == "insufficient"
    # consecutive_losses also sample-gated during cold start
    assert by["consecutive_losses"]["status"] == "insufficient"


def test_kill_conditions_trip_on_low_winrate_and_consec_losses(monkeypatch):
    """Full windows of poor results trip win_rate / brier / expectancy; a recent
    LOSS streak trips consecutive_losses."""
    import core.prediction as _pred
    monkeypatch.setenv("KILL_WINRATE_WINDOW", "30")
    monkeypatch.setenv("KILL_BRIER_WINDOW", "30")
    monkeypatch.setenv("KILL_EXPECTANCY_WINDOW", "20")
    monkeypatch.setenv("KILL_CONSEC_LOSSES", "3")
    # newest-first: 5 fresh losses (streak), then 5 wins, then 20 more losses (30 total)
    rows = [(0.85, "LOSS", -3.0)] * 5 + [(0.85, "WIN", 2.0)] * 5 + [(0.85, "LOSS", -3.0)] * 20
    _kill_db(rows, monkeypatch)
    out = _pred.evaluate_kill_conditions()
    by = {c["name"]: c for c in out["conditions"]}
    # win rate over 30 = 5/30 = 0.167 < 0.70
    assert by["win_rate"]["status"] == "red"
    assert by["win_rate"]["current"] < 0.70
    # 5 leading losses >= 3
    assert by["consecutive_losses"]["status"] == "red"
    assert by["consecutive_losses"]["current"] == 5
    # brier: 25 losses at conf .85 contribute .7225 each -> mean well above .35
    assert by["brier"]["status"] == "red"
    # expectancy over 20 = (5*-3 + 5*2 + 10*-3)/20 = -1.75 < 0
    assert by["expectancy"]["status"] == "red"
    assert by["expectancy"]["current"] < 0
    assert out["any_triggered"] is True


# ── audit §2: kill-condition ENFORCEMENT (pause / cooldown / resume) ─────

def _enforcement_db(resolved_rows, state, monkeypatch):
    """Stateful fake DB: resolved-pick SELECTs return `resolved_rows`; ghost_state
    reads/writes are backed by the shared `state` dict so pause persists across
    db_conn() contexts."""
    import core.prediction as _pred

    class _Cur:
        def execute(self, sql, params=None):
            s = sql.strip()
            self._mode = None
            self._keys = None
            if s.startswith("CREATE TABLE"):
                return
            if "FROM predictions" in s:
                self._mode = "resolved"
            elif s.startswith("SELECT key,val FROM ghost_state"):
                self._mode = "state_select"
                self._keys = params
            elif s.startswith("INSERT INTO ghost_state"):
                if params and len(params) == 2:
                    state[params[0]] = params[1]
            elif s.startswith("DELETE FROM ghost_state"):
                for k in (params or []):
                    state.pop(k, None)

        def fetchall(self):
            if self._mode == "resolved":
                return list(resolved_rows)
            if self._mode == "state_select":
                return [(k, state[k]) for k in (self._keys or []) if k in state]
            return []

        def fetchone(self):
            return None

    class _Conn:
        def cursor(self): return _Cur()

    class _DbCtx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(_pred, "db_conn", lambda: _DbCtx())


def test_enforce_pauses_on_trip_and_manual_resume(monkeypatch):
    """A hard trip (auto_pause etc.) pauses the engine with no auto-resume; the
    state persists and resume_engine() clears it. Telegram is stubbed."""
    import core.prediction as _pred, core.telegram as _tel
    monkeypatch.setattr(_tel, "send_health_alert", lambda *a, **k: None)
    monkeypatch.setenv("KILL_WINRATE_WINDOW", "30")
    monkeypatch.setenv("KILL_EXPECTANCY_WINDOW", "20")
    state = {}
    rows = [(0.85, "LOSS", -3.0)] * 30   # trips win_rate/brier/expectancy/consec
    _enforcement_db(rows, state, monkeypatch)

    res = _pred.enforce_kill_conditions()
    assert res["paused"] is True
    assert "win_rate" in res["reason"]
    assert res["auto_resume_at"] is None          # hard trip => manual resume
    assert state["engine_paused"] == "1"
    assert _pred.engine_pause_state()["paused"] is True

    _pred.resume_engine()
    assert _pred.engine_pause_state()["paused"] is False
    assert "engine_paused" not in state


def test_enforce_cooldown_only_sets_autoresume(monkeypatch):
    """Only consecutive-losses trips (insufficient samples elsewhere) => cooldown
    with an auto-resume time; the pause clears itself once that time passes."""
    import core.prediction as _pred, core.telegram as _tel
    monkeypatch.setattr(_tel, "send_health_alert", lambda *a, **k: None)
    monkeypatch.setenv("KILL_CONSEC_LOSSES", "3")
    monkeypatch.setenv("KILL_MIN_SAMPLES", "3")
    monkeypatch.setenv("KILL_COOLDOWN_MINUTES", "60")
    state = {}
    rows = [(0.85, "LOSS", -3.0)] * 3   # consec trips; windows of 30/20 insufficient
    _enforcement_db(rows, state, monkeypatch)

    res = _pred.enforce_kill_conditions()
    assert res["paused"] is True
    assert res["actions"] == ["cooldown"]
    assert res["auto_resume_at"] is not None
    # force the auto-resume time into the past -> next read auto-resumes
    state["engine_pause_auto_resume_at"] = str(int(time.time()) - 1)
    assert _pred.engine_pause_state()["paused"] is False


def test_enforce_clears_pause_when_conditions_clear(monkeypatch):
    """When kill conditions no longer trip, enforcement auto-resumes the engine."""
    import core.prediction as _pred, core.telegram as _tel
    monkeypatch.setattr(_tel, "send_health_alert", lambda *a, **k: None)
    state = {"engine_paused": "1", "engine_pause_reason": "consecutive_losses->cooldown"}
    rows = [(0.85, "WIN", 2.0)] * 5
    _enforcement_db(rows, state, monkeypatch)
    res = _pred.enforce_kill_conditions()
    assert res.get("paused") is False
    assert res.get("cleared") is True
    assert "engine_paused" not in state


def test_enforce_inert_when_disabled(monkeypatch):
    """KILL_SWITCH_ENABLED=0 => enforcement never pauses, even on a tripping set."""
    import core.prediction as _pred
    monkeypatch.setenv("KILL_SWITCH_ENABLED", "0")
    state = {}
    _enforcement_db([(0.85, "LOSS", -3.0)] * 30, state, monkeypatch)
    res = _pred.enforce_kill_conditions()
    assert res["paused"] is False
    assert state == {}


# ── feat/telegram-cards: daily card / weekly summary / silence formatters ──

def test_conviction_and_news_influence():
    from core.telegram_cards import conviction_from_confidence, compute_news_influence
    assert conviction_from_confidence(0.92) == "HIGH"
    assert conviction_from_confidence(0.86) == "MEDIUM"
    assert conviction_from_confidence(0.80) == "LOW"
    # no nudge => 0% news, 100% model
    assert compute_news_influence(0.80, 0.80) == {"influence_pct": 0, "model_pct": 100}
    # confidence moved 0.72 -> 0.80 by news => ~10% influence
    n = compute_news_influence(0.80, 0.72)
    assert n["influence_pct"] == 10 and n["model_pct"] == 90
    # model logic is never 0 even on a huge nudge (capped at 95% news)
    capped = compute_news_influence(0.90, 0.05)
    assert capped["influence_pct"] <= 95 and capped["model_pct"] >= 5


def test_format_daily_card_with_and_without_news():
    from core.telegram_cards import format_daily_card
    base = {
        "date": "Monday May 26, 2026", "direction": "UP", "confidence": 0.88,
        "current_price": 12.34, "buy_point": 12.34, "sell_target": 12.71,
        "stop_loss": 12.10, "expected_move_pct": 3.0,
        "rates": {"today_pct": 88, "week_high_pct": 91, "week_low_pct": 80},
        "track_record": {"wins": 40, "losses": 12, "win_rate_pct": 76.9,
                         "last5": ["W", "W", "L", "W", "W"], "streak": "3W"},
    }
    # no news
    out = format_daily_card({**base, "news": {"influence_pct": 0, "model_pct": 100}})
    assert "WOLF Daily Card" in out
    assert "Conviction: MEDIUM" in out
    assert "Confidence: 88%" in out
    assert "Buy Point (entry): $12.34" in out
    assert "Expected Move: +3.0%" in out
    assert "No material news in last 48hrs. Prediction is 100% model-driven." in out
    assert "All-time: 40-12 (76.9%)" in out
    assert "Last 5: W W L W W" in out
    assert "Streak: 3W" in out
    # with news
    out2 = format_daily_card({**base, "news": {"influence_pct": 35, "model_pct": 65,
                                               "summary": "Wolfspeed wins $2B chip subsidy."}})
    assert "Wolfspeed wins $2B chip subsidy." in out2
    assert "News influence: 35% | Model logic: 65%" in out2


def test_format_weekly_summary():
    from core.telegram_cards import format_weekly_summary
    out = format_weekly_summary({
        "week_range": "May 19 - May 23",
        "followed": {"wins": 4, "losses": 1, "win_rate_pct": 80.0, "pnl_usd": 120.5},
        "alltime": {"win_rate_pct": 76.9, "wins": 40, "losses": 12},
        "retrain_in_days": 9,
        "top_pick": {"day": "Tuesday", "confidence_pct": 91},
        "weakest_pick": {"day": "Thursday", "confidence_pct": 85},
        "news_driven": {"count": 2, "total": 5},
    })
    assert "WOLF Weekly Summary" in out
    assert "4W / 1L — 80.0%" in out
    assert "+$120.50 on $1,000 deployed" in out
    assert "All-time: 76.9% accuracy (40W/12L)" in out
    assert "Model retrains in: 9 days" in out
    assert "Top Confidence Pick: Tuesday @ 91%" in out
    assert "Weakest Pick: Thursday @ 85%" in out
    assert "News-Driven Picks: 2 of 5" in out


def test_format_weekly_summary_negative_pnl():
    from core.telegram_cards import format_weekly_summary
    out = format_weekly_summary({"followed": {"wins": 1, "losses": 3, "win_rate_pct": 25.0, "pnl_usd": -47.25}})
    assert "-$47.25 on $1,000 deployed" in out


def test_format_silence_card():
    from core.telegram_cards import format_silence_card
    out = format_silence_card({"ghost_score": 72, "reason": "Insufficient edge — model confidence 72%"})
    assert "Status: SILENCE — No high-conviction signal today" in out
    assert "Ghost Score: 72/100" in out
    assert "Insufficient edge — model confidence 72%" in out
    assert "Next scan: Tomorrow 8 AM CT" in out


def test_build_silence_card_data_fetches_ghost_score(monkeypatch):
    monkeypatch.setattr(
        "api.wolf_endpoints.ghost_score_payload_sync",
        lambda **kw: {"ok": True, "score": 65.0},
    )
    out = wolf_app._build_silence_card_data({
        "top_reason_label": "v3 model prob below BUY floor",
        "confidence_floor": 0.75,
    })
    assert out["ghost_score"] == 65
    assert "floor 75%" in out["reason"]


def test_build_silence_card_data_score_fallback_on_error(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("score unavailable")

    monkeypatch.setattr("api.wolf_endpoints.ghost_score_payload_sync", _boom)
    out = wolf_app._build_silence_card_data({"top_reason_label": "v3 model prob below BUY floor"})
    assert out["ghost_score"] == "--"


# ── feat/admin-lineage-audit: model lineage + admin action log ──────────

def test_record_admin_action_appends_and_caps(monkeypatch):
    store = {}

    class _Cur:
        last = ""
        def execute(self, sql, params=None):
            self.last = sql
            # _record_admin_action uses an inline key + single val param
            if sql.strip().startswith("INSERT INTO ghost_state") and "admin_audit_log" in sql and params:
                store["admin_audit_log"] = params[0]
        def fetchone(self):
            if "admin_audit_log" in self.last:
                return (store.get("admin_audit_log"),)
            return None

    class _Conn:
        def cursor(self): return _Cur()

    class _DbCtx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _DbCtx())
    wolf_app._record_admin_action("purge_test_predictions", "deleted=3")
    logged = json.loads(store["admin_audit_log"])
    assert logged[-1]["action"] == "purge_test_predictions"
    assert logged[-1]["detail"] == "deleted=3"
    assert "ts" in logged[-1]


def test_v3_lineage_endpoint_newest_first(monkeypatch):
    hist = [
        {"ts": 1000, "symbols": [{"symbol": "WOLF", "accuracy": 0.66, "passed": True}]},
        {"ts": 2000, "symbols": [{"symbol": "WOLF", "accuracy": 0.70, "passed": True}]},
    ]

    class _Cur:
        def execute(self, sql, params=None): self.last = sql
        def fetchone(self):
            if "model_lineage" in self.last:
                return (json.dumps(hist),)
            return None

    class _Conn:
        def cursor(self): return _Cur()

    class _DbCtx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _DbCtx())
    out = wolf_app.v3_lineage(limit=50)
    assert out["ok"] is True
    assert out["count"] == 2
    assert out["runs"][0]["ts"] == 2000   # newest first


def test_admin_audit_log_requires_cookie(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "testsecret")
    from fastapi import HTTPException

    class _Req:
        cookies = {}

    try:
        wolf_app.admin_audit_log(request=_Req(), limit=50)
        assert False, "expected 404"
    except HTTPException as e:
        assert e.status_code == 404


def test_admin_audit_log_returns_entries_with_valid_cookie(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "testsecret")
    log = [{"ts": 1, "action": "resume_engine", "detail": "cleared"},
           {"ts": 2, "action": "purge_test_predictions", "detail": "deleted=3"}]

    class _Cur:
        def execute(self, sql, params=None): self.last = sql
        def fetchone(self):
            if "admin_audit_log" in self.last:
                return (json.dumps(log),)
            return None

    class _Conn:
        def cursor(self): return _Cur()

    class _DbCtx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _DbCtx())
    token = wolf_app._admin_mint_token()

    class _Req:
        cookies = {wolf_app._ADMIN_COOKIE: token}

    out = wolf_app.admin_audit_log(request=_Req(), limit=50)
    assert out["ok"] is True
    assert out["count"] == 2
    assert out["actions"][0]["ts"] == 2   # newest first


# ── feat/free-api-wiring: short-interest squeeze risk + trend ───────────

def test_squeeze_risk_tag_bands():
    from api.wolf_endpoints import _squeeze_risk_tag
    assert _squeeze_risk_tag(40, 1) == "extreme"   # float >= 35
    assert _squeeze_risk_tag(10, 6) == "extreme"   # dtc >= 5
    assert _squeeze_risk_tag(28, 1) == "high"      # float >= 25
    assert _squeeze_risk_tag(10, 3.5) == "high"    # dtc >= 3
    assert _squeeze_risk_tag(18, 1) == "medium"    # float >= 15
    assert _squeeze_risk_tag(10, 2) == "medium"    # dtc >= 2
    assert _squeeze_risk_tag(5, 1) == "low"
    assert _squeeze_risk_tag(None, None) == "low"


def test_short_trend_direction():
    from api.wolf_endpoints import _short_trend
    assert _short_trend(None, 100) is None
    assert _short_trend(100, None) is None
    rising = _short_trend(120, 100)
    assert rising["direction"] == "rising" and rising["delta"] == 20 and rising["pct"] == 20.0
    falling = _short_trend(80, 100)
    assert falling["direction"] == "falling" and falling["pct"] == -20.0
    assert _short_trend(100, 100)["direction"] == "flat"


# ── feat/version-bump: /api/_version reports 2.1.0 / PR 50 ──────────────

def test_deploy_version_reports_bumped_version():
    out = wolf_app.deploy_version()
    assert out["ok"] is True
    assert out["app_version"] == "2.1.0"
    assert out["_pr_version"] == 50
    assert wolf_app.APP_VERSION == "2.1.0"
    assert wolf_app.APP.version == "2.1.0"


# ── fix/cockpit-ghost-rows: portfolio API filters ZZE2E/test rows ───────

def test_get_portfolio_filters_ghost_rows(monkeypatch):
    import core.portfolio_routes as pr
    # rows: (id,symbol,asset_type,quantity,buy_price,buy_date,notes,manual_price)
    rows = [
        (1, "WOLF", "stock", 100.0, 3.50, "", "", 4.00),
        (2, "ZZE2E1779572554878", "stock", 1.0, 1.000, "", "", None),
        (3, "ZZE2E1779572468489", "stock", 1.0, 1.000, "", "", None),
        (4, "ZZTEST", "stock", 5.0, 2.0, "", "", None),
    ]

    class _Cur:
        def execute(self, sql, params=None): self.last = sql
        def fetchall(self):
            return rows if "user_portfolio" in getattr(self, "last", "") else []
        def fetchone(self): return None

    class _Conn:
        def cursor(self): return _Cur()
        def commit(self): pass

    class _Ctx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(pr, "db_conn", lambda: _Ctx())
    monkeypatch.setattr("core.prices.get_price", lambda s, a=None: 4.00)

    out = pr.build_portfolio_payload()
    syms = [p["symbol"] for p in out["positions"]]
    assert syms == ["WOLF"]                       # ghost/test rows excluded
    # totals only reflect the real WOLF position (100 * 3.50 = 350), not $3k+
    assert out["total_cost"] == 350.0


def test_is_ghost_symbol():
    from core.portfolio_routes import _is_ghost_symbol
    assert _is_ghost_symbol("ZZE2E1779572554878") is True
    assert _is_ghost_symbol("zze2e123") is True   # case-insensitive
    assert _is_ghost_symbol("STOCK GHOST") is True
    assert _is_ghost_symbol("TESTPOS") is True
    assert _is_ghost_symbol("WOLF") is False
    assert _is_ghost_symbol("AAPL") is False


# ── audit v2: /api/news WOLF filter + /api/picks WOLF-only default ──────

def test_is_wolf_relevant_filters_offtopic():
    from wolf_app import _is_wolf_relevant
    assert _is_wolf_relevant({"title": "Wolfspeed announces new SiC fab", "summary": ""}) is True
    assert _is_wolf_relevant({"title": "WOLF jumps 10%", "summary": "shares rally"}) is True
    assert _is_wolf_relevant({"title": "Zoom Video beats earnings", "summary": "video calls"}) is False
    assert _is_wolf_relevant({"title": "Ross Stores raises guidance", "summary": "retail"}) is False
    assert _is_wolf_relevant({"title": "Lionsgate, Advance Auto Parts movers", "summary": ""}) is False


def test_get_news_filters_offtopic(monkeypatch):
    import core.news as _news
    arts = [
        {"title": "Wolfspeed expands SiC capacity", "summary": "x"},
        {"title": "Zoom Video tops estimates", "summary": "video"},
        {"title": "Ross Stores Q1 results", "summary": "retail"},
    ]
    monkeypatch.setattr(_news, "get_recent_articles", lambda n=20: arts)
    out = wolf_app.get_news()
    assert out["ok"] is True
    assert [a["title"] for a in out["articles"]] == ["Wolfspeed expands SiC capacity"]
    assert out["count"] == 1


def test_get_picks_defaults_to_wolf_and_honors_asset_type(monkeypatch):
    captured = {}

    class _Cur:
        description = [("id",), ("symbol",), ("outcome",)]
        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params
        def fetchall(self): return []

    class _Conn:
        def cursor(self): return _Cur()

    class _Ctx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _Ctx())

    out = wolf_app.get_picks()                       # default => WOLF-only
    assert out["ok"] is True
    assert "symbol = %s" in captured["sql"]
    assert captured["params"] == ("WOLF",)

    wolf_app.get_picks(symbol="ALL", asset_type="stock")   # ALL bypasses symbol, asset_type honored
    assert "symbol = %s" not in captured["sql"]
    assert "asset_type = %s" in captured["sql"]
    assert captured["params"] == ("stock",)


# ── fix/ema-fallback: _calculate_features trend flags for young tickers ──

def _ohlcv_series(n, direction):
    """n synthetic daily bars, rising or falling, shaped like _fetch_ohlcv rows."""
    rows = []
    for i in range(n):
        px = (100.0 + i * 0.5) if direction == "up" else (300.0 - i * 0.5)
        rows.append({"ts": "2026-01-01T00:00:00Z", "open": px, "high": px + 0.6,
                     "low": px - 0.6, "close": px, "volume": 1000 + i})
    return rows


def test_calc_features_young_ticker_uptrend_not_structurally_bearish():
    """~168 daily bars (<200, like post-Ch.11 WOLF), uptrend: the old code forced
    above_ema200/ema_trend_bullish to 0 (EMA200 fell back to cur). Now they
    reflect the real short/mid trend."""
    import core.signal_engine as _se
    f = _se._calculate_features(_ohlcv_series(168, "up"))
    assert f["above_ema200"] == 1
    assert f["ema_trend_bullish"] == 1


def test_calc_features_young_ticker_downtrend_flags_zero():
    """<200 bars, downtrend: flags are 0 (the fix isn't just forcing 1)."""
    import core.signal_engine as _se
    f = _se._calculate_features(_ohlcv_series(168, "down"))
    assert f["above_ema200"] == 0
    assert f["ema_trend_bullish"] == 0


def test_calc_features_mature_ticker_uses_real_ema200():
    """>=200 bars: full 20>50>200 stack. Uptrend -> 1, downtrend -> 0."""
    import core.signal_engine as _se
    up = _se._calculate_features(_ohlcv_series(250, "up"))
    assert up["above_ema200"] == 1 and up["ema_trend_bullish"] == 1
    dn = _se._calculate_features(_ohlcv_series(250, "down"))
    assert dn["above_ema200"] == 0 and dn["ema_trend_bullish"] == 0


def test_calc_features_too_few_bars_neutral_trend():
    """<20 bars: no meaningful EMA stack -> neutral (1) so the regime gate
    doesn't permanently block BUYs for a brand-new ticker."""
    import core.signal_engine as _se
    f = _se._calculate_features(_ohlcv_series(12, "up"))
    assert f["above_ema200"] == 1
    assert f["ema_trend_bullish"] == 1


# ── roadmap #1d: out-of-band pick-fire alerts (email/SMS) ───────────────

def test_notify_gating_when_unconfigured(monkeypatch):
    import core.notify as _n
    for k in ("SMTP_HOST", "SMTP_FROM", "ALERT_EMAIL_TO", "TWILIO_ACCOUNT_SID",
              "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "ALERT_SMS_TO"):
        monkeypatch.delenv(k, raising=False)
    assert _n.email_configured() is False
    assert _n.sms_configured() is False
    assert _n.send_email("s", "b") is False
    assert _n.send_sms("b") is False
    assert _n.notify_pick_fired("s", "b") == {"email": False, "sms": False}


def test_notify_email_sends_when_configured(monkeypatch):
    import core.notify as _n
    monkeypatch.setenv("ALERTS_ENABLED", "1")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "ghost@example.com")
    monkeypatch.setenv("ALERT_EMAIL_TO", "me@example.com")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASS", "p")
    sent = {}

    class _SMTP:
        def __init__(self, host, port, timeout=15): sent["host"] = host
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): sent["tls"] = True
        def login(self, u, p): sent["login"] = (u, p)
        def sendmail(self, frm, to, msg): sent["mail"] = (frm, to, msg)

    monkeypatch.setattr(_n.smtplib, "SMTP", _SMTP)
    assert _n.send_email("Subj", "Body text") is True
    assert sent["host"] == "smtp.example.com"
    assert sent["login"] == ("u", "p")
    assert sent["mail"][0] == "ghost@example.com" and sent["mail"][1] == ["me@example.com"]
    assert "Body text" in sent["mail"][2] and "Subj" in sent["mail"][2]


def test_notify_sms_sends_when_configured(monkeypatch):
    import core.notify as _n
    monkeypatch.setenv("ALERTS_ENABLED", "1")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACxxx")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TWILIO_FROM", "+15550001111")
    monkeypatch.setenv("ALERT_SMS_TO", "+15550002222,+15550003333")
    calls = []

    class _Resp:
        ok = True
        text = ""

    def _post(url, auth=None, data=None, timeout=None):
        calls.append({"url": url, "auth": auth, "data": data})
        return _Resp()

    import requests
    monkeypatch.setattr(requests, "post", _post)
    assert _n.send_sms("Fire!") is True
    assert len(calls) == 2                       # one per recipient
    assert "ACxxx" in calls[0]["url"]
    assert calls[0]["auth"] == ("ACxxx", "tok")
    assert calls[0]["data"]["Body"] == "Fire!" and calls[0]["data"]["From"] == "+15550001111"


def test_notify_pick_fired_dispatches_both(monkeypatch):
    import core.notify as _n
    monkeypatch.setattr(_n, "send_email", lambda s, b: True)
    monkeypatch.setattr(_n, "send_sms", lambda b: True)
    assert _n.notify_pick_fired("s", "b") == {"email": True, "sms": True}


def test_wf_fold_bounds_purges_label_horizon():
    """Default purged layout leaves a hold_bars gap between train and test."""
    import core.signal_engine as _se
    bounds = _se._wf_fold_bounds(
        n=127, min_train=60, test_size=15, step=15, purge=3,
        min_train_floor=60, test_size_floor=15,
    )
    assert bounds == [(60, 63, 78), (75, 78, 93), (90, 93, 108), (105, 108, 123)]
    # every fold: train ends purge bars before its test block, no overlap
    for train_end, test_start, _test_end in bounds:
        assert test_start - train_end == 3
        assert train_end >= 60


def test_wf_fold_bounds_purge_zero_is_contiguous():
    """purge=0 reproduces the old contiguous walk-forward (train_end==test_start)."""
    import core.signal_engine as _se
    bounds = _se._wf_fold_bounds(
        n=127, min_train=60, test_size=15, step=15, purge=0,
        min_train_floor=60, test_size_floor=15,
    )
    assert bounds[0] == (60, 60, 75)
    for train_end, test_start, _ in bounds:
        assert train_end == test_start


def test_wf_fold_bounds_respects_floors_and_fits_n():
    """Folds never exceed n and never train below the floor."""
    import core.signal_engine as _se
    bounds = _se._wf_fold_bounds(
        n=80, min_train=60, test_size=15, step=15, purge=3,
        min_train_floor=60, test_size_floor=15,
    )
    # n=80: start=63 -> test[63:78] ok (train_end 60); next start=78 -> 78+15>80 stop
    assert bounds == [(60, 63, 78)]
    for _train_end, _test_start, test_end in bounds:
        assert test_end <= 80


def test_assemble_pooled_training_stacks_with_weights():
    import numpy as np
    import core.signal_engine as _se
    X_train = np.array([[1.0, 2.0], [3.0, 4.0]])
    y_train = np.array([1, 0])
    peer_rows = [
        {"features": {"a": 5.0, "b": 6.0}, "label": 1},
        {"features": {"a": 7.0, "b": 8.0}, "label": 0},
        {"features": {"a": 9.0, "b": 10.0}, "label": 1},
    ]
    X, y, sw = _se._assemble_pooled_training(X_train, y_train, peer_rows, ["a", "b"], 3.0)
    assert X.shape == (5, 2)
    assert list(y) == [1, 0, 1, 0, 1]
    assert list(sw) == [3.0, 3.0, 1.0, 1.0, 1.0]   # WOLF weighted, peers = 1.0
    assert list(X[2]) == [5.0, 6.0]                 # peer row mapped by col order


def test_assemble_pooled_training_no_peers_returns_target():
    import numpy as np
    import core.signal_engine as _se
    X_train = np.array([[1.0], [2.0]])
    y_train = np.array([1, 0])
    X, y, sw = _se._assemble_pooled_training(X_train, y_train, [], ["a"], 2.5)
    assert X is X_train and y is y_train
    assert list(sw) == [2.5, 2.5]


def test_peer_symbols_parsing(monkeypatch):
    import core.signal_engine as _se
    monkeypatch.delenv("PEER_SYMBOLS", raising=False)
    default = _se._v3_peer_symbols()
    assert "ON" in default and "STM" in default
    monkeypatch.setenv("PEER_SYMBOLS", "on, stm ,ON,,POWI")
    assert _se._v3_peer_symbols() == ["ON", "STM", "POWI"]   # upper, dedupe, drop empties


def test_collect_peer_rows_skips_thin_and_failed(monkeypatch):
    import core.signal_engine as _se
    monkeypatch.setattr(_se, "_v3_peer_symbols", lambda: ["WOLF", "GOOD", "THIN", "BOOM"])
    monkeypatch.setattr(_se, "_min_train_rows", lambda: 3)

    def fake_bt(sym, atype):
        if sym == "GOOD":
            return [{"features": {}, "label": 1}] * 5
        if sym == "THIN":
            return [{"features": {}, "label": 0}] * 2   # below min_train_rows
        if sym == "BOOM":
            raise RuntimeError("feed down")
        return []
    monkeypatch.setattr(_se, "backtest_symbol", fake_bt)

    pooled, used = _se._collect_peer_rows("WOLF")       # target excluded from peers
    assert len(pooled) == 5
    assert used == [{"symbol": "GOOD", "n": 5}]


def test_feature_schema_tracks_pool_and_sector_flags(monkeypatch):
    import core.signal_engine as _se
    monkeypatch.setenv("V3_SECTOR_FEATURE", "off")
    monkeypatch.setenv("V3_POOL_TRAINING", "on")
    assert _se._v3_feature_schema() == "macd_pct_v1+sec0"
    monkeypatch.setenv("V3_POOL_TRAINING", "off")
    assert _se._v3_feature_schema() == "macd_raw_v0+sec0"
    monkeypatch.setenv("V3_SECTOR_FEATURE", "on")
    assert _se._v3_feature_schema() == "macd_raw_v0+sec1"


def test_active_feature_cols_appends_sector_only_when_on(monkeypatch):
    import core.signal_engine as _se
    monkeypatch.setenv("V3_SECTOR_FEATURE", "off")
    assert "sector_rel_strength" not in _se._active_feature_cols()
    assert _se._active_feature_cols() == list(_se.FEATURE_COLS)
    monkeypatch.setenv("V3_SECTOR_FEATURE", "on")
    cols = _se._active_feature_cols()
    assert cols[-1] == "sector_rel_strength"
    assert len(cols) == len(_se.FEATURE_COLS) + 1


def test_proba_ensemble_averages_and_predicts():
    import numpy as np
    import core.signal_engine as _se

    class _Stub:
        classes_ = np.array([0, 1])
        def __init__(self, p):
            self._p = np.array(p)
        def predict_proba(self, X):
            return np.tile(self._p, (len(X), 1))

    ens = _se._ProbaEnsemble([_Stub([0.8, 0.2]), _Stub([0.4, 0.6])])
    proba = ens.predict_proba([[0], [0]])
    assert proba.shape == (2, 2)
    assert proba[0][0] == pytest.approx(0.6)   # mean(0.8, 0.4)
    assert proba[0][1] == pytest.approx(0.4)    # mean(0.2, 0.6)
    assert list(ens.predict([[0]])) == [0]      # argmax -> class 0
    assert list(ens.classes_) == [0, 1]


def test_proba_ensemble_weighted():
    import numpy as np
    import core.signal_engine as _se

    class _Stub:
        classes_ = np.array([0, 1])
        def __init__(self, p):
            self._p = np.array(p)
        def predict_proba(self, X):
            return np.tile(self._p, (len(X), 1))

    ens = _se._ProbaEnsemble([_Stub([1.0, 0.0]), _Stub([0.0, 1.0])], weights=[3.0, 1.0])
    proba = ens.predict_proba([[0]])
    assert proba[0][1] == pytest.approx(0.25)   # 1/(3+1) weight on second model


def test_prune_features_removes_columns(monkeypatch):
    import core.signal_engine as _se
    monkeypatch.setenv("V3_SECTOR_FEATURE", "off")
    monkeypatch.setenv("V3_PRUNE_FEATURES", "hour_of_day, day_of_week ,is_weekend")
    cols = _se._active_feature_cols()
    assert "hour_of_day" not in cols and "day_of_week" not in cols and "is_weekend" not in cols
    assert len(cols) == len(_se.FEATURE_COLS) - 3
    # sector column still added on top of pruning when enabled
    monkeypatch.setenv("V3_SECTOR_FEATURE", "on")
    assert _se._active_feature_cols()[-1] == "sector_rel_strength"


def test_align_sector_closes_forward_fills_and_no_lookahead():
    import core.signal_engine as _se
    target = [{"ts": "2026-01-02T00:00:00Z"}, {"ts": "2026-01-03T00:00:00Z"},
              {"ts": "2026-01-06T00:00:00Z"}]
    sector = [{"ts": "2026-01-02T00:00:00Z", "close": 100.0},
              # no 01-03 bar -> forward-fill 100.0
              {"ts": "2026-01-06T00:00:00Z", "close": 110.0}]
    aligned = _se._align_sector_closes(target, sector)
    assert aligned == [100.0, 100.0, 110.0]


def test_align_sector_closes_none_before_data():
    import core.signal_engine as _se
    target = [{"ts": "2026-01-01T00:00:00Z"}, {"ts": "2026-01-05T00:00:00Z"}]
    sector = [{"ts": "2026-01-05T00:00:00Z", "close": 50.0}]
    # first target bar predates any sector data -> None (neutral, not guessed)
    assert _se._align_sector_closes(target, sector) == [None, 50.0]


def test_sector_rel_at_point_in_time():
    import core.signal_engine as _se
    # target +20% over 2 bars, sector +10% -> rel strength = +10%
    rows = [{"close": 100.0}, {"close": 0.0}, {"close": 120.0}]
    aligned = [10.0, None, 11.0]
    assert _se._sector_rel_at(rows, aligned, 2, 2) == pytest.approx(0.20 - 0.10)


def test_sector_rel_at_insufficient_history_is_neutral():
    import core.signal_engine as _se
    rows = [{"close": 100.0}, {"close": 120.0}]
    aligned = [10.0, 11.0]
    assert _se._sector_rel_at(rows, aligned, 1, 5) == 0.0     # i < lookback
    # missing sector close -> neutral, never a future guess
    assert _se._sector_rel_at(rows, [None, 11.0], 1, 1) == 0.0


def test_macd_hist_normalized_only_when_pooling_on(monkeypatch):
    import core.signal_engine as _se
    bars = []
    price = 100.0
    for i in range(60):                                 # rising series -> macd_hist != 0
        price *= 1.01
        bars.append({"close": price, "high": price * 1.01,
                     "low": price * 0.99, "volume": 1000 + i})
    last = bars[-1]["close"]

    monkeypatch.setenv("V3_POOL_TRAINING", "off")
    raw = _se._calculate_features(bars)["macd_hist"]
    monkeypatch.setenv("V3_POOL_TRAINING", "on")
    feat_on = _se._calculate_features(bars)

    assert abs(raw) > 1e-6
    assert feat_on["macd_hist"] == pytest.approx(raw / last, rel=1e-6)  # price-normalized
    assert feat_on["macd_bullish"] == (1 if raw > 0 else 0)             # sign preserved


def _fit_tiny_model():
    """A cheap fitted binary classifier for calibration tests (no XGBoost)."""
    pytest.importorskip("sklearn")  # ML deps are prod-only; skip where absent
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    rng = np.random.RandomState(0)
    X = rng.rand(40, 3)
    y = (X[:, 0] + rng.rand(40) * 0.3 > 0.6).astype(int)
    y[0], y[1] = 0, 1  # guarantee both classes
    return LogisticRegression().fit(X, y), X, y


def test_maybe_calibrate_wraps_when_viable(monkeypatch):
    import numpy as np
    import core.signal_engine as _se
    monkeypatch.delenv("V3_CALIBRATION", raising=False)
    monkeypatch.setenv("V3_CALIBRATION_METHOD", "auto")
    model, X, _ = _fit_tiny_model()
    Xc = X[:20]
    yc = np.array([i % 2 for i in range(20)])
    final, info = _se._maybe_calibrate(model, Xc, yc)
    assert info["calibrated"] is True
    assert info["method"] == "sigmoid"            # auto -> sigmoid for small calib set
    assert info["n_calib"] == 20
    proba = final.predict_proba(X[:1])
    assert proba.shape == (1, 2)
    assert abs(float(proba[0].sum()) - 1.0) < 1e-6


# The fallback paths return before touching sklearn, so they use a sentinel
# "model" and synthetic numpy arrays — verifiable without the ML deps installed.
def test_maybe_calibrate_disabled_returns_raw(monkeypatch):
    import numpy as np
    import core.signal_engine as _se
    monkeypatch.setenv("V3_CALIBRATION", "off")
    sentinel = object()
    final, info = _se._maybe_calibrate(sentinel, np.zeros((20, 3)), np.array([i % 2 for i in range(20)]))
    assert info["calibrated"] is False
    assert info["skip_reason"] == "disabled"
    assert final is sentinel


def test_maybe_calibrate_single_class_calib_falls_back(monkeypatch):
    import numpy as np
    import core.signal_engine as _se
    monkeypatch.delenv("V3_CALIBRATION", raising=False)
    sentinel = object()
    final, info = _se._maybe_calibrate(sentinel, np.zeros((15, 3)), np.zeros(15, dtype=int))
    assert info["calibrated"] is False
    assert info["skip_reason"] == "insufficient_calib_data"
    assert final is sentinel


def test_maybe_calibrate_too_few_points_falls_back(monkeypatch):
    import numpy as np
    import core.signal_engine as _se
    monkeypatch.delenv("V3_CALIBRATION", raising=False)
    sentinel = object()
    final, info = _se._maybe_calibrate(sentinel, np.zeros((6, 3)), np.array([0, 1, 0, 1, 0, 1]))
    assert info["calibrated"] is False
    assert info["skip_reason"] == "insufficient_calib_data"
    assert final is sentinel


def test_has_loadable_v3_model_requires_actual_load(monkeypatch):
    """Startup retrain must gate on a model that LOADS, not a mere row.
    Regression for the feature_schema-guard dormancy: old rows kept
    label_type=tp_sl_daily (so the row existed) but load_model rejected them."""
    import wolf_app
    import core.signal_engine as _se
    monkeypatch.setenv("STOCK_SYMBOLS", "WOLF")
    # rows may exist, but load_model rejects (schema/label/age) -> NOT loadable -> retrain
    monkeypatch.setattr(_se, "load_model", lambda s: (None, None, None))
    assert wolf_app._has_loadable_v3_model() is False
    # a model that actually loads -> loadable -> skip retrain
    monkeypatch.setattr(_se, "load_model", lambda s: (object(), ["rsi"], {"feature_schema": "macd_pct_v1+sec0"}))
    assert wolf_app._has_loadable_v3_model() is True


def test_has_loadable_v3_model_any_symbol_counts(monkeypatch):
    import wolf_app
    import core.signal_engine as _se
    monkeypatch.setenv("STOCK_SYMBOLS", "WOLF,ON")
    monkeypatch.setattr(_se, "load_model",
                        lambda s: (object(), ["rsi"], {}) if s == "ON" else (None, None, None))
    assert wolf_app._has_loadable_v3_model() is True
