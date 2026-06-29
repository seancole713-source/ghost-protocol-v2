"""Phase 5: training label simulation must match live TP/SL reconcile path."""

from core import tp_sl_resolve as tps
from core.vol_targets import stop_pct_from_vol


def _daily_rows(dates, closes, highs=None, lows=None):
    rows = []
    for i, d in enumerate(dates):
        c = closes[i]
        h = highs[i] if highs else c * 1.01
        lo = lows[i] if lows else c * 0.99
        rows.append({"ts": f"{d}T00:00:00Z", "open": c, "high": h, "low": lo, "close": c, "volume": 1})
    return rows


def test_simulate_matches_reconcile_on_win_path():
    vol = 0.02
    entry = 100.0
    target = entry * (1 + vol)
    rows = _daily_rows(
        ["2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"],
        [entry, entry, entry, entry],
        highs=[entry, entry, target + 0.5, entry],
        lows=[entry, entry, entry, entry],
    )
    label = tps.simulate_tp_sl_label(rows, 0, hold_bars=3, vol_pct=vol)
    live = tps.reconcile_training_label(rows=rows, entry_idx=0, hold_bars=3, vol_pct=vol)
    assert label == "WIN"
    assert live == "WIN"


def test_simulate_matches_reconcile_on_loss_path():
    vol = 0.02
    entry = 100.0
    stop = entry * (1 - stop_pct_from_vol(vol))
    rows = _daily_rows(
        ["2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"],
        [entry, entry, entry, entry],
        highs=[entry, entry, entry, entry],
        lows=[entry, stop - 0.5, entry, entry],
    )
    label = tps.simulate_tp_sl_label(rows, 0, hold_bars=3, vol_pct=vol)
    live = tps.reconcile_training_label(rows=rows, entry_idx=0, hold_bars=3, vol_pct=vol)
    assert label == "LOSS"
    assert live == "LOSS"


def test_simulate_matches_reconcile_on_expired_path():
    vol = 0.02
    entry = 100.0
    rows = _daily_rows(
        ["2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05", "2026-06-06"],
        [entry, entry, entry, entry, entry],
        highs=[entry * 1.005] * 5,
        lows=[entry * 0.995] * 5,
    )
    label = tps.simulate_tp_sl_label(rows, 0, hold_bars=3, vol_pct=vol)
    live = tps.reconcile_training_label(rows=rows, entry_idx=0, hold_bars=3, vol_pct=vol)
    assert label == "EXPIRED"
    assert live == "EXPIRED"


def test_calendar_forward_differs_from_naive_index_slice():
    """Phase 5 guard: same-calendar-day bar after entry must not count in the window."""
    vol = 0.02
    entry = 100.0
    target = entry * (1 + vol)
    rows = [
        {"ts": "2026-06-02T21:00:00Z", "close": entry, "high": entry, "low": entry, "open": entry, "volume": 1},
        {"ts": "2026-06-02T22:00:00Z", "close": entry, "high": target + 1, "low": entry, "open": entry, "volume": 1},
        {"ts": "2026-06-03T00:00:00Z", "close": entry, "high": entry, "low": entry, "open": entry, "volume": 1},
        {"ts": "2026-06-04T00:00:00Z", "close": entry, "high": entry, "low": entry, "open": entry, "volume": 1},
        {"ts": "2026-06-05T00:00:00Z", "close": entry, "high": entry, "low": entry, "open": entry, "volume": 1},
    ]
    label = tps.simulate_tp_sl_label(rows, 0, hold_bars=3, vol_pct=vol)
    naive_fwd = rows[1:4]
    naive = tps.resolve_tp_sl_bar_path(naive_fwd, target, entry * (1 - stop_pct_from_vol(vol)), "UP", max_bars=3)
    assert naive == "WIN"
    assert label == "EXPIRED"


def test_signal_engine_wrapper_delegates_to_shared_path():
    import core.signal_engine as _se

    vol = 0.025
    entry = 50.0
    target = entry * (1 + vol)
    rows = _daily_rows(
        ["2026-06-10", "2026-06-11", "2026-06-12", "2026-06-13"],
        [entry, entry, entry, entry],
        highs=[entry, entry, target + 0.2, entry],
    )
    assert _se._simulate_up_tp_sl(rows, 0, 3, vol) == tps.simulate_tp_sl_label(rows, 0, 3, vol)


def test_load_model_rejects_stale_label_schema(monkeypatch):
    import json

    import core.db as _db
    import core.signal_engine as _se

    assert _se._v3_label_schema() == tps.LABEL_SCHEMA

    class _Cur:
        def __init__(self):
            self._key = None

        def execute(self, sql, params=None):
            self._key = params[0] if params else None

        def fetchone(self):
            if self._key == "meta_WOLF":
                return (json.dumps({
                    "label_type": _se.LABEL_TYPE,
                    "label_schema": "tp_sl_index_v0",
                    "feature_schema": _se._v3_feature_schema(),
                    "trained_at": 9999999999,
                }),)
            if self._key == "model_WOLF":
                # Invalid payload proves stale meta rejects before base64/pickle.
                return ("not-valid-base64",)
            return None

    class _Conn:
        def cursor(self):
            return _Cur()

    class _DbCtx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(_db, "db_conn", lambda: _DbCtx())
    model, cols, meta = _se.load_model("WOLF")
    assert model is None
    assert cols is None
    assert meta is None


def test_label_schema_constant():
    assert tps.LABEL_SCHEMA == "tp_sl_fwd_v1"
