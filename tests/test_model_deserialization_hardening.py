import base64
import hashlib
import json
import pickle
import time

import core.signal_engine as _se


class _DbCtx:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        rows = self.rows

        class _Cur:
            key = None

            def execute(self, sql, params=None):
                self.key = params[0] if params else None

            def fetchone(self):
                val = rows.get(self.key)
                return (val,) if val is not None else None

        class _Conn:
            def cursor(self):
                return _Cur()

        return _Conn()

    def __exit__(self, *a):
        return False


def _valid_meta(**extra):
    meta = {
        "feature_cols": _se.FEATURE_COLS,
        "label_type": _se.LABEL_TYPE,
        "label_schema": _se._v3_label_schema(),
        "feature_schema": _se._v3_feature_schema(),
        "trained_at": time.time(),
    }
    meta.update(extra)
    return meta


def test_load_model_rejects_hash_mismatch(monkeypatch):
    raw = pickle.dumps({"model": "ok"})
    encoded = base64.b64encode(raw).decode("ascii")
    rows = {
        "meta_WOLF": json.dumps(_valid_meta(model_sha256="0" * 64)),
        "model_WOLF": encoded,
    }
    import core.db as _db
    monkeypatch.setattr(_db, "db_conn", lambda: _DbCtx(rows))
    assert _se.load_model("WOLF") == (None, None, None)


def test_load_model_accepts_valid_hash(monkeypatch):
    obj = {"model": "ok"}
    raw = pickle.dumps(obj)
    encoded = base64.b64encode(raw).decode("ascii")
    rows = {
        "meta_WOLF": json.dumps(_valid_meta(model_sha256=hashlib.sha256(raw).hexdigest())),
        "model_WOLF": encoded,
    }
    import core.db as _db
    monkeypatch.setattr(_db, "db_conn", lambda: _DbCtx(rows))
    model, cols, meta = _se.load_model("WOLF")
    assert model == obj
    assert cols == _se.FEATURE_COLS
    assert meta["model_sha256"] == hashlib.sha256(raw).hexdigest()


def test_load_model_rejects_oversized_payload_before_pickle(monkeypatch):
    monkeypatch.setenv("V3_MODEL_MAX_BYTES", "1024")
    rows = {
        "meta_WOLF": json.dumps(_valid_meta()),
        "model_WOLF": base64.b64encode(b"x" * 2048).decode("ascii"),
    }
    import core.db as _db
    monkeypatch.setattr(_db, "db_conn", lambda: _DbCtx(rows))
    assert _se.load_model("WOLF") == (None, None, None)
