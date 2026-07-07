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


def test_target_fills_at_target_not_better():
    # Even if price spiked past the target, a resting limit books the limit.
    assert exit_fill(10.9, target=10.5, stop=9.5, expires_at=None, now=1) == (10.5, "target")


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
    # Ghost stock default geometry: +2% target; stop = 2% * stop_mult.
    monkeypatch.setenv("V3_STOP_VOL_MULT", "0.65")
    tgt, stp, exp = fresh_bands("NVDA", 100.0, now=1_000_000)
    assert tgt > 100.0 and stp < 100.0          # brackets the entry
    assert abs(tgt - 102.0) < 0.01              # +2.0%
    assert abs(stp - 98.7) < 0.01               # -1.3% (2% * 0.65)
    assert exp > 1_000_000                       # future expiry


def test_fresh_bands_never_precrossed(monkeypatch):
    # The whole point of Option B: a fresh entry can never be already-resolved.
    from core.paper_wallet import fresh_bands, exit_fill
    for mult in ("0.65", "1.8"):
        monkeypatch.setenv("V3_STOP_VOL_MULT", mult)
        entry = 36.46
        tgt, stp, exp = fresh_bands("WOLF", entry, now=1_000_000)
        assert exit_fill(entry, tgt, stp, exp, 1_000_000) is None  # not pre-crossed
