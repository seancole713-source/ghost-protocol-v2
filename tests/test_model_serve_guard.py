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
