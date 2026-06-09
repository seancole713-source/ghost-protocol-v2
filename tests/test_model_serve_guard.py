"""Serveability guards — DB rows vs load_model/loadable counts."""
import json
import time

import core.signal_engine as _se


def test_model_serve_guard_rejects_stale_feature_schema():
    meta = {
        "label_type": _se.LABEL_TYPE,
        "label_schema": _se._v3_label_schema(),
        "feature_schema": "macd_pct_v1+sec0",
        "trained_at": int(time.time()),
    }
    assert _se.model_serve_guard(meta) == "feature_schema_stale"


def test_model_serve_guard_accepts_current_schema():
    meta = {
        "label_type": _se.LABEL_TYPE,
        "label_schema": _se._v3_label_schema(),
        "feature_schema": _se._v3_feature_schema(),
        "trained_at": int(time.time()),
    }
    assert _se.model_serve_guard(meta) is None


def test_max_calibration_brier_phase5_floor(monkeypatch):
    monkeypatch.setenv("V3_MAX_CALIBRATION_BRIER", "0.24")
    assert _se._v3_max_calibration_brier() == 0.31


def test_sma5_from_daily_bars_uses_last_five_closes():
    rows = [{"close": float(i)} for i in (10, 11, 12, 13, 14, 15, 16)]
    assert _se._sma5_from_daily_bars(rows) == (14 + 15 + 16 + 13 + 12) / 5


def test_block_up_below_sma5_blocks_when_price_under_sma(monkeypatch):
    monkeypatch.setenv("V3_BLOCK_UP_BELOW_SMA5", "1")
    monkeypatch.setattr(
        _se,
        "_fetch_ohlcv",
        lambda symbol, asset_type, period=None, interval="1d": [
            {"close": 100.0},
            {"close": 100.0},
            {"close": 100.0},
            {"close": 100.0},
            {"close": 100.0},
        ],
    )
    blocked, sma, cur = _se._block_up_below_sma5("WOLF", "stock", 95.0)
    assert blocked is True
    assert sma == 100.0
    assert cur == 95.0


def test_block_up_below_sma5_can_disable(monkeypatch):
    monkeypatch.setenv("V3_BLOCK_UP_BELOW_SMA5", "0")
    blocked, _, _ = _se._block_up_below_sma5("WOLF", "stock", 1.0)
    assert blocked is False


def test_get_model_status_counts_serveable_only(monkeypatch):
    wolf_meta = {
        "label_type": _se.LABEL_TYPE,
        "label_schema": _se._v3_label_schema(),
        "feature_schema": _se._v3_feature_schema(),
        "trained_at": int(time.time()),
        "accuracy": 0.6,
        "edge": 0.1,
    }
    stale_meta = {
        "label_type": _se.LABEL_TYPE,
        "label_schema": _se._v3_label_schema(),
        "feature_schema": "macd_pct_v1+sec0",
        "trained_at": int(time.time()),
        "accuracy": 0.5,
    }
    rows = {
        "meta_WOLF": json.dumps(wolf_meta),
        "meta_STALE": json.dumps(stale_meta),
        "model_WOLF": "x",
    }

    class _Cur:
        def execute(self, sql, params=None):
            self._sql = sql

        def fetchall(self):
            if "meta_%" in self._sql and "LIKE 'model_%'" not in self._sql:
                return [(k, v) for k, v in rows.items() if k.startswith("meta_")]
            if "LIKE 'model_%'" in self._sql:
                return [(k,) for k in rows if k.startswith("model_")]
            return []

        def fetchone(self):
            return None

    class _Conn:
        def cursor(self):
            return _Cur()

    class _DbCtx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    import core.db as _db

    monkeypatch.setattr(_db, "db_conn", lambda: _DbCtx())
    monkeypatch.setattr(_se, "get_last_train_gate_summary", lambda: {})
    st = _se.get_model_status()
    assert st["models"] == 1
    assert st["models_stored"] == 2
    assert "WOLF" in st["symbols"]
    assert "STALE" in st["stored_symbols"]
    assert st["stored_symbols"]["STALE"]["serve_reject"] == "feature_schema_stale"
