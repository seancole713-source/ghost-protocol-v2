"""Audit F08: paper-wallet missing/zero/NaN/stale quotes are never fabricated
into unchanged prices. Cost basis stays separate from market value and totals
report valuation coverage."""
import math

import core.paper_wallet as pw


def _row(tid, sym, qty, entry, book="shadow"):
    # (id, book, symbol, qty, entry_price, entry_ts, target, stop, expires_at)
    return (tid, book, sym, qty, entry, 1_700_000_000, entry * 1.02, entry * 0.987, 1_800_000_000)


def _q(price, **kw):
    base = {"price": price, "source": "iex", "age_seconds": 5,
            "quote_status": "fresh", "stale": False, "missing_reason": None}
    base.update(kw)
    return base


def test_missing_quote_is_not_valued_at_entry():
    out = pw.wallet_valuation([_row(1, "ABC", 10, 100.0)], {}, cash=9000.0)
    p = out["positions"][0]
    assert p["current_price"] is None
    assert p["value"] is None and p["pnl"] is None and p["pnl_pct"] is None
    assert p["quote_missing"] is True
    assert p["quote_missing_reason"] == "no_quote"
    assert p["cost_basis"] == 1000.0           # cost kept separate
    assert out["invested"] == 1000.0
    assert out["market_value"] is None
    assert out["equity"] is None               # not 9000 + 1000 fabricated
    assert out["totals_partial"] is True
    assert out["quote_coverage"] == {"priced": 0, "total": 1, "stale": 0,
                                     "missing": 1, "ratio": 0.0}
    assert out["priced_subset"]["value"] is None


def test_zero_quote_is_missing():
    out = pw.wallet_valuation([_row(1, "ABC", 10, 100.0)],
                              {"ABC": _q(0.0)}, cash=9000.0)
    p = out["positions"][0]
    assert p["current_price"] is None and p["value"] is None
    assert p["quote_missing"] is True
    assert out["equity"] is None


def test_nan_quote_is_missing():
    out = pw.wallet_valuation([_row(1, "ABC", 10, 100.0)],
                              {"ABC": _q(float("nan"))}, cash=9000.0)
    p = out["positions"][0]
    assert p["current_price"] is None and p["value"] is None
    assert out["totals_partial"] is True
    assert out["equity"] is None


def test_stale_quote_values_but_is_flagged():
    out = pw.wallet_valuation(
        [_row(1, "ABC", 10, 100.0)],
        {"ABC": _q(105.0, stale=True, quote_status="stale", age_seconds=3600)},
        cash=9000.0)
    p = out["positions"][0]
    assert p["current_price"] == 105.0 and p["value"] == 1050.0 and p["pnl"] == 50.0
    assert p["quote_missing"] is False
    assert p["quote_stale"] is True
    assert p["quote_status"] == "stale"
    assert p["quote_source"] == "iex" and p["quote_age_seconds"] == 3600
    assert out["quote_coverage"]["stale"] == 1
    assert out["totals_partial"] is False
    assert out["equity"] == 10050.0


def test_partial_coverage_nulls_totals_and_reports_priced_subset():
    rows = [_row(1, "ABC", 10, 100.0), _row(2, "XYZ", 5, 20.0)]
    out = pw.wallet_valuation(rows, {"XYZ": _q(22.0)}, cash=8900.0)
    assert out["totals_partial"] is True
    assert out["equity"] is None and out["market_value"] is None
    assert out["invested"] == 1100.0
    assert out["quote_coverage"]["priced"] == 1
    assert out["quote_coverage"]["total"] == 2
    assert out["quote_coverage"]["ratio"] == 0.5
    assert out["priced_subset"] == {"label": "priced positions only",
                                    "cost": 100.0, "value": 110.0, "pnl": 10.0}


def test_full_coverage_and_empty_book():
    out = pw.wallet_valuation([_row(1, "ABC", 10, 100.0)], {"ABC": _q(101.0)}, cash=9000.0)
    assert out["totals_partial"] is False
    assert out["equity"] == 10010.0 and out["market_value"] == 1010.0
    empty = pw.wallet_valuation([], {}, cash=10000.0)
    assert empty["totals_partial"] is False
    assert empty["equity"] == 10000.0
    assert empty["quote_coverage"]["ratio"] is None


def test_live_quotes_rejects_zero_nan_future_and_flags_stale(monkeypatch):
    import core.market_sessions as ms
    import core.prices as prices

    def _sessions(symbols, max_fresh=None):
        return {"ok": True, "sessions": {
            "ZERO": {"price": 0, "quote_status": "fresh", "price_source": "iex"},
            "NANQ": {"price": float("nan"), "quote_status": "fresh"},
            "FUT": {"price": 10.0, "quote_status": "future_timestamp"},
            "OLD": {"price": 12.0, "quote_status": "stale", "freshness_seconds": 5400,
                    "price_source": "sip"},
            "REF": {"price": 9.0, "quote_status": "reference_only",
                    "price_source": "rth_close_fallback", "freshness_seconds": None},
            "GOOD": {"price": 11.0, "quote_status": "fresh", "freshness_seconds": 3,
                     "price_source": "iex"},
        }}

    spot = {"ZERO": 0.0, "NANQ": float("nan"), "FUT": None, "NONE": 7.5}
    monkeypatch.setattr(ms, "get_market_sessions", _sessions)
    monkeypatch.setattr(prices, "get_price", lambda s, *a, **k: spot.get(s))

    q = pw._live_quotes(["zero", "NANQ", "FUT", "OLD", "REF", "GOOD", "NONE"])
    assert q["ZERO"]["price"] is None and q["ZERO"]["missing_reason"] == "non_positive"
    assert q["NANQ"]["price"] is None and q["NANQ"]["missing_reason"] == "non_finite"
    assert q["FUT"]["price"] is None and q["FUT"]["missing_reason"] == "future_timestamp"
    assert q["OLD"]["price"] == 12.0 and q["OLD"]["stale"] is True
    assert q["OLD"]["age_seconds"] == 5400 and q["OLD"]["source"] == "sip"
    assert q["REF"]["stale"] is True
    assert q["GOOD"]["price"] == 11.0 and q["GOOD"]["stale"] is False
    assert q["NONE"]["price"] == 7.5 and q["NONE"]["source"] == "spot_fallback"

    prices_only = pw._live_prices(["ZERO", "NANQ", "GOOD"])
    assert prices_only == {"GOOD": 11.0}
    assert all(math.isfinite(v) and v > 0 for v in prices_only.values())


def test_wallet_summary_missing_quote_end_to_end(monkeypatch):
    """The audit repro: 10 shares bought at $100, no quote -> no fabricated
    current_price=100 / value=1000 / pnl=0."""
    open_row = _row(7, "ABC", 10.0, 100.0)

    class _Cur:
        def __init__(self): self._last = ""
        def execute(self, sql, params=None): self._last = sql
        def fetchone(self):
            s = self._last
            if "COUNT(*)" in s:
                return (0, 0.0, 0)
            if "SUM(qty*entry_price)" in s:
                return (1000.0,)
            if "SUM(" in s:
                return (0.0,)
            return None
        def fetchall(self):
            if "status='open'" in self._last and "entry_ts" in self._last:
                return [open_row]
            return []

    class _Conn:
        def cursor(self): return _Cur()
        def commit(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    import core.db as db
    monkeypatch.setattr(db, "db_conn", lambda: _Conn())
    monkeypatch.setattr(db, "ensure_ghost_state", lambda cur: None)
    monkeypatch.setattr(pw, "_live_quotes", lambda syms: {
        s: {"price": None, "source": None, "age_seconds": None,
            "quote_status": "missing", "stale": False, "missing_reason": "no_quote"}
        for s in syms})
    out = pw.wallet_summary()
    assert out["ok"] is True
    p = out["open_positions"][0]
    assert p["current_price"] is None and p["value"] is None and p["pnl"] is None
    assert p["quote_missing"] is True
    assert out["invested"] == 1000.0
    assert out["cash"] == 9000.0
    assert out["market_value"] is None
    assert out["total_value"] is None
    assert out["total_pnl"] is None and out["total_pnl_pct"] is None
    assert out["totals_partial"] is True
    assert out["quote_coverage"]["priced"] == 0 and out["quote_coverage"]["total"] == 1
    g = out["goal"]
    assert g["valuation_partial"] is True
    assert g["pct_of_goal"] is None and g["progress_pct"] is None
    assert g["reached"] is None and g["need_per_day"] is None
