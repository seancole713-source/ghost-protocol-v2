"""PR #138: paper wallet — fill rules, config clamps, kill-switch flag."""
import core.paper_wallet as pw
from core.paper_wallet import exit_fill


def test_stop_fills_at_stop_when_touched():
    assert exit_fill(9.99, target=10.5, stop=10.0, expires_at=None, now=1) == (9.99, "stop")
    assert exit_fill(10.0, target=10.5, stop=10.0, expires_at=None, now=1) == (10.0, "stop")


def test_gap_through_stop_fills_at_gapped_price():
    # Overnight gap: price opens far below the stop — real slippage recorded.
    price, reason = exit_fill(9.20, target=10.5, stop=10.0, expires_at=None, now=1)
    assert (price, reason) == (9.20, "stop")


def test_target_fills_at_limit_or_better():
    # PR #162 symmetry fix: a resting limit sell fills at limit OR BETTER.
    # A gap up through the target books the gapped price — mirroring how a
    # gap down through the stop books the gapped price. Exact touch = limit.
    assert exit_fill(10.9, target=10.5, stop=9.5, expires_at=None, now=1) == (10.9, "target")
    assert exit_fill(10.5, target=10.5, stop=9.5, expires_at=None, now=1) == (10.5, "target")


def test_gap_fills_are_symmetric():
    # Same 5% gap on either side must book the same magnitude of surprise.
    down, _ = exit_fill(9.5, target=None, stop=10.0, expires_at=None, now=1)
    up, _ = exit_fill(11.025, target=10.5, stop=None, expires_at=None, now=1)
    assert down == 9.5      # stop gapped through: books the gap
    assert up == 11.025     # target gapped through: books the gap too


def test_stop_checked_before_target():
    # Degenerate data (stop above target): stop wins — conservative.
    price, reason = exit_fill(9.0, target=8.0, stop=9.5, expires_at=None, now=1)
    assert reason == "stop"


def test_expiry_closes_at_market():
    assert exit_fill(10.1, target=10.5, stop=9.5, expires_at=100, now=100) == (10.1, "expiry")
    assert exit_fill(10.1, target=10.5, stop=9.5, expires_at=100, now=99) is None


def test_no_exit_inside_band():
    assert exit_fill(10.0, target=10.5, stop=9.5, expires_at=None, now=1) is None


def test_reset_wallet_clamps_balance(monkeypatch):
    calls = {}

    class _Cur:
        def execute(self, sql, *a):
            calls.setdefault("sqls", []).append(sql)
        def fetchone(self):
            return None

    class _Conn:
        def cursor(self):
            return _Cur()
        def commit(self):
            calls["committed"] = True
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    import core.db as db
    monkeypatch.setattr(db, "db_conn", lambda: _Conn())
    out = pw.reset_wallet(5)          # below floor
    assert out["starting_balance"] == 100.0
    out = pw.reset_wallet(999_999_999)  # above ceiling
    assert out["starting_balance"] == 10_000_000.0
    out = pw.reset_wallet(10_000)
    assert out["starting_balance"] == 10_000.0
    assert calls.get("committed") is True


def test_wallet_kill_flag(monkeypatch):
    monkeypatch.setenv("PAPER_WALLET_ENABLED", "0")
    out = pw.run_wallet_cycle()
    assert out["ok"] is True and "skipped" in out


def test_never_touches_a_broker():
    # Guardrail tripwire: the module must not import broker order APIs or
    # reference the live orders endpoint. Fake money stays fake.
    import inspect
    src = inspect.getsource(pw)
    assert "/v2/orders" not in src
    assert "alpaca.markets/v2/orders" not in src


def test_fresh_bands_bracket_entry(monkeypatch):
    from core.paper_wallet import fresh_bands
    # PR #154: wallet geometry is +2% target; stop = 2% * WALLET mult (0.65 default).
    monkeypatch.delenv("PAPER_WALLET_STOP_VOL_MULT", raising=False)
    tgt, stp, exp = fresh_bands("NVDA", 100.0, now=1_000_000)
    assert tgt > 100.0 and stp < 100.0          # brackets the entry
    assert abs(tgt - 102.0) < 0.01              # +2.0%
    assert abs(stp - 98.7) < 0.01               # -1.3% (2% * 0.65)
    assert exp > 1_000_000                       # future expiry


def test_fresh_bands_never_precrossed(monkeypatch):
    # The whole point of Option B: a fresh entry can never be already-resolved.
    from core.paper_wallet import fresh_bands, exit_fill
    for mult in ("0.65", "1.8"):
        monkeypatch.setenv("PAPER_WALLET_STOP_VOL_MULT", mult)
        entry = 36.46
        tgt, stp, exp = fresh_bands("WOLF", entry, now=1_000_000)
        assert exit_fill(entry, tgt, stp, exp, 1_000_000) is None  # not pre-crossed


def test_wallet_stop_decoupled_from_model_mult(monkeypatch):
    # PR #154 geometry fix: the wallet must NOT inherit the model's global
    # V3_STOP_VOL_MULT. With the model at 1.8, the wallet stays at its own 0.65.
    import core.paper_wallet as pw
    monkeypatch.setenv("V3_STOP_VOL_MULT", "1.8")        # model geometry (prod)
    monkeypatch.delenv("PAPER_WALLET_STOP_VOL_MULT", raising=False)
    assert pw._wallet_stop_vol_mult() == 0.65
    tgt, stp, exp = pw.fresh_bands("NVDA", 100.0, now=1_000_000)
    assert abs(tgt - 102.0) < 0.01                       # +2.0% target
    assert abs(stp - 98.7) < 0.01                        # -1.3% stop (NOT -3.6%)


def test_wallet_stop_env_override(monkeypatch):
    import core.paper_wallet as pw
    monkeypatch.setenv("PAPER_WALLET_STOP_VOL_MULT", "0.9")
    assert pw._wallet_stop_vol_mult() == 0.9
    _, stp, _ = pw.fresh_bands("NVDA", 100.0, now=1_000_000)
    assert abs(stp - 98.2) < 0.01                        # -1.8% (2% * 0.9)


def test_geometry_stats_breakeven_math():
    import core.paper_wallet as pw
    # +2% target / -1.3% stop -> reward:risk ~1.54, break-even ~39.4%
    g = pw.geometry_stats(0.02, 0.013)
    assert abs(g["reward_risk"] - 1.5385) < 0.01
    assert abs(g["break_even_win_rate"] - 0.3939) < 0.01
    # +2% target / -3.6% stop (old model geometry) -> break-even ~64.3%
    g2 = pw.geometry_stats(0.02, 0.036)
    assert abs(g2["break_even_win_rate"] - 0.6429) < 0.01


def test_closed_trade_expectancy_flips_positive_with_tight_stop():
    import core.paper_wallet as pw
    # Same 44% win rate (4 wins / 5 losses ~ 44%). Old geometry loses; new wins.
    old = [{"pnl_pct": v} for v in [2.0, 2.0, 2.0, 2.0] + [-3.8] * 5]
    new = [{"pnl_pct": v} for v in [2.0, 2.0, 2.0, 2.0] + [-1.3] * 5]
    eo = pw.closed_trade_expectancy(old)
    en = pw.closed_trade_expectancy(new)
    assert eo["win_rate"] == en["win_rate"]              # identical predictions
    assert eo["profitable"] is False                     # -3.8% stop bleeds
    assert en["profitable"] is True                      # -1.3% stop profits
    assert en["expectancy_pct"] > 0 > eo["expectancy_pct"]


def test_expectancy_by_geometry_splits_legacy_from_current():
    import core.paper_wallet as pw
    # Current config: 2% vol * 0.65 mult = 0.013 stop fraction.
    # Legacy trades carry frozen -3.6% stops; current carry -1.3%.
    legacy = [{"entry_price": 100.0, "stop_price": 96.4, "pnl_pct": -3.7}] * 3
    current = [{"entry_price": 100.0, "stop_price": 98.7, "pnl_pct": 2.0}] * 4
    no_stop = [{"entry_price": 100.0, "stop_price": None, "pnl_pct": 1.0}]
    out = pw.expectancy_by_geometry(legacy + current + no_stop, 0.013)
    assert out["legacy_geometry"]["n"] == 3
    assert out["current_geometry"]["n"] == 4
    assert out["unknown_geometry_n"] == 1
    assert out["legacy_geometry"]["profitable"] is False
    assert out["current_geometry"]["profitable"] is True
    # WOLF's wider base vol (2.5% * 0.65 = 0.016) still counts as current.
    wolf = [{"entry_price": 100.0, "stop_price": 98.37, "pnl_pct": 2.0}]
    out2 = pw.expectancy_by_geometry(wolf, 0.013)
    assert out2["current_geometry"]["n"] == 1


def test_month_rollover_records_and_resets(monkeypatch):
    import core.paper_wallet as pw
    rows = {"daily": [("2026-07-31", 11500.0)], "trades_deleted": 0, "monthly": []}

    class _Cur:
        def __init__(self): self._last = ""
        def execute(self, sql, params=None):
            self._last = sql
            if "ghost_paper_monthly" in sql and "INSERT" in sql:
                rows["monthly"].append(params)
            if "DELETE FROM ghost_paper_trades" in sql:
                rows["trades_deleted"] += 1
        def fetchone(self):
            if "ghost_paper_daily ORDER BY" in self._last:
                return (rows["daily"][0][1],)
            return None
    cur = _Cur()
    # July config, but "today" is August → rollover fires
    monkeypatch.setattr(pw, "_month_key", lambda: "2026-08")
    cfg = {"starting_balance": 10000.0, "monthly_goal": 20000.0, "goal_month": "2026-07"}
    out = pw._maybe_roll_month(cur, cfg)
    assert out["goal_month"] == "2026-08"          # advanced to new month
    assert rows["trades_deleted"] == 1              # books wiped
    assert rows["monthly"], "prior month must be recorded"
    rec = rows["monthly"][0]
    assert rec[0] == "2026-07" and rec[3] == 11500.0 and rec[4] is False  # $11.5k < $20k goal


def test_month_no_rollover_same_month(monkeypatch):
    import core.paper_wallet as pw
    monkeypatch.setattr(pw, "_month_key", lambda: "2026-07")
    cfg = {"starting_balance": 10000.0, "monthly_goal": 20000.0, "goal_month": "2026-07"}

    class _Cur:
        def execute(self, *a): raise AssertionError("must not touch DB when same month")
        def fetchone(self): return None
    assert pw._maybe_roll_month(_Cur(), cfg) is cfg  # unchanged, no-op


def test_goal_cannot_be_below_start(monkeypatch):
    import core.paper_wallet as pw

    class _Cur:
        def execute(self, *a): pass
        def fetchone(self): return None
    class _Conn:
        def cursor(self): return _Cur()
        def commit(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
    import core.db as db
    monkeypatch.setattr(db, "db_conn", lambda: _Conn())
    monkeypatch.setattr(pw, "get_config", lambda cur: {"monthly_goal": 20000.0})
    out = pw.reset_wallet(10000.0, monthly_goal=5000.0)  # goal below start
    assert out["monthly_goal"] == 10000.0  # clamped up to start


def test_goal_pct_of_goal_is_positive_when_underwater():
    # PR #150: progress_pct goes negative underwater; pct_of_goal must stay
    # positive (equity/goal) so the bar and number agree.
    start, goal, equity = 10000.0, 20000.0, 9800.0
    progress_pct = round((equity - start) / (goal - start) * 100, 1)
    pct_of_goal = round(equity / goal * 100, 1)
    assert progress_pct < 0            # -2.0% (below start)
    assert pct_of_goal == 49.0         # still 49% of the way to $20k
    assert pct_of_goal > 0


def test_shadow_wallet_defaults_require_prob_floor_and_skill(monkeypatch):
    import inspect
    import core.paper_wallet as pw

    monkeypatch.delenv("PAPER_SHADOW_MIN_PROB", raising=False)
    monkeypatch.delenv("PAPER_SHADOW_SKILL_MIN_TP_RATE", raising=False)
    monkeypatch.delenv("PAPER_SHADOW_SKILL_MIN_RESOLVED", raising=False)

    assert pw._shadow_min_prob() == 0.55
    assert pw._shadow_skill_min_tp_rate() == 0.55
    assert pw._shadow_skill_min_resolved() == 10

    src = inspect.getsource(pw.run_wallet_cycle)
    assert "COALESCE(s.resolved, 0) >= %s" in src
    assert "COALESCE(s.wins, 0)::float" in src


def test_shadow_wallet_env_overrides_skill_filter(monkeypatch):
    import core.paper_wallet as pw

    monkeypatch.setenv("PAPER_SHADOW_MIN_PROB", "0.60")
    monkeypatch.setenv("PAPER_SHADOW_SKILL_MIN_TP_RATE", "0.62")
    monkeypatch.setenv("PAPER_SHADOW_SKILL_MIN_RESOLVED", "18")
    assert pw._shadow_min_prob() == 0.60
    assert pw._shadow_skill_min_tp_rate() == 0.62
    assert pw._shadow_skill_min_resolved() == 18


# ---------------------------------------------------------------- PR #163
# Session gate: entries only in the RTH window; exits always run.

def _ct(hour, minute, weekday_date="2026-07-08"):  # a Wednesday
    from datetime import datetime
    from zoneinfo import ZoneInfo
    y, m, d = map(int, weekday_date.split("-"))
    return datetime(y, m, d, hour, minute, tzinfo=ZoneInfo("America/Chicago"))


def test_entry_window_open_midday(monkeypatch):
    monkeypatch.delenv("PAPER_SESSION_GATE", raising=False)
    out = pw.entry_window(_ct(11, 0))
    assert out["open"] is True


def test_entry_window_blocks_overnight_and_weekend(monkeypatch):
    monkeypatch.delenv("PAPER_SESSION_GATE", raising=False)
    assert pw.entry_window(_ct(2, 0))["open"] is False       # pre-market night
    assert pw.entry_window(_ct(18, 0))["open"] is False      # after hours
    # Saturday 2026-07-11
    assert pw.entry_window(_ct(11, 0, "2026-07-11"))["open"] is False


def test_entry_window_blocks_open_and_close_buffers(monkeypatch):
    monkeypatch.delenv("PAPER_SESSION_GATE", raising=False)
    monkeypatch.delenv("PAPER_ENTRY_OPEN_BUFFER_MIN", raising=False)
    monkeypatch.delenv("PAPER_ENTRY_CLOSE_BUFFER_MIN", raising=False)
    assert pw.entry_window(_ct(8, 35))["open"] is False      # first 15 min
    assert pw.entry_window(_ct(8, 46))["open"] is True       # after buffer
    assert pw.entry_window(_ct(14, 31))["open"] is False     # last 30 min
    assert pw.entry_window(_ct(14, 29))["open"] is True


def test_entry_window_kill_switch(monkeypatch):
    monkeypatch.setenv("PAPER_SESSION_GATE", "off")
    assert pw.entry_window(_ct(2, 0))["open"] is True


# ---------------------------------------------------------------- PR #163
# ATR bands: per-symbol realized vol for WALLET brackets only.

def _bars(rng_pct, n=15, px=100.0):
    # daily bars with a constant high-low range of rng_pct
    return [{"open": px, "high": px * (1 + rng_pct / 2), "low": px * (1 - rng_pct / 2),
             "close": px, "volume": 1000} for _ in range(n)]


def test_wallet_vol_uses_realized_range_when_wider(monkeypatch):
    monkeypatch.delenv("PAPER_ATR_BANDS", raising=False)
    out = pw._wallet_vol_pct("XYZ", "stock", bars=_bars(0.10))  # 10% daily range
    assert out["vol_pct"] > 0.02                                # wider than flat 2%
    assert out["source"].startswith("realized_range")


def test_wallet_vol_falls_back_to_base_for_quiet_names(monkeypatch):
    monkeypatch.delenv("PAPER_ATR_BANDS", raising=False)
    out = pw._wallet_vol_pct("XYZ", "stock", bars=_bars(0.005))  # 0.5% range
    assert out["vol_pct"] == 0.02                                # base floor holds
    assert out["source"] == "base"


def test_wallet_vol_kill_switch(monkeypatch):
    monkeypatch.setenv("PAPER_ATR_BANDS", "off")
    out = pw._wallet_vol_pct("XYZ", "stock", bars=_bars(0.10))
    assert out["vol_pct"] == 0.02 and out["source"] == "base"


def test_fresh_bands_reward_risk_preserved_with_atr(monkeypatch):
    # Wider vol widens target AND stop together; reward:risk is untouched.
    monkeypatch.delenv("PAPER_ATR_BANDS", raising=False)
    monkeypatch.delenv("PAPER_WALLET_STOP_VOL_MULT", raising=False)
    tgt, stp, _ = pw.fresh_bands("XYZ", 100.0, bars=_bars(0.10))
    t_pct = tgt / 100.0 - 1
    s_pct = 1 - stp / 100.0
    assert t_pct > 0.02                                  # wider than flat
    assert abs(s_pct / t_pct - 0.65) < 0.01              # mult preserved


# ---------------------------------------------------------------- no dup/symbol
# One open lot per (book, symbol). ``source UNIQUE`` only stops re-mirroring the
# SAME signal row; distinct shadow rows for one symbol each carry their own
# source and used to stack correlated lots (ARDT/LCID/YMM x3 live 2026-07-13).

def test_filter_skips_symbol_already_open_same_book():
    # A symbol already held in the shadow book gets no second shadow lot.
    kept, skipped = pw._filter_new_symbol_candidates(
        [("shadow", "shadow:2", "ARDT")],
        {("shadow", "ARDT")},
    )
    assert kept == []
    assert skipped == 1


def test_filter_collapses_same_symbol_candidates_within_one_cycle():
    # Three fresh ARDT candidates in ONE cycle collapse to the first (freshest).
    kept, skipped = pw._filter_new_symbol_candidates(
        [("shadow", "shadow:9", "ARDT"),
         ("shadow", "shadow:8", "ARDT"),
         ("shadow", "shadow:7", "ARDT")],
        set(),
    )
    assert kept == [("shadow", "shadow:9", "ARDT")]
    assert skipped == 2


def test_filter_is_case_insensitive():
    # _enter stores symbol.upper(); the guard must match regardless of case.
    kept, skipped = pw._filter_new_symbol_candidates(
        [("shadow", "shadow:5", "ardt")],
        {("shadow", "ARDT")},
    )
    assert kept == []
    assert skipped == 1


def test_filter_scope_is_per_book():
    # A gated lot must NOT suppress shadow research for the same symbol,
    # and vice-versa — the two books are independent.
    kept, skipped = pw._filter_new_symbol_candidates(
        [("shadow", "shadow:1", "ABCL")],
        {("gated", "ABCL")},
    )
    assert kept == [("shadow", "shadow:1", "ABCL")]
    assert skipped == 0


def test_filter_keeps_distinct_symbols_in_order():
    # Distinct new symbols all pass, input order preserved.
    cands = [("shadow", "shadow:1", "ABCL"),
             ("shadow", "shadow:2", "BB"),
             ("gated", "pick:3", "XPO")]
    kept, skipped = pw._filter_new_symbol_candidates(cands, set())
    assert kept == cands
    assert skipped == 0


def test_run_cycle_does_not_open_duplicate_symbol(monkeypatch):
    """Integration: two shadow candidates for ARDT (one already open, one fresh
    dup in-cycle) yield exactly ZERO new ARDT lots; a distinct symbol still
    enters. Proves the guard holds through the real run_wallet_cycle path."""
    monkeypatch.setenv("PAPER_WALLET_ENABLED", "1")
    monkeypatch.setenv("PAPER_SESSION_GATE", "0")   # entry window always open
    inserts = []

    class _Cur:
        def __init__(self):
            self._last = ""
            self.rowcount = 0

        def execute(self, sql, params=None):
            self._last = sql
            if "INSERT INTO ghost_paper_trades" in sql and params is not None:
                # params: (book, symbol, qty, entry, now, tgt, stop, exp, source, now)
                inserts.append({"book": params[0], "symbol": params[1],
                                "source": params[8]})
                self.rowcount = 1                # a real INSERT affected one row
            else:
                self.rowcount = 0

        def fetchone(self):
            s = self._last
            if "COUNT(*) FROM ghost_paper_trades" in s:
                return (1,)                      # 1 already open (the held ARDT)
            return None

        def fetchall(self):
            s = self._last
            if "FROM ghost_shadow_outcomes o" in s:
                # id, symbol, entry, target, stop, expires, resolved, wins
                return [
                    (101, "ARDT", 10.0, 10.5, 9.7, 9_999_999_999, 20, 15),
                    (102, "ARDT", 10.1, 10.6, 9.8, 9_999_999_999, 20, 15),
                    (103, "BB", 8.0, 8.4, 7.7, 9_999_999_999, 20, 15),
                ]
            if "FROM predictions" in s:
                return []                        # gated book silent
            if "SELECT DISTINCT book, symbol FROM ghost_paper_trades" in s:
                return [("shadow", "ARDT")]      # ARDT already open
            if "SELECT source FROM ghost_paper_trades" in s:
                return []                        # no source collision
            if "SELECT id, symbol, qty" in s:
                return []                        # exits: nothing to close
            if "GROUP BY symbol" in s:
                return []                        # daily snapshot: no open rows
            return []

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import core.db as db
    monkeypatch.setattr(db, "db_conn", lambda: _Conn())
    monkeypatch.setattr(pw, "_cash", lambda cur, cfg: 1_000_000.0)
    monkeypatch.setattr(pw, "_max_open", lambda: 50)
    monkeypatch.setattr(pw, "get_config", lambda cur: {"starting_balance": 10000.0})
    monkeypatch.setattr(pw, "_maybe_roll_month", lambda cur, cfg: cfg)
    monkeypatch.setattr(pw, "_live_prices", lambda syms: {s.upper(): 10.0 for s in syms})
    monkeypatch.setattr(pw, "fresh_bands",
                        lambda sym, entry, **k: (entry * 1.02, entry * 0.987, 9_999_999_999))

    out = pw.run_wallet_cycle()
    assert out["ok"] is True
    entered_symbols = [i["symbol"] for i in inserts]
    assert "ARDT" not in entered_symbols          # already open -> never re-entered
    assert entered_symbols.count("BB") == 1       # distinct symbol still enters
    assert out["diag"]["skip_open_symbol"] >= 2   # 1 already-open + 1 in-cycle dup


# ------------------------------------------------------- duplicate cleanup ops
# PR #133 prevents NEW duplicate open lots; this admin repair closes pre-existing
# duplicate paper positions with audit reason, dry-run first.

def test_cleanup_duplicate_positions_dry_run_keeps_oldest(monkeypatch):
    rows = [
        # id, book, symbol, qty, entry_price, entry_ts, source
        (10, "shadow", "ARDT", 10.0, 10.00, 100, "shadow:10"),
        (11, "shadow", "ardt", 10.0, 10.10, 200, "shadow:11"),
        (12, "shadow", "ARDT", 10.0, 10.20, 300, "shadow:12"),
        (20, "gated", "ARDT", 10.0, 10.00, 100, "pick:20"),  # per-book independent
        (30, "shadow", "BB", 5.0, 8.00, 100, "shadow:30"),   # no duplicate
    ]
    updates = []
    committed = {"n": 0}

    class _Cur:
        rowcount = 0
        def __init__(self): self._last = ""
        def execute(self, sql, params=None):
            self._last = sql
            if "UPDATE ghost_paper_trades" in sql:
                updates.append(params)
                self.rowcount = 1
            else:
                self.rowcount = 0
        def fetchall(self):
            if "SELECT id, book, symbol" in self._last:
                return rows
            return []

    class _Conn:
        def cursor(self): return _Cur()
        def commit(self): committed["n"] += 1
        def __enter__(self): return self
        def __exit__(self, *a): return False

    import core.db as db
    monkeypatch.setattr(db, "db_conn", lambda: _Conn())
    monkeypatch.setattr(pw, "_live_prices", lambda syms: {"ARDT": 10.50})

    out = pw.cleanup_duplicate_open_positions(dry_run=True)
    assert out["ok"] is True and out["dry_run"] is True
    assert out["duplicate_groups"] == [{"book": "shadow", "symbol": "ARDT", "open_lots": 3, "keep_id": 10}]
    assert [i["close_id"] for i in out["to_close"]] == [11, 12]
    assert out["planned_close_count"] == 2
    assert out["closed_count"] == 0
    assert updates == []
    assert committed["n"] == 0


def test_cleanup_duplicate_positions_apply_closes_at_quote(monkeypatch):
    rows = [
        (10, "shadow", "XPO", 2.0, 100.00, 100, "shadow:10"),
        (11, "shadow", "XPO", 2.0, 101.00, 200, "shadow:11"),
    ]
    updates = []
    committed = {"n": 0}

    class _Cur:
        rowcount = 0
        def __init__(self): self._last = ""
        def execute(self, sql, params=None):
            self._last = sql
            if "UPDATE ghost_paper_trades" in sql:
                updates.append(params)
                self.rowcount = 1
            else:
                self.rowcount = 0
        def fetchall(self):
            return rows if "SELECT id, book, symbol" in self._last else []

    class _Conn:
        def cursor(self): return _Cur()
        def commit(self): committed["n"] += 1
        def __enter__(self): return self
        def __exit__(self, *a): return False

    import core.db as db
    monkeypatch.setattr(db, "db_conn", lambda: _Conn())
    monkeypatch.setattr(pw, "_live_prices", lambda syms: {"XPO": 103.0})
    monkeypatch.setattr(pw.time, "time", lambda: 1234567890)

    out = pw.cleanup_duplicate_open_positions(dry_run=False)
    assert out["closed_count"] == 1
    assert committed["n"] == 1
    assert len(updates) == 1
    exit_price, exit_ts, reason, pnl, pnl_pct, close_id = updates[0]
    assert (exit_price, exit_ts, reason, close_id) == (103.0, 1234567890, "duplicate_symbol_cleanup", 11)
    assert pnl == 4.0
    assert pnl_pct == 1.98


def test_cleanup_duplicate_positions_skips_when_no_live_price(monkeypatch):
    rows = [
        (10, "shadow", "LCID", 1.0, 5.00, 100, "shadow:10"),
        (11, "shadow", "LCID", 1.0, 5.10, 200, "shadow:11"),
    ]

    class _Cur:
        rowcount = 0
        def __init__(self): self._last = ""
        def execute(self, sql, params=None): self._last = sql
        def fetchall(self): return rows if "SELECT id, book, symbol" in self._last else []
    class _Conn:
        def cursor(self): return _Cur()
        def commit(self): raise AssertionError("no commit when nothing closes")
        def __enter__(self): return self
        def __exit__(self, *a): return False

    import core.db as db
    monkeypatch.setattr(db, "db_conn", lambda: _Conn())
    monkeypatch.setattr(pw, "_live_prices", lambda syms: {})

    out = pw.cleanup_duplicate_open_positions(dry_run=False)
    assert out["planned_close_count"] == 0
    assert out["closed_count"] == 0
    assert out["skipped_count"] == 1
    assert out["skipped"][0]["skip_reason"] == "no_live_price"


def test_cleanup_duplicate_positions_rejects_unsupported_keep_policy():
    out = pw.cleanup_duplicate_open_positions(dry_run=True, keep="best")
    assert out["ok"] is False
    assert out["error"] == "unsupported_keep_policy"


def test_cleanup_duplicate_route_is_gated_and_dry_run_default(monkeypatch):
    from fastapi.testclient import TestClient
    from wolf_app import APP

    calls = []
    monkeypatch.setattr("wolf_app._cron_ok", lambda secret: secret == "ok")
    monkeypatch.setattr("wolf_app._admin_token_valid", lambda tok: False)
    monkeypatch.setattr(
        "core.paper_wallet.cleanup_duplicate_open_positions",
        lambda dry_run=True, keep="oldest": calls.append((dry_run, keep)) or {
            "ok": True, "dry_run": dry_run, "keep": keep, "planned_close_count": 0,
        },
    )
    c = TestClient(APP)
    assert c.post("/api/wallet/cleanup-duplicates").status_code == 403
    r = c.post("/api/wallet/cleanup-duplicates", headers={"x-cron-secret": "ok"})
    assert r.status_code == 200
    assert r.json()["dry_run"] is True
    assert calls[-1] == (True, "oldest")
    r2 = c.post("/api/wallet/cleanup-duplicates?dry_run=0", headers={"x-cron-secret": "ok"})
    assert r2.json()["dry_run"] is False
    assert calls[-1] == (False, "oldest")
