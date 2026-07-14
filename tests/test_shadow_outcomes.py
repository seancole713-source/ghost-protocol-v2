"""Tests for shadow scoring (core.shadow_outcomes) — pure helpers, no DB."""
import json

from core.shadow_outcomes import (
    PROB_FLOOR,
    _bucket_for,
    _eval_entry_price,
    aggregate_shadow_stats,
    pick_daily_first,
)


def test_pick_daily_first_keeps_earliest_per_symbol_day():
    base = 1781000000  # all same CT day
    evals = [
        {"symbol": "STUB", "eval_ts": base + 3600},
        {"symbol": "STUB", "eval_ts": base},          # earliest — kept
        {"symbol": "STUB", "eval_ts": base + 7200},
        {"symbol": "FLNC", "eval_ts": base + 100},
    ]
    out = pick_daily_first(evals)
    by_sym = {e["symbol"]: e for e in out}
    assert len(out) == 2
    assert by_sym["STUB"]["eval_ts"] == base
    assert by_sym["FLNC"]["eval_ts"] == base + 100


def test_seed_filters_unpriced_before_grouping():
    """An earlier same-day eval without a price must not block a priced one."""
    base = 1781000000
    evals = [
        {"symbol": "STUB", "eval_ts": base, "scores": {}},               # unpriced
        {"symbol": "STUB", "eval_ts": base + 3600, "scores": {"price": 9.5}},
    ]
    priced = [ev for ev in evals if _eval_entry_price(ev) is not None]
    out = pick_daily_first(priced)
    assert len(out) == 1
    assert out[0]["eval_ts"] == base + 3600


def test_pick_daily_first_separate_days_kept():
    evals = [
        {"symbol": "STUB", "eval_ts": 1781000000},
        {"symbol": "STUB", "eval_ts": 1781000000 + 3 * 86400},
    ]
    assert len(pick_daily_first(evals)) == 2


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
    assert flnc["tp_rate_pct"] is None  # no TP/SL decisions yet

    assert out["buckets"]["fireable"]["n"] == 2
    assert out["buckets"]["fireable"]["tp_rate_pct"] == 50.0
    assert out["buckets"]["near"]["n"] == 2


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
    pending_row = (1, "STUB", entry_ts, 10.0, 10.2, 9.87, expires)
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


def test_resolve_shadow_rows_expires_when_bars_unavailable(monkeypatch):
    """Expired virtual picks with no bars must not stay pending forever.

    PR #151: if the resolver cannot fetch OHLCV, it closes already-expired
    virtual rows as EXPIRED at entry (0% P&L) instead of crediting WIN/LOSS.
    """
    import datetime as _dt
    from core import shadow_outcomes as so

    entry_ts = int(_dt.datetime(2026, 5, 20, 15, 0, tzinfo=_dt.timezone.utc).timestamp())
    expires = int(_dt.datetime(2026, 5, 28, 21, 0, tzinfo=_dt.timezone.utc).timestamp())
    pending_row = (9, "NOFEED", entry_ts, 10.0, 10.2, 9.87, expires)
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
    assert n == 1
    assert updates == [("EXPIRED", 10.0, 0.0, expires + 3600, 9)]


def test_seed_persists_regime_label_into_durable_column(monkeypatch):
    """seed_shadow_rows must copy the eval's regime_label into the outcome row.

    The 70+ slice search conditions on regime, and the perf-eval join source is
    pruned after ~90 days while shadow outcomes are not - the durable column is
    what keeps a forward proof's conditioning signal from decaying.
    """
    import core.shadow_outcomes as so

    base = 1781000000
    # symbol, eval_ts, up_prob, confidence, skip_code, fired,
    # entry_price, target_price, stop_price, scores, regime_label
    eval_rows = [
        ("WOLF", base, 0.72, 0.72, None, True, 10.0, 10.6, 9.7, {"price": 10.0}, "Trend-up"),
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
    # regime_label is the last positional param in the insert tuple.
    assert captured["insert_params"][-1] == "Trend-up"
