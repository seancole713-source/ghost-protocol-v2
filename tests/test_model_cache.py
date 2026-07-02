"""Model cache — load_model caches per (symbol, direction), writers invalidate."""
import base64
import hashlib
import json
import pickle
import time

import core.signal_engine as _se


def _make_rows():
    """Valid model + meta rows for WOLF UP (passes every load_model guard)."""
    payload = pickle.dumps({"stub": "model"})
    meta = {
        "label_type": _se.LABEL_TYPE,
        "label_schema": _se._v3_label_schema(),
        "feature_schema": _se._v3_feature_schema(),
        "trained_at": int(time.time()),
        "feature_cols": ["a", "b"],
        "model_sha256": hashlib.sha256(payload).hexdigest(),
    }
    return {
        "meta_WOLF_up": json.dumps(meta),
        "model_WOLF_up": base64.b64encode(payload).decode(),
    }


class _CountingDb:
    def __init__(self, rows):
        self.rows = rows
        self.selects = 0

    def make_ctx(self):
        db = self

        class _Cur:
            def execute(self, sql, params=None):
                db.selects += 1
                self._key = params[0] if params else None

            def fetchone(self):
                val = db.rows.get(self._key)
                return (val,) if val is not None else None

        class _Conn:
            def cursor(self):
                return _Cur()

        class _Ctx:
            def __enter__(self):
                return _Conn()

            def __exit__(self, *a):
                return False

        return _Ctx()


def _patch_db(monkeypatch, db):
    import core.db as _db
    monkeypatch.setattr(_db, "db_conn", lambda: db.make_ctx())


def test_second_load_hits_cache(monkeypatch):
    monkeypatch.setenv("V3_MODEL_CACHE_TTL_S", "3600")
    db = _CountingDb(_make_rows())
    _patch_db(monkeypatch, db)

    _se.invalidate_model_cache()
    m1, cols1, meta1 = _se.load_model("WOLF", "UP")
    assert m1 is not None and cols1 == ["a", "b"]
    n_after_first = db.selects
    assert n_after_first > 0

    m2, cols2, _ = _se.load_model("WOLF", "UP")
    assert m2 is not None and cols2 == ["a", "b"]
    assert db.selects == n_after_first  # no new DB round-trips


def test_invalidate_forces_reload(monkeypatch):
    monkeypatch.setenv("V3_MODEL_CACHE_TTL_S", "3600")
    db = _CountingDb(_make_rows())
    _patch_db(monkeypatch, db)

    _se.invalidate_model_cache()
    _se.load_model("WOLF", "UP")
    n = db.selects
    _se.invalidate_model_cache("WOLF")
    _se.load_model("WOLF", "UP")
    assert db.selects > n  # reload went back to the DB


def test_negative_lookup_is_cached(monkeypatch):
    monkeypatch.setenv("V3_MODEL_CACHE_TTL_S", "3600")
    db = _CountingDb({})  # no rows at all
    _patch_db(monkeypatch, db)

    _se.invalidate_model_cache()
    assert _se.load_model("WOLF", "DOWN") == (None, None, None)
    n = db.selects
    assert _se.load_model("WOLF", "DOWN") == (None, None, None)
    assert db.selects == n


def test_ttl_zero_disables_cache(monkeypatch):
    monkeypatch.setenv("V3_MODEL_CACHE_TTL_S", "0")
    db = _CountingDb(_make_rows())
    _patch_db(monkeypatch, db)

    _se.invalidate_model_cache()
    _se.load_model("WOLF", "UP")
    n = db.selects
    _se.load_model("WOLF", "UP")
    assert db.selects > n  # every call goes to the DB


def test_cached_model_still_respects_serve_guard(monkeypatch):
    """A cached model whose meta ages past the 14d window must stop serving."""
    monkeypatch.setenv("V3_MODEL_CACHE_TTL_S", "3600")
    db = _CountingDb(_make_rows())
    _patch_db(monkeypatch, db)

    _se.invalidate_model_cache()
    m1, _, meta1 = _se.load_model("WOLF", "UP")
    assert m1 is not None
    # Age the cached meta in place past the expiry window
    meta1["trained_at"] = int(time.time()) - 15 * 86400
    m2, _, _ = _se.load_model("WOLF", "UP")
    assert m2 is None
