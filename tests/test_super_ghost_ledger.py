"""Tests for the Super Ghost Prediction Truth Ledger (PR #84).

The resolver math is tested as pure functions (no DB). The log/history/accuracy/
if-followed paths are tested with an in-memory fake DB so the suite stays offline.
"""

import core.super_ghost_ledger as ledger


# ─────────────────────────── pure resolver math ───────────────────────────

def test_direction_correct_rules():
    assert ledger._direction_correct("UP", 5.0) is True
    assert ledger._direction_correct("UP", -2.0) is False
    assert ledger._direction_correct("DOWN", -4.0) is True
    assert ledger._direction_correct("DOWN", 1.0) is False
    # HOLD / NO-EDGE: small move => skip was correct
    assert ledger._direction_correct("HOLD", 1.5) is True
    assert ledger._direction_correct("HOLD", 9.0) is False
    assert ledger._direction_correct("UP", None) is None


def test_wilson_lower_bound_is_conservative():
    # 1/1 raw = 100% but Wilson floor must be far below 1.0
    low = ledger._wilson_low(1, 1)
    assert low is not None and low < 0.8
    # Larger sample with same rate => higher (tighter) floor
    assert ledger._wilson_low(80, 100) > ledger._wilson_low(8, 10)
    assert ledger._wilson_low(0, 0) is None


def _series_from_closes(start_ts, closes, *, daily_range=0.0):
    """Build a daily OHLC series; one bar per day starting the day after start_ts."""
    rows = []
    day = 86400
    for i, c in enumerate(closes):
        ts = start_ts + (i + 1) * day
        rows.append({
            "ts": ts,
            "open": c,
            "high": c + daily_range,
            "low": c - daily_range,
            "close": c,
        })
    return rows


def test_resolve_one_up_prediction_hits_all_horizons():
    t0 = 1_700_000_000
    # 20 rising bars from ref=100 -> price climbs ~1/day
    closes = [100 + i for i in range(1, 21)]  # bars day1..day20: 101..120
    series = _series_from_closes(t0, closes)
    row = {
        "id": 1, "created_at": t0, "reference_price": 100.0, "direction": "UP",
        "target_price": 110.0, "stop_loss": 95.0,
        "resolved_1d_at": None, "resolved_5d_at": None, "resolved_20d_at": None,
    }
    now = t0 + 40 * 86400  # well past 20d
    u = ledger._resolve_one(row, series, now)
    # 1d bar close = 101 -> +1%
    assert u["price_1d"] == 101.0
    assert u["return_1d_pct"] == 1.0
    assert u["correct_1d"] is True
    # 5d bar close = 105 -> +5%
    assert u["return_5d_pct"] == 5.0
    assert u["correct_5d"] is True
    # 20d bar close = 120 -> +20%
    assert u["return_20d_pct"] == 20.0
    assert u["correct_20d"] is True
    assert u["fully_resolved"] is True
    # target 110 hit somewhere in window; stop 95 never hit
    assert u["hit_target"] is True
    assert u["hit_stop"] is False
    assert u["max_favorable_pct"] == 20.0
    assert u["max_adverse_pct"] == 1.0  # lowest low is bar1 (101) with range 0 -> +1%


def test_resolve_one_down_prediction_scored_correct_when_price_falls():
    t0 = 1_700_000_000
    closes = [100 - i for i in range(1, 6)]  # 99..95 over 5 bars
    series = _series_from_closes(t0, closes)
    row = {
        "id": 2, "created_at": t0, "reference_price": 100.0, "direction": "DOWN",
        "target_price": None, "stop_loss": None,
        "resolved_1d_at": None, "resolved_5d_at": None, "resolved_20d_at": None,
    }
    # Only ~6 days elapsed: 5d horizon resolves from bars; 20d neither has bars
    # nor enough wall-clock time, so it must stay unresolved.
    now = t0 + 6 * 86400
    u = ledger._resolve_one(row, series, now)
    assert u["return_5d_pct"] == -5.0
    assert u["correct_5d"] is True
    # only 5 bars -> 20d horizon not resolvable by bars, and not enough wall time
    assert "resolved_20d_at" not in u or u.get("resolved_20d_at") is None
    assert not u.get("fully_resolved")


def test_resolve_one_partial_only_one_day_elapsed():
    t0 = 1_700_000_000
    series = _series_from_closes(t0, [101.0])  # only 1 forward bar
    row = {
        "id": 3, "created_at": t0, "reference_price": 100.0, "direction": "UP",
        "target_price": None, "stop_loss": None,
        "resolved_1d_at": None, "resolved_5d_at": None, "resolved_20d_at": None,
    }
    now = t0 + 2 * 86400
    u = ledger._resolve_one(row, series, now)
    assert u.get("correct_1d") is True
    assert "return_5d_pct" not in u  # 5d not yet resolvable
    assert not u.get("fully_resolved")


def test_bars_after_excludes_same_day_bar():
    t0 = 1_700_000_000
    # a bar only 1 hour later should NOT count (same trading day)
    series = [
        {"ts": t0 + 3600, "open": 1, "high": 1, "low": 1, "close": 1},
        {"ts": t0 + 86400, "open": 2, "high": 2, "low": 2, "close": 2},
    ]
    fwd = ledger._bars_after(series, t0)
    assert len(fwd) == 1
    assert fwd[0]["close"] == 2


def test_resolve_one_wall_clock_indeterminate_when_bars_missing():
    # Lots of wall-clock time elapsed but NO forward bars (e.g. halted/delisted):
    # every horizon should be marked resolved (indeterminate) so it stops blocking.
    t0 = 1_700_000_000
    row = {
        "id": 9, "created_at": t0, "reference_price": 100.0, "direction": "UP",
        "target_price": None, "stop_loss": None,
        "resolved_1d_at": None, "resolved_5d_at": None, "resolved_20d_at": None,
    }
    now = t0 + 60 * 86400  # well past 20d + buffer
    u = ledger._resolve_one(row, [], now)
    assert u.get("resolved_1d_at") == now
    assert u.get("resolved_5d_at") == now
    assert u.get("resolved_20d_at") == now
    assert u.get("fully_resolved") is True
    # No bars => no return computed, no false correctness
    assert "return_1d_pct" not in u


def test_extract_row_flattens_report():
    report = {
        "ok": True, "symbol": "wolf", "engine": "super_ghost_checklist_v1", "ts": 123,
        "prediction": {"direction": "UP", "confidence": 0.72, "conviction_score": 55.0,
                        "edge_score": 30.0, "quality_score": 80.0, "accuracy_grade": "B+",
                        "action": "WATCHLIST UP BIAS", "data_quality": 0.8, "critical_data_quality": 0.9},
        "market_regime": {"label": "risk_on", "risk_state": "risk_on", "conviction_multiplier": 1.12},
        "coverage": {"available": 20, "total": 25},
        "risk_plan": {"entry": 100.0, "stop_loss": 95.0, "target_price": 110.0, "risk_reward_ratio": 2.0},
        "checklist": [{"id": 1}], "top_drivers": {"bullish": []}, "ai_brief": {"available": False},
    }
    row = ledger._extract_row(report)
    assert row["symbol"] == "WOLF"
    assert row["reference_price"] == 100.0
    assert row["direction"] == "UP"
    assert row["accuracy_grade"] == "B+"
    assert row["checklist_coverage"] == 20
    assert row["regime_label"] == "risk_on"
    assert row["risk_reward"] == 2.0


# ─────────────────────────── DB-mocked paths ───────────────────────────

class FakeCursor:
    def __init__(self, store):
        self.store = store
        self._result = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        low = s.lower()
        self._result = []
        if low.startswith("create") or low.startswith("alter") or "create index" in low:
            return
        if low.startswith("insert into super_ghost_predictions"):
            rid = self.store["next_id"]
            self.store["next_id"] += 1
            self.store["rows"].append({"id": rid, "params": params})
            self._result = [(rid,)]
            return
        if "select count(*)" in low:
            self._result = [(len(self.store["rows"]),)]
            return
        if "from super_ghost_predictions" in low:
            # Return the prepared synthetic resolved rows for accuracy/if-followed/history.
            self._result = list(self.store.get("select_rows", []))
            return

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class FakeConn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return FakeCursor(self.store)


class FakeDbCtx:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return FakeConn(self.store)

    def __exit__(self, *a):
        return False


def _install_fake_db(monkeypatch, store):
    import core.db as dbmod
    monkeypatch.setattr(dbmod, "db_conn", lambda: FakeDbCtx(store))


def test_log_prediction_inserts_and_returns_id(monkeypatch):
    store = {"rows": [], "next_id": 1, "select_rows": []}
    _install_fake_db(monkeypatch, store)
    report = {
        "ok": True, "symbol": "WOLF", "engine": "e", "ts": 100,
        "prediction": {"direction": "UP", "confidence": 0.7, "accuracy_grade": "B"},
        "market_regime": {"label": "risk_on", "risk_state": "risk_on", "conviction_multiplier": 1.1},
        "coverage": {"available": 18}, "risk_plan": {"entry": 100.0, "stop_loss": 95.0, "target_price": 110.0},
        "checklist": [], "top_drivers": {}, "ai_brief": {},
    }
    rid = ledger.log_prediction(report)
    assert rid == 1
    assert len(store["rows"]) == 1


def test_log_prediction_skips_non_ok_report(monkeypatch):
    store = {"rows": [], "next_id": 1, "select_rows": []}
    _install_fake_db(monkeypatch, store)
    assert ledger.log_prediction({"ok": False, "symbol": "WOLF"}) is None
    assert ledger.log_prediction({"ok": True}) is None  # no symbol
    assert len(store["rows"]) == 0


def test_log_prediction_disabled(monkeypatch):
    monkeypatch.setenv("SUPER_GHOST_LEDGER", "0")
    assert ledger.log_prediction({"ok": True, "symbol": "WOLF", "prediction": {"direction": "UP"}}) is None


def test_get_accuracy_aggregates(monkeypatch):
    store = {"rows": [], "next_id": 1}
    # rows shape for accuracy query: direction, action, grade, confidence, conviction, regime_state, correct, ret
    store["select_rows"] = [
        ("UP", "WATCHLIST UP BIAS", "A", 0.82, 70.0, "risk_on", True, 6.0),
        ("UP", "WATCHLIST UP BIAS", "B", 0.72, 55.0, "risk_on", False, -2.0),
        ("DOWN", "HIGH-CONVICTION DOWN PREDICTION", "A", 0.85, 75.0, "risk_off", True, -4.0),
    ]
    _install_fake_db(monkeypatch, store)
    out = ledger.get_accuracy(horizon=5)
    assert out["ok"] is True
    # COUNT(*) returns len(rows)=0 here (we didn't append inserts), but resolved=3
    assert out["resolved_at_horizon"] == 3
    assert out["overall"]["n"] == 3
    assert out["overall"]["wins"] == 2
    assert out["overall"]["win_rate"] == round(2 / 3, 4)
    assert "A" in out["by_grade"] and out["by_grade"]["A"]["n"] == 2
    assert "80-100%" in out["by_confidence_tier"]


def test_get_if_followed_profit_factor_and_drawdown(monkeypatch):
    store = {"rows": [], "next_id": 1}
    # if-followed query rows: direction, action, grade, ret, correct
    store["select_rows"] = [
        ("UP", "WATCHLIST UP BIAS", "A", 10.0, True),    # +10
        ("UP", "WATCHLIST UP BIAS", "B", -4.0, False),   # -4
        ("DOWN", "HIGH-CONVICTION DOWN PREDICTION", "A", -6.0, True),  # short: +6
    ]
    _install_fake_db(monkeypatch, store)
    out = ledger.get_if_followed(horizon=5)
    assert out["ok"] is True
    assert out["followed_calls"] == 3
    assert out["wins"] == 2  # +10 and +6
    assert out["losses"] == 1  # -4
    # gross win=16, gross loss=4 => PF=4.0
    assert out["profit_factor"] == 4.0
    assert out["net_return_pct"] == 12.0  # 10 -4 +6
    assert out["avg_win_pct"] == 8.0
    assert out["max_drawdown_pct"] <= 0.0


def test_get_history_returns_rows(monkeypatch):
    store = {"rows": [], "next_id": 1}
    # history needs a full tuple matching _HISTORY_COLS length
    cols = ledger._HISTORY_COLS
    fake = tuple(range(len(cols)))
    store["select_rows"] = [fake]
    _install_fake_db(monkeypatch, store)
    out = ledger.get_history(limit=10)
    assert out["ok"] is True
    assert out["count"] == 1
    assert out["rows"][0]["id"] == 0
    assert set(out["rows"][0].keys()) >= set(cols)


def test_auto_log_watchlist_skips_recently_logged_symbols(monkeypatch):
    """The resolver job fires hourly; the daily guard must make repeat calls
    no-ops so volume stays ~43/day, not ~1,032/day of correlated duplicates."""
    from config.symbols import OFFICIAL_WATCHLIST
    built = []
    monkeypatch.setattr(ledger, "ledger_enabled", lambda: True)
    monkeypatch.setattr(ledger, "_symbols_logged_since", lambda cutoff: set(OFFICIAL_WATCHLIST))
    monkeypatch.setattr("core.super_ghost.build_super_ghost",
                        lambda sym: built.append(sym) or {"ok": True, "symbol": sym})
    monkeypatch.setattr(ledger, "log_prediction", lambda report: 1)
    out = ledger.auto_log_watchlist()
    assert out["ok"] is True
    assert out["auto_logged"] == 0
    assert out["skipped_recent"] == len(OFFICIAL_WATCHLIST)
    assert built == []  # no expensive report builds for already-logged symbols


def test_auto_log_watchlist_logs_only_missing_symbols(monkeypatch):
    from config.symbols import OFFICIAL_WATCHLIST
    all_syms = list(OFFICIAL_WATCHLIST)
    already = set(all_syms[:-2])  # all but the last two logged today
    built = []
    monkeypatch.setattr(ledger, "_symbols_logged_since", lambda cutoff: already)
    monkeypatch.setattr("core.super_ghost.build_super_ghost",
                        lambda sym: built.append(sym) or {"ok": True, "symbol": sym})
    monkeypatch.setattr(ledger, "log_prediction", lambda report: 7)
    out = ledger.auto_log_watchlist()
    assert out["ok"] is True
    assert out["auto_logged"] == 2
    assert out["skipped_recent"] == len(all_syms) - 2
    assert sorted(built) == sorted(all_syms[-2:])


def test_resolve_predictions_updates_rows(monkeypatch):
    # One unresolved UP row for WOLF; provide a rising price series.
    t0 = 1_700_000_000
    store = {"rows": [], "next_id": 1}
    # resolve query selects: id, symbol, created_at, reference_price, direction,
    # target_price, stop_loss, resolved_1d_at, resolved_5d_at, resolved_20d_at
    store["select_rows"] = [
        (1, "WOLF", t0, 100.0, "UP", 110.0, 95.0, None, None, None),
    ]
    _install_fake_db(monkeypatch, store)
    closes = [100 + i for i in range(1, 21)]
    series = _series_from_closes(t0, closes)
    monkeypatch.setattr(ledger, "_ohlc_series", lambda sym, period="6mo": series)
    out = ledger.resolve_predictions(now=t0 + 40 * 86400)
    assert out["ok"] is True
    assert out["updated"] == 1
    assert out["horizons_filled"] >= 3
