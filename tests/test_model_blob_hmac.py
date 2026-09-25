"""F05: model blobs must be HMAC-verified (key outside the DB) before unpickle."""
import base64
import hashlib
import json
import logging
import pickle

import pytest

import core.model_blob_integrity as mbi
import core.signal_engine as _se
from tests.calibration_fixtures import serveable_meta

KEY_ENV = mbi.HMAC_KEY_ENV
ALLOW_ENV = mbi.ALLOW_UNSIGNED_ENV

_UNPICKLED = []


def _record_unpickle(tag):
    _UNPICKLED.append(tag)
    return {"evil": tag}


class _Evil:
    """Unpickling this object calls _record_unpickle: proves pickle ran."""

    def __reduce__(self):
        return (_record_unpickle, ("ran",))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    mbi._reset_warning_state_for_tests()
    _UNPICKLED.clear()
    yield
    mbi._reset_warning_state_for_tests()


# ── helper module ───────────────────────────────────────────────────────────

def test_sign_returns_none_without_key_and_warns_once(caplog):
    caplog.set_level(logging.WARNING, logger="ghost.model_blob_integrity")
    assert mbi.sign_model_blob(b"abc") is None
    assert mbi.verify_model_blob(b"abc", None) is None
    assert mbi.verify_model_blob(b"abc", "") is None
    warnings = [r for r in caplog.records if KEY_ENV in r.getMessage()]
    assert len(warnings) == 1


def test_key_unset_keeps_legacy_load():
    raw = pickle.dumps({"m": 1})
    assert mbi.load_verified_pickle(raw, None) == {"m": 1}


def test_valid_signature_loads(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "unit-test-key")
    raw = pickle.dumps({"m": 2})
    sig = mbi.sign_model_blob(raw)
    assert sig and len(sig) == 64
    assert mbi.load_verified_pickle(raw, sig) == {"m": 2}


def test_bad_signature_refused_before_unpickle(monkeypatch, caplog):
    monkeypatch.setenv(KEY_ENV, "unit-test-key")
    raw = pickle.dumps(_Evil())
    caplog.set_level(logging.ERROR, logger="ghost.model_blob_integrity")
    with pytest.raises(mbi.ModelBlobIntegrityError) as exc:
        mbi.load_verified_pickle(raw, "0" * 64, context="t")
    assert exc.value.reason == "model_hmac_mismatch"
    assert _UNPICKLED == []
    assert any("HMAC mismatch" in r.getMessage() for r in caplog.records)
    # the key value itself is never logged
    assert all("unit-test-key" not in r.getMessage() for r in caplog.records)


def test_signature_from_other_key_refused(monkeypatch):
    raw = pickle.dumps(_Evil())
    monkeypatch.setenv(KEY_ENV, "attacker-key")
    forged = mbi.sign_model_blob(raw)
    monkeypatch.setenv(KEY_ENV, "real-key")
    with pytest.raises(mbi.ModelBlobIntegrityError):
        mbi.load_verified_pickle(raw, forged)
    assert _UNPICKLED == []


def test_plain_sha256_is_not_a_valid_signature(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "real-key")
    raw = pickle.dumps(_Evil())
    with pytest.raises(mbi.ModelBlobIntegrityError):
        mbi.load_verified_pickle(raw, hashlib.sha256(raw).hexdigest())
    assert _UNPICKLED == []


def test_unsigned_refused_by_default_when_key_set(monkeypatch, caplog):
    monkeypatch.setenv(KEY_ENV, "real-key")
    raw = pickle.dumps(_Evil())
    caplog.set_level(logging.ERROR, logger="ghost.model_blob_integrity")
    with pytest.raises(mbi.ModelBlobIntegrityError) as exc:
        mbi.load_verified_pickle(raw, None, context="legacy")
    assert exc.value.reason == "model_hmac_missing"
    assert _UNPICKLED == []
    assert any("REFUSED unsigned" in r.getMessage() for r in caplog.records)


def test_unsigned_allowed_only_with_explicit_flag(monkeypatch, caplog):
    monkeypatch.setenv(KEY_ENV, "real-key")
    monkeypatch.setenv(ALLOW_ENV, "1")
    raw = pickle.dumps({"legacy": True})
    caplog.set_level(logging.WARNING, logger="ghost.model_blob_integrity")
    assert mbi.load_verified_pickle(raw, "", context="legacy") == {"legacy": True}
    assert any("LOADING UNSIGNED" in r.getMessage() for r in caplog.records)


def test_allow_unsigned_flag_does_not_excuse_bad_signature(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "real-key")
    monkeypatch.setenv(ALLOW_ENV, "1")
    raw = pickle.dumps(_Evil())
    with pytest.raises(mbi.ModelBlobIntegrityError):
        mbi.load_verified_pickle(raw, "ab" * 32)
    assert _UNPICKLED == []


# ── signal_engine.load_model ────────────────────────────────────────────────

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


def _rows(raw, **meta_extra):
    meta = serveable_meta(
        feature_cols=_se.FEATURE_COLS,
        model_sha256=hashlib.sha256(raw).hexdigest(),
        **meta_extra,
    )
    return {
        "meta_WOLF": json.dumps(meta),
        "model_WOLF": base64.b64encode(raw).decode("ascii"),
    }


def _load(monkeypatch, rows):
    import core.db as _db
    monkeypatch.setattr(_db, "db_conn", lambda: _DbCtx(rows))
    monkeypatch.setattr(_se, "_model_cache_ttl_s", lambda: 0, raising=False)
    return _se._load_model_uncached("WOLF", "UP")


def test_load_model_refuses_db_supplied_hash_without_hmac(monkeypatch):
    """An attacker with DB write access supplies payload AND its sha256."""
    monkeypatch.setenv(KEY_ENV, "real-key")
    raw = pickle.dumps(_Evil())
    assert _load(monkeypatch, _rows(raw)) == (None, None, None)
    assert _UNPICKLED == []


def test_load_model_refuses_bad_hmac(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "real-key")
    raw = pickle.dumps(_Evil())
    assert _load(monkeypatch, _rows(raw, model_hmac="0" * 64)) == (None, None, None)
    assert _UNPICKLED == []


def test_load_model_accepts_signed_blob(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "real-key")
    raw = pickle.dumps({"model": "ok"})
    sig = mbi.sign_model_blob(raw)
    model, cols, meta = _load(monkeypatch, _rows(raw, model_hmac=sig))
    assert model == {"model": "ok"}
    assert cols == _se.FEATURE_COLS
    assert meta["model_hmac"] == sig


def test_load_model_unsigned_migration_flag(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "real-key")
    monkeypatch.setenv(ALLOW_ENV, "1")
    raw = pickle.dumps({"model": "legacy"})
    model, _cols, _meta = _load(monkeypatch, _rows(raw))
    assert model == {"model": "legacy"}


def test_load_model_without_key_keeps_legacy_behaviour(monkeypatch):
    raw = pickle.dumps({"model": "legacy"})
    model, _cols, _meta = _load(monkeypatch, _rows(raw))
    assert model == {"model": "legacy"}


# ── research artifacts / activation ─────────────────────────────────────────

def test_validated_live_model_refuses_unsigned_when_key_set(monkeypatch):
    from core import research_activation as ra
    monkeypatch.setenv(KEY_ENV, "real-key")
    raw = pickle.dumps(_Evil())
    monkeypatch.setattr(_se, "model_serve_guard", lambda *a, **k: None)
    meta = {"model_sha256": hashlib.sha256(raw).hexdigest(), "direction": "UP"}
    payload = base64.b64encode(raw).decode("ascii")
    assert ra._validated_live_model(payload, meta, "UP") == (None, "model_hmac_missing")
    meta["model_hmac"] = "1" * 64
    assert ra._validated_live_model(payload, meta, "UP") == (None, "model_hmac_mismatch")
    assert _UNPICKLED == []


def test_validated_live_model_accepts_signed(monkeypatch):
    from core import research_activation as ra
    monkeypatch.setenv(KEY_ENV, "real-key")
    raw = pickle.dumps({"ok": 1})
    monkeypatch.setattr(_se, "model_serve_guard", lambda *a, **k: None)
    meta = {
        "model_sha256": hashlib.sha256(raw).hexdigest(),
        "direction": "UP",
        "model_hmac": mbi.sign_model_blob(raw),
    }
    payload = base64.b64encode(raw).decode("ascii")
    got, err = ra._validated_live_model(payload, meta, "UP")
    assert err is None and got is meta


class _RecCur:
    def __init__(self):
        self.calls = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.calls.append((sql, params))


def _artifact_meta(raw):
    from core.research_artifacts import ArtifactMeta, compute_artifact_sha
    kw = dict(
        contract_id="c1", policy_lineage_id="WOLF/UP", policy_lineage_version=1,
        feature_order=("a", "b"), feature_schema="fs", label_schema="ls",
        validation_schema="vs", hold_bars=3, training_manifest_sha="m",
        symbol_scope=("WOLF",), trained_at=1000,
    )
    sha = compute_artifact_sha(
        model_sha256=hashlib.sha256(raw).hexdigest(), direction="UP", **kw,
    )
    return ArtifactMeta(
        artifact_sha=sha, contract_id="c1", policy_lineage_id="WOLF/UP",
        policy_lineage_version=1, symbol_scope=("WOLF",), output_domain=("UP",),
        feature_schema="fs", evidence_schema="ls", validation_schema="vs",
        horizon_bars=3, training_manifest_sha="m", calibration_proof={},
        gate_proof={}, feature_order=("a", "b"), trained_at=1000,
    )


def test_register_artifact_stores_hmac(monkeypatch):
    from core.research_artifacts import _register_artifact_impl
    monkeypatch.setenv(KEY_ENV, "real-key")
    raw = pickle.dumps({"ok": 1})
    cur = _RecCur()
    assert _register_artifact_impl(cur, _artifact_meta(raw), base64.b64encode(raw).decode())
    sql, params = cur.calls[0]
    assert "model_hmac" in sql
    assert params[-1] == mbi.sign_model_blob(raw)


def test_register_artifact_without_key_stores_empty_hmac():
    from core.research_artifacts import _register_artifact_impl
    raw = pickle.dumps({"ok": 1})
    cur = _RecCur()
    assert _register_artifact_impl(cur, _artifact_meta(raw), base64.b64encode(raw).decode())
    assert cur.calls[0][1][-1] == ""
