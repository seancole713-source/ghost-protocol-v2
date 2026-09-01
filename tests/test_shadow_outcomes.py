"""Tests for shadow scoring (core.shadow_outcomes) — pure helpers, no DB."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from core.shadow_outcomes import (
    PROB_FLOOR,
    _bucket_for,
    _eval_entry_price,
    aggregate_shadow_stats,
    pick_daily_first,
)


def test_pick_daily_first_keeps_earliest_post_close_per_symbol_day():
    base = 1781035800  # 2026-06-09 15:10 CT
    evals = [
        {"symbol": "STUB", "eval_ts": base + 600},
        {"symbol": "STUB", "eval_ts": base},          # earliest — kept
        {"symbol": "STUB", "eval_ts": base + 1200},
        {"symbol": "FLNC", "eval_ts": base + 100},
    ]
    out = pick_daily_first(evals)
    by_sym = {e["symbol"]: e for e in out}
    assert len(out) == 2
    assert by_sym["STUB"]["eval_ts"] == base
    assert by_sym["FLNC"]["eval_ts"] == base + 100


def test_seed_filters_unpriced_before_grouping():
    """An earlier same-day eval without a price must not block a priced one."""
    base = 1781035800
    evals = [
        {"symbol": "STUB", "eval_ts": base, "scores": {}},               # unpriced
        {"symbol": "STUB", "eval_ts": base + 600, "scores": {"price": 9.5}},
    ]
    priced = [ev for ev in evals if _eval_entry_price(ev) is not None]
    out = pick_daily_first(priced)
    assert len(out) == 1
    assert out[0]["eval_ts"] == base + 600


def test_pick_daily_first_separate_days_kept():
    evals = [
        {"symbol": "STUB", "eval_ts": 1781035800},
        {"symbol": "STUB", "eval_ts": 1781035800 + 3 * 86400},
    ]
    assert len(pick_daily_first(evals)) == 2


def test_pick_daily_first_rejects_weekend_evaluations():
    central = ZoneInfo("America/Chicago")
    evals = [
        {"symbol": "STUB", "eval_ts": int(datetime(2026, 8, 1, 15, 10, tzinfo=central).timestamp())},
        {"symbol": "STUB", "eval_ts": int(datetime(2026, 8, 2, 15, 10, tzinfo=central).timestamp())},
        {"symbol": "STUB", "eval_ts": int(datetime(2026, 8, 3, 15, 10, tzinfo=central).timestamp())},
    ]

    out = pick_daily_first(evals)

    assert len(out) == 1
    assert out[0]["eval_ts"] == evals[2]["eval_ts"]


def test_pick_daily_first_rejects_intraday_daily_model_samples():
    central = ZoneInfo("America/Chicago")
    out = pick_daily_first([
        {"symbol": "STUB", "eval_ts": int(datetime(2026, 8, 3, 10, tzinfo=central).timestamp())},
        {"symbol": "STUB", "eval_ts": int(datetime(2026, 8, 3, 15, 10, tzinfo=central).timestamp())},
    ])
    assert len(out) == 1
    assert datetime.fromtimestamp(out[0]["eval_ts"], central).hour == 15


def test_pick_daily_first_keeps_same_day_model_generations():
    base = 1781035800
    def _ev(ts, sha):
        return {
            "symbol": "STUB", "eval_ts": ts, "direction": "UP",
            "scores": {
                "up_prob": 0.72,
                "model_identity_by_direction": {
                    "UP": {"model_sha256": sha, "feature_schema": "feature-v1",
                           "label_schema": "label-v1",
                           "validation_schema": "validation-v1", "hold_bars": 5},
                },
            },
        }
    out = pick_daily_first([
        _ev(base, "sha-old"), _ev(base + 60, "sha-old"),
        _ev(base + 120, "sha-new"),
    ])
    assert len(out) == 2
    assert [ev["scores"]["model_identity_by_direction"]["UP"]["model_sha256"]
            for ev in out] == ["sha-new", "sha-old"]


def test_eval_entry_price_prefers_fired_entry():
    ev = {"entry_price": 65.21, "scores": {"price": 64.0}}
    assert _eval_entry_price(ev) == 65.21


def test_eval_entry_price_falls_back_to_scan_price():
    assert _eval_entry_price({"entry_price": None, "scores": {"price": 12.5}}) == 12.5
    # psycopg may hand back JSON as a string
    assert _eval_entry_price({"scores": json.dumps({"price": 3.3})}) == 3.3
    assert _eval_entry_price({"scores": {}}) is None
    assert _eval_entry_price({}) is None


def test_bucket_for_edges():
    assert _bucket_for(None) == "unknown"
    assert _bucket_for(0.40) == "weak"
    assert _bucket_for(0.50) == "near"
    assert _bucket_for(PROB_FLOOR) == "fireable"
    assert _bucket_for(0.80) == "fireable"


def test_aggregate_shadow_stats_per_symbol_and_buckets():
    rows = [
        {"symbol": "STUB", "eval_ts": 1, "up_prob": 0.56, "outcome": "WIN", "pnl_pct": 2.0},
        {"symbol": "STUB", "eval_ts": 2, "up_prob": 0.57, "outcome": "LOSS", "pnl_pct": -1.3},
        {"symbol": "STUB", "eval_ts": 3, "up_prob": 0.52, "outcome": "WIN", "pnl_pct": 2.0},
        {"symbol": "FLNC", "eval_ts": 1, "up_prob": 0.51, "outcome": "EXPIRED", "pnl_pct": 0.4},
        {"symbol": "AMC", "eval_ts": 9, "up_prob": 0.49, "outcome": None, "pnl_pct": None},
    ]
    out = aggregate_shadow_stats(rows)
    assert out["resolved"] == 4
    assert out["pending"] == 1

    stub = next(s for s in out["symbols"] if s["symbol"] == "STUB")
    assert stub["n"] == 3
    assert stub["wins"] == 2 and stub["losses"] == 1
    assert stub["tp_rate_pct"] == 66.7
    assert stub["last_outcome"] == "WIN"

    flnc = next(s for s in out["symbols"] if s["symbol"] == "FLNC")
    assert flnc["expired"] == 1
    assert flnc["tp_rate_pct"] == 0.0  # expiry is resolved evidence, not a win

    assert out["buckets"]["up:fireable"]["n"] == 2
    assert out["buckets"]["up:fireable"]["tp_rate_pct"] == 50.0
    assert out["buckets"]["up:near"]["n"] == 2


def test_aggregate_shadow_stats_separates_direction_and_generation():
    rows = [
        {"symbol": "WOLF", "direction": "UP", "model_sha256": "old",
         "feature_schema": "f1", "model_prob": 0.7, "eval_ts": 1,
         "outcome": "WIN", "pnl_pct": 2},
        {"symbol": "WOLF", "direction": "UP", "model_sha256": "new",
         "feature_schema": "f2", "model_prob": 0.7, "eval_ts": 2,
         "outcome": "LOSS", "pnl_pct": -1},
        {"symbol": "WOLF", "direction": "DOWN", "model_sha256": "new",
         "feature_schema": "f2", "model_prob": 0.8, "eval_ts": 3,
         "outcome": "EXPIRED", "pnl_pct": 0},
    ]
    out = aggregate_shadow_stats(rows)
    assert len(out["symbols"]) == 3
    assert set(out["buckets"]) == {"up:fireable", "down:fireable"}
    assert out["buckets"]["down:fireable"]["tp_rate_pct"] == 0.0


def test_aggregate_shadow_stats_sorts_by_tp_rate():
    rows = [
        {"symbol": "AAA", "eval_ts": 1, "up_prob": 0.6, "outcome": "LOSS", "pnl_pct": -1},
        {"symbol": "BBB", "eval_ts": 1, "up_prob": 0.6, "outcome": "WIN", "pnl_pct": 2},
    ]
    out = aggregate_shadow_stats(rows)
    assert [s["symbol"] for s in out["symbols"]] == ["BBB", "AAA"]


def test_format_candidate_lines():
    from core.telegram_cards import format_candidate_lines

    lines = format_candidate_lines([
        {"symbol": "STUB", "up_prob": 0.5483, "min_win_proba": 0.55, "skip_code": "v3_prob_low"},
        {"symbol": "HOOD", "up_prob": 0.58, "min_win_proba": 0.55, "fired": True},
        {"symbol": "GME", "up_prob": None},
    ])
    assert lines[0] == "1. STUB 54.8% (needs 55%) — prob below floor"
    assert lines[1] == "2. HOOD 58.0% (needs 55%) — FIRED"
    assert len(lines) == 2  # missing up_prob dropped


def test_silence_card_includes_leaderboard():
    from core.telegram_cards import format_silence_card

    out = format_silence_card({
        "ghost_score": 46,
        "reason": "v3 model prob below BUY floor (floor 55%)",
        "top_candidates": [
            {"symbol": "STUB", "up_prob": 0.5483, "min_win_proba": 0.55, "skip_code": "v3_prob_low"},
        ],
    })
    assert "Closest candidates today:" in out
    assert "STUB 54.8%" in out
    assert "Next scan:" in out


def test_silence_card_no_leaderboard_when_empty():
    from core.telegram_cards import format_silence_card

    out = format_silence_card({"ghost_score": 46, "reason": "x"})
    assert "Closest candidates" not in out


def test_resolve_shadow_rows_writes_outcome(monkeypatch):
    """Resolver closes a virtual pick once the hold window + bar path decide."""
    import datetime as _dt

    from core import shadow_outcomes as so

    entry_ts = int(_dt.datetime(2026, 5, 20, 15, 0, tzinfo=_dt.timezone.utc).timestamp())
    expires = int(_dt.datetime(2026, 5, 28, 21, 0, tzinfo=_dt.timezone.utc).timestamp())
    pending_row = (1, "STUB", entry_ts, 10.0, 10.2, 9.87, expires, "UP", 3)
    updates = []

    class _Cur:
        def __init__(self):
            self.last_sql = ""

        def execute(self, sql, params=None):
            self.last_sql = sql
            if "UPDATE ghost_shadow_outcomes" in sql:
                updates.append(params)

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

    import core.signal_engine as _se

    monkeypatch.setattr(
        _se,
        "_fetch_ohlcv",
        lambda sym, atype, period="3m": [
            {"ts": "2026-05-21", "high": 10.05, "low": 9.95, "close": 10.0},
            {"ts": "2026-05-22", "high": 10.05, "low": 9.95, "close": 10.0},
            {"ts": "2026-05-23", "high": 10.05, "low": 9.95, "close": 10.0},
        ],
    )
    n = so.resolve_shadow_rows(max_symbols=5)
    assert n == 1
    assert updates
    assert updates[0][0] in ("WIN", "LOSS", "EXPIRED")
    expected_close = int(_dt.datetime(2026, 5, 23, 21, 0, tzinfo=_dt.timezone.utc).timestamp())
    assert updates[0][3] == expected_close


def test_regime_blocked_eval_still_scores_up_prob(monkeypatch):
    """Full-44 shadow coverage: a regime-gated symbol must still journal
    up_prob (model scored before the gate is enforced), and the firing
    behavior must be unchanged (None, 'regime_gate')."""
    import time as _t

    import numpy as _np

    import core.signal_engine as _se

    rows = []
    for i in range(220):
        px = 100.0 + i * 0.4
        rows.append({"ts": "2026-05-20T%02d:00:00Z" % (i % 24),
                     "open": px - 0.2, "high": px + 0.5, "low": px - 0.5,
                     "close": px, "volume": 1000 + i * 5})
    monkeypatch.setattr(_se, "_fetch_ohlcv",
                        lambda s, a, period="5d", interval="1h": rows)

    class _M:
        def predict_proba(self, X):
            return _np.array([[0.4, 0.6]])

    meta = {"edge": 0.3, "accuracy": 0.66, "wf_acc_mean": 0.64,
            "wf_edge_mean": 0.2, "wf_fold_count": 4, "trained_at": _t.time(),
            "precision_gate": {"ok": True, "threshold": 0.55, "target": 0.70}}
    monkeypatch.setattr(_se, "load_model", lambda s, direction="UP": (_M(), _se.FEATURE_COLS, meta))
    # Uptrend clears gates 1-2; force the SMA5 gate to block.
    monkeypatch.setattr(_se, "_block_up_below_sma5",
                        lambda s, a, px: (True, px * 1.1, px))
    for k, v in {"V3_MIN_WIN_PROBA": "0.55", "V3_MIN_EDGE": "0.0",
                 "V3_MIN_HOLDOUT_ACC": "0.0", "V3_MIN_WF_ACC_MEAN": "0.0",
                 "REGIME_GATE_SMA5_TREND_UP_BYPASS": "0",
                 "GHOST_REGIME_CALIBRATION": "0"}.items():
        monkeypatch.setenv(k, v)

    scores = {}
    sig, reason = _se.predict_live_ex("WOLF", "stock", scores=scores)
    assert sig is None
    assert reason == "regime_gate"
    assert scores.get("up_prob") == 0.6
    assert scores.get("model_meta", {}).get("min_win_proba") == 0.55


def test_resolve_shadow_rows_keeps_pending_when_bars_unavailable(monkeypatch):
    """A wall-clock deadline without the full bar path is not mature evidence."""
    import datetime as _dt
    from core import shadow_outcomes as so

    entry_ts = int(_dt.datetime(2026, 5, 20, 15, 0, tzinfo=_dt.timezone.utc).timestamp())
    expires = int(_dt.datetime(2026, 5, 28, 21, 0, tzinfo=_dt.timezone.utc).timestamp())
    pending_row = (9, "NOFEED", entry_ts, 10.0, 10.2, 9.87, expires, "UP", 3)
    updates = []

    class _Cur:
        def __init__(self):
            self.last_sql = ""
        def execute(self, sql, params=None):
            self.last_sql = sql
            if "UPDATE ghost_shadow_outcomes" in sql:
                updates.append(params)
        def fetchall(self):
            if "outcome IS NULL" in self.last_sql:
                return [pending_row]
            return []

    class _Conn:
        def cursor(self): return _Cur()
    class _Ctx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr("core.db.db_conn", lambda: _Ctx())
    monkeypatch.setattr(so, "ensure_shadow_table", lambda cur: None)
    monkeypatch.setattr(so.time, "time", lambda: expires + 3600)

    import core.signal_engine as _se
    monkeypatch.setattr(_se, "_fetch_ohlcv", lambda *a, **k: [])

    n = so.resolve_shadow_rows(max_symbols=5)
    assert n == 0
    assert updates == []


def test_resolve_shadow_rows_rotates_start_point_across_cycles(monkeypatch):
    """Every pending symbol must get a resolution attempt within a bounded
    number of cycles, not just whichever ones sort alphabetically first.

    Confirmed live 2026-09-01: 448 pending shadow rows across 103+ symbols,
    max_symbols=60 processed alphabetically from "A" every cycle, oldest
    pending eval from 2026-08-17, last cycle resolved 0 new rows -- anything
    past the alphabetical head of the 60-symbol budget was never attempted.
    This test uses 12 symbols and a budget of 5 (bars always unavailable, so
    every row stays pending) and checks that the SECOND cycle does not repeat
    the FIRST cycle's symbols, and that three cycles cover the full universe.
    """
    import datetime as _dt
    from core import shadow_outcomes as so

    entry_ts = int(_dt.datetime(2026, 5, 20, 15, 0, tzinfo=_dt.timezone.utc).timestamp())
    expires = int(_dt.datetime(2026, 5, 28, 21, 0, tzinfo=_dt.timezone.utc).timestamp())
    symbols = [f"SYM{i:02d}" for i in range(1, 13)]  # 12 symbols, already sorted
    pending_rows = [
        (i, sym, entry_ts, 10.0, 10.2, 9.87, expires, "UP", 3)
        for i, sym in enumerate(symbols, start=1)
    ]

    ghost_state_store: dict = {}

    class _Cur:
        def __init__(self):
            self.last_sql = ""
            self.last_params = None

        def execute(self, sql, params=None):
            self.last_sql = sql
            self.last_params = params
            if "INSERT INTO ghost_state" in sql:
                key, val = params
                ghost_state_store[key] = val

        def fetchall(self):
            if "outcome IS NULL" in self.last_sql:
                return list(pending_rows)
            return []

        def fetchone(self):
            if "SELECT val FROM ghost_state" in self.last_sql:
                val = ghost_state_store.get(self.last_params[0])
                return (val,) if val is not None else None
            return None

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

    import core.signal_engine as _se

    fetched_this_cycle: list = []

    def _fake_fetch(sym, atype, period="3m"):
        fetched_this_cycle.append(sym)
        return []  # no bar path -> row stays pending, isolates the attempt

    monkeypatch.setattr(_se, "_fetch_ohlcv", _fake_fetch)

    attempts_per_cycle = []
    for _ in range(3):
        fetched_this_cycle.clear()
        so.resolve_shadow_rows(max_symbols=5)
        attempts_per_cycle.append(list(fetched_this_cycle))

    cycle1, cycle2, cycle3 = attempts_per_cycle
    assert cycle1 == symbols[0:5]
    assert cycle2 == symbols[5:10]
    assert cycle3 == symbols[10:12] + symbols[0:3]
    # The old bug: every cycle restarted at "A" and re-attempted the same
    # alphabetical head instead of advancing past it.
    assert set(cycle1).isdisjoint(cycle2)
    assert set(cycle1) | set(cycle2) | set(cycle3) == set(symbols)


def test_seed_persists_regime_label_into_durable_column(monkeypatch):
    """seed_shadow_rows must copy the eval's regime_label into the outcome row.

    The 70+ slice search conditions on regime, and the perf-eval join source is
    pruned after ~90 days while shadow outcomes are not - the durable column is
    what keeps a forward proof's conditioning signal from decaying.
    """
    import core.shadow_outcomes as so

    base = 1781035800
    # symbol, eval_ts, up_prob, confidence, skip_code, fired,
    # entry_price, target_price, stop_price, scores, regime_label, direction,
    # prob_model_raw, prob_train_calibrated, prob_live_recalibrated, confidence_final
    scores = {
        "price": 10.0, "up_prob": 0.72, "winning_direction": "UP",
        "model_identity_by_direction": {
            "UP": {"model_sha256": "sha-up", "feature_schema": "feature-v1",
                   "label_schema": "label-v1",
                   "validation_schema": "validation-v1", "hold_bars": 5},
        },
    }
    eval_rows = [
        ("WOLF", base, 0.72, 0.72, None, True, 10.0, 10.6, 9.7,
         scores, "Trend-up", "UP", 0.74, 0.72, 0.70, 0.68),
    ]
    captured = {}

    class _Cur:
        rowcount = 0

        def execute(self, sql, params=None):
            self._last = sql
            s = sql.strip()
            if s.startswith("SELECT pg_try_advisory_xact_lock"):
                self._fetch = (True,)
            elif "FROM ghost_perf_symbol_evals" in sql and "SELECT symbol" in sql:
                self._fetch = None
                self._rows = list(eval_rows)
            elif s.startswith("INSERT INTO ghost_shadow_outcomes"):
                captured["insert_sql"] = sql
                captured["insert_params"] = params
                self.rowcount = 1
            else:
                self._fetch = None

        def fetchone(self):
            return getattr(self, "_fetch", None)

        def fetchall(self):
            return getattr(self, "_rows", [])

    class _Conn:
        def cursor(self):
            return _Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import core.db as db
    monkeypatch.setattr(db, "db_conn", lambda: _Conn())
    monkeypatch.setattr(so, "ensure_shadow_table", lambda cur: None)
    monkeypatch.setattr("core.tp_sl_resolve.label_hold_bars", lambda: 5)
    monkeypatch.setattr(
        "core.tp_sl_resolve.expires_at_nth_trading_close", lambda ts, hold: ts + 5 * 86400
    )

    so.seed_shadow_rows(days_back=3)
    assert "regime_label" in captured.get("insert_sql", "")
    assert captured["insert_params"][12] == "Trend-up"
    assert captured["insert_params"][22:] == (
        "sha-up", "feature-v1", "label-v1", "validation-v1", 5,
    )


def test_shadow_identity_selects_down_lane_and_requires_generation():
    from core.shadow_outcomes import _shadow_identity

    scores = {
        "winning_direction": "DOWN", "up_prob": 0.21, "down_prob": 0.83,
        "model_identity_by_direction": {
            "DOWN": {"model_sha256": "sha-down", "feature_schema": "feature-v2",
                     "label_schema": "label-v2",
                     "validation_schema": "validation-v3", "hold_bars": 5},
        },
    }
    out = _shadow_identity({"scores": scores})
    assert out == {
        "direction": "DOWN", "model_prob": 0.83, "model_sha256": "sha-down",
        "feature_schema": "feature-v2", "label_schema": "label-v2",
        "validation_schema": "validation-v3", "hold_bars": 5,
    }
    del scores["model_identity_by_direction"]["DOWN"]["model_sha256"]
    assert _shadow_identity({"scores": scores}) == {}


def test_shadow_identity_requires_exact_hold_horizon():
    from core.shadow_outcomes import _shadow_identity
    for bad in (True, 3.5, float("nan"), float("inf")):
        scores = {
            "winning_direction": "UP", "up_prob": 0.7,
            "model_identity_by_direction": {
                "UP": {"model_sha256": "sha", "feature_schema": "feature",
                       "label_schema": "label",
                       "validation_schema": "validation", "hold_bars": bad},
            },
        }
        assert _shadow_identity({"scores": scores}) == {}


def test_pick_daily_first_handles_mixed_legacy_and_current_rows():
    base = 1781035800
    legacy = {"symbol": "STUB", "eval_ts": base}
    current = {
        "symbol": "STUB", "eval_ts": base + 60, "direction": "UP",
        "scores": {"up_prob": 0.7, "model_identity_by_direction": {
            "UP": {"model_sha256": "sha", "feature_schema": "feature",
                   "label_schema": "label",
                   "validation_schema": "validation", "hold_bars": 5},
        }},
    }
    assert len(pick_daily_first([legacy, current])) == 2


def test_seed_query_and_conflict_are_direction_neutral(monkeypatch):
    """DOWN-only rows are read and exact generation uniqueness is delegated to DB."""
    import core.shadow_outcomes as so

    base = 1781035800
    scores = {
        "price": 10.0, "down_prob": 0.81, "winning_direction": "DOWN",
        "model_identity_by_direction": {
            "DOWN": {"model_sha256": "sha-down", "feature_schema": "feature-v2",
                     "label_schema": "label-v2",
                     "validation_schema": "validation-v3", "hold_bars": 5},
        },
    }
    eval_rows = [
        ("WOLF", base, None, None, "down_shadow_only", False, None, None, None,
         scores, "Trend-down", "DOWN", 0.82, 0.81, None, None),
    ]
    captured = {}

    class _Cur:
        rowcount = 0
        def execute(self, sql, params=None):
            self._last = sql
            stripped = sql.strip()
            if stripped.startswith("SELECT pg_try_advisory_xact_lock"):
                self._fetch = (True,)
            elif "FROM ghost_perf_symbol_evals" in sql and "SELECT symbol" in sql:
                captured["select_sql"] = sql
                self._rows = list(eval_rows)
                self._fetch = None
            elif stripped.startswith("INSERT INTO ghost_shadow_outcomes"):
                captured["insert_sql"] = sql
                captured["insert_params"] = params
                self.rowcount = 1
            else:
                self._fetch = None
        def fetchone(self):
            return getattr(self, "_fetch", None)
        def fetchall(self):
            return getattr(self, "_rows", [])

    class _Conn:
        def cursor(self): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *args): return False

    monkeypatch.setattr("core.db.db_conn", lambda: _Conn())
    monkeypatch.setattr(so, "ensure_shadow_table", lambda cur: None)
    monkeypatch.setattr("core.tp_sl_resolve.label_hold_bars", lambda: 5)
    monkeypatch.setattr(
        "core.tp_sl_resolve.expires_at_nth_trading_close", lambda ts, hold: ts + 5 * 86400,
    )

    assert so.seed_shadow_rows(days_back=3) == 1
    assert "up_prob IS NOT NULL" not in captured["select_sql"]
    assert "ON CONFLICT DO NOTHING" in captured["insert_sql"]
    assert "(symbol, trade_date)" not in captured["insert_sql"].split("ON CONFLICT", 1)[1]
    assert captured["insert_params"][16:23] == (
        "DOWN", 0.81, 0.82, 0.81, None, None, "sha-down",
    )
    assert captured["insert_params"][23:] == (
        "feature-v2", "label-v2", "validation-v3", 5,
    )


def test_shadow_table_uses_generation_identity_uniqueness():
    import core.shadow_outcomes as so

    sql = []
    class _Cur:
        def execute(self, statement, params=None): sql.append(statement)

    so.ensure_shadow_table(_Cur())
    joined = " ".join(sql)
    assert "DROP CONSTRAINT IF EXISTS ghost_shadow_outcomes_symbol_trade_date_key" in joined
    assert "idx_shadow_generation_daily_v2" in joined
    assert "model_sha256" in joined and "feature_schema" in joined


def test_regime_flag_helper_reads_scores_regime():
    from core.shadow_outcomes import _regime_flag
    ev = {"scores": {"regime": {"adx_trending": 1, "above_ema200": 0}}}
    assert _regime_flag(ev, "adx_trending") == 1
    assert _regime_flag(ev, "above_ema200") == 0
    assert _regime_flag(ev, "ema_trend_bullish") is None
    # JSON-string scores (psycopg variance)
    import json as _j
    ev2 = {"scores": _j.dumps({"regime": {"adx_trending": 1}})}
    assert _regime_flag(ev2, "adx_trending") == 1
    assert _regime_flag({"scores": {}}, "adx_trending") is None
    assert _regime_flag({}, "adx_trending") is None


def test_seed_persists_regime_gate_flags_into_durable_columns(monkeypatch):
    """seed_shadow_rows must copy adx_trending/above_ema200/ema_trend_bullish
    from the eval scores.regime into the durable outcome columns."""
    import core.shadow_outcomes as so

    base = 1781035800
    # symbol, eval_ts, up_prob, confidence, skip_code, fired,
    # entry_price, target_price, stop_price, scores, regime_label, direction,
    # probability lifecycle stages
    scores = {
        "price": 10.0, "up_prob": 0.72, "winning_direction": "UP",
        "regime": {"adx_trending": 1, "above_ema200": 0, "ema_trend_bullish": 1},
        "model_identity_by_direction": {
            "UP": {"model_sha256": "sha-up", "feature_schema": "feature-v1",
                   "label_schema": "label-v1",
                   "validation_schema": "validation-v1", "hold_bars": 5},
        },
    }
    eval_rows = [
        ("WOLF", base, 0.72, 0.72, None, True, 10.0, 10.6, 9.7,
         scores, "Trend-up", "UP", 0.74, 0.72, 0.70, 0.68),
    ]
    captured = {}

    class _Cur:
        rowcount = 0

        def execute(self, sql, params=None):
            self._last = sql
            s = sql.strip()
            if s.startswith("SELECT pg_try_advisory_xact_lock"):
                self._fetch = (True,)
            elif "FROM ghost_perf_symbol_evals" in sql and "SELECT symbol" in sql:
                self._fetch = None
                self._rows = list(eval_rows)
            elif s.startswith("INSERT INTO ghost_shadow_outcomes"):
                captured["insert_sql"] = sql
                captured["insert_params"] = params
                self.rowcount = 1
            else:
                self._fetch = None

        def fetchone(self):
            return getattr(self, "_fetch", None)

        def fetchall(self):
            return getattr(self, "_rows", [])

    class _Conn:
        def cursor(self):
            return _Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import core.db as db
    monkeypatch.setattr(db, "db_conn", lambda: _Conn())
    monkeypatch.setattr(so, "ensure_shadow_table", lambda cur: None)
    monkeypatch.setattr("core.tp_sl_resolve.label_hold_bars", lambda: 5)
    monkeypatch.setattr(
        "core.tp_sl_resolve.expires_at_nth_trading_close", lambda ts, hold: ts + 5 * 86400
    )

    so.seed_shadow_rows(days_back=3)
    sql = captured.get("insert_sql", "")
    for col in ("adx_trending", "above_ema200", "ema_trend_bullish"):
        assert col in sql
    params = captured["insert_params"]
    assert params[13] == 1   # adx_trending
    assert params[14] == 0   # above_ema200
    assert params[15] == 1   # ema_trend_bullish


# ---------------------------------------------------- shadow checklist lane --

from core import shadow_outcomes as so  # noqa: E402  (section-local alias, matches file style)

def test_shadow_market_ctx_rebuilds_only_from_the_frozen_row():
    """Nothing is re-fetched: a snapshot written hours after the eval can only
    see what the scan itself persisted."""
    scores = json.dumps({
        "price": 12.5,
        "features": {
            "feature_asof_ts": 1_766_000_000,
            "volume_ratio": 2.4,
            "mom_4h": 0.031,
            "macro_spy_20d_return": -0.012,
        },
    })
    ctx = so._shadow_market_ctx(scores)
    assert ctx["price"] == 12.5
    assert ctx["feature_asof_ts"] == 1_766_000_000
    assert ctx["price_as_of_ts"] == 1_766_000_000
    assert ctx["relative_volume"] == 2.4
    assert ctx["trend_slope_pct"] == 3.1
    assert ctx["market_move_pct"] == -1.2


def test_shadow_market_ctx_missing_data_stays_missing():
    assert so._shadow_market_ctx(None) == {}
    assert so._shadow_market_ctx("not json !!") == {}
    ctx = so._shadow_market_ctx({"price": 9.0})
    assert ctx == {"price": 9.0}  # no fabricated timestamps or ratios


def test_snapshot_shadow_checklists_freezes_at_eval_ts_with_shadow_link(monkeypatch):
    calls = {}

    def _fake_collect(symbol, *, asof_ts=None, market_ctx=None):
        calls["collect"] = {"symbol": symbol, "asof_ts": asof_ts, "ctx": market_ctx}
        return {"_asof_ts": asof_ts}

    def _fake_eval(symbol, direction, evidence):
        calls["eval"] = {"symbol": symbol, "direction": direction}
        return {"checklist_version": "v", "hold_bars": 3, "score_pct": 20.0, "blocked": False}

    stored = []
    monkeypatch.setattr("core.checklist_evidence.collect_evidence", _fake_collect)
    monkeypatch.setattr("core.catalyst_checklist.evaluate_checklist", _fake_eval)
    monkeypatch.setattr(
        "core.checklist_ledger.store_snapshot", lambda **kw: stored.append(kw) or 1,
    )

    rows = [{
        "shadow_outcome_id": 402, "symbol": "PYPL", "direction": "DOWN",
        "eval_ts": 1_766_000_000, "entry": 60.0, "target": 57.0, "stop": 62.0,
        "expires_at": 1_766_400_000,
        "scores": {"price": 60.0, "features": {"feature_asof_ts": 1_766_000_000}},
    }]
    written = so.snapshot_shadow_checklists(rows)

    assert written == 1
    assert calls["collect"]["asof_ts"] == 1_766_000_000
    assert calls["collect"]["ctx"]["price"] == 60.0
    assert calls["eval"]["direction"] == "DOWN"
    kw = stored[0]
    assert kw["issued_at"] == 1_766_000_000    # eval time, never snapshot time
    assert kw["lane"] == "shadow"
    assert kw["shadow_outcome_id"] == 402
    assert kw["deadline_ts"] == 1_766_400_000


def test_snapshot_shadow_checklists_budget_and_isolation(monkeypatch):
    """The per-run budget bounds network work, and one failing row never
    stops the rest."""
    monkeypatch.setattr(
        "core.checklist_evidence.collect_evidence",
        lambda symbol, *, asof_ts=None, market_ctx=None: {},
    )
    monkeypatch.setattr(
        "core.catalyst_checklist.evaluate_checklist",
        lambda symbol, direction, evidence: {
            "checklist_version": "v", "hold_bars": 3, "score_pct": 0.0, "blocked": False,
        },
    )
    stored = []

    def _store(**kw):
        if kw["symbol"] == "BAD":
            raise RuntimeError("db hiccup")
        stored.append(kw["symbol"])
        return 1

    monkeypatch.setattr("core.checklist_ledger.store_snapshot", _store)

    def _row(sym, sid):
        return {
            "shadow_outcome_id": sid, "symbol": sym, "direction": "UP",
            "eval_ts": 1, "entry": 1.0, "target": 2.0, "stop": 0.5,
            "expires_at": 2, "scores": None,
        }

    rows = [_row("BAD", 1), _row("A", 2), _row("B", 3), _row("C", 4)]
    written = so.snapshot_shadow_checklists(rows, budget=2)

    assert written == 2          # BAD failed, A and B fit the budget, C skipped
    assert stored == ["A", "B"]


def test_seed_passes_new_rows_to_checklist_after_the_transaction(monkeypatch):
    """The seeding transaction must close before any snapshot work begins,
    and only genuinely-new rows (RETURNING id) get snapshots."""
    eval_ts = 1781035800  # known post-close CT timestamp (matches sibling seed tests)
    state = {"in_txn": False}
    captured = {}

    class _Cur:
        rowcount = 0

        def execute(self, sql, params=None):
            stripped = " ".join(sql.split())
            if "pg_try_advisory_xact_lock" in stripped:
                self._fetch = (True,)
            elif stripped.startswith("SELECT symbol, eval_ts"):
                self._rows = [(
                    "WOLF", eval_ts, 0.61, 0.61, None, False,
                    None, None, None,
                    json.dumps({
                        "price": 25.0, "up_prob": 0.61,
                        "features": {"feature_asof_ts": eval_ts, "volume_ratio": 1.5},
                        "model_identity_by_direction": {"UP": {
                            "model_sha256": "a" * 64, "feature_schema": "f1",
                            "label_schema": "l1", "validation_schema": "v1",
                            "hold_bars": 5, "model_prob": 0.61,
                        }},
                    }),
                    None, "UP", 0.61, 0.61, None, 0.61,
                )]
                self._fetch = None
            elif stripped.startswith("INSERT INTO ghost_shadow_outcomes"):
                self.rowcount = 1
                self._fetch = (777,)
            else:
                self._fetch = None

        def fetchone(self):
            return getattr(self, "_fetch", None)

        def fetchall(self):
            return getattr(self, "_rows", [])

    class _Conn:
        def cursor(self):
            return _Cur()

        def __enter__(self):
            state["in_txn"] = True
            return self

        def __exit__(self, *a):
            state["in_txn"] = False
            return False

    import core.db as db
    monkeypatch.setattr(db, "db_conn", lambda: _Conn())
    monkeypatch.setattr(so, "ensure_shadow_table", lambda cur: None)
    monkeypatch.setattr("core.tp_sl_resolve.label_hold_bars", lambda: 5)
    monkeypatch.setattr(
        "core.tp_sl_resolve.expires_at_nth_trading_close", lambda ts, hold: ts + 5 * 86400
    )

    def _fake_snapshots(rows, **kw):
        captured["rows"] = rows
        captured["txn_open_during_snapshot"] = state["in_txn"]
        return len(rows)

    monkeypatch.setattr(so, "snapshot_shadow_checklists", _fake_snapshots)

    assert so.seed_shadow_rows(days_back=3) == 1
    assert captured["txn_open_during_snapshot"] is False
    row = captured["rows"][0]
    assert row["shadow_outcome_id"] == 777
    assert row["symbol"] == "WOLF"
    assert row["eval_ts"] == eval_ts
