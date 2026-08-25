"""tests/test_research_tier.py — research-tier models (operator-approved 2026-07-16).

Gate-failing models may be stored ONLY as an up_prob source for shadow
evidence. Three invariants, each tested:
  1. they never overwrite a proven, still-serveable model;
  2. they never fire (hard block in _evaluate_lane, before any floor math);
  3. they never inflate the pass ratio (train state stays honest).
"""
from __future__ import annotations

import json
import os
import time

import core.signal_engine as se


# ── Storage policy (train_and_validate) ──────────────────────────────

def _run_train(monkeypatch, *, passed, research_allowed):
    """Drive train_and_validate with one symbol and a stubbed trainer."""
    stores = []
    monkeypatch.setattr(se, "backtest_symbol",
                        lambda s, a: ([{"features": {}, "label": 1}] * 25, []))
    monkeypatch.setattr(se, "_v3_pool_training_enabled", lambda: False)
    monkeypatch.setattr(se, "_persist_train_details", lambda d: None)
    monkeypatch.setattr(se, "clear_ohlcv_cache", lambda: None)
    monkeypatch.setattr(se, "invalidate_model_cache", lambda s: None)
    monkeypatch.setattr(se, "_v3_train_symbol_delay_sec", lambda: 0.0)
    meta = json.dumps({"tier": "proven" if passed else "research"})
    monkeypatch.setattr(
        se, "_train_one_direction",
        lambda rows, sym, d, cols, peers, used, pool: (passed, {"passed": passed}, "BYTES", meta))
    def store(sym, direction, model_bytes, meta_json):
        allowed = passed or research_allowed
        if allowed:
            stores.append((sym, direction, meta_json))
        return allowed
    monkeypatch.setattr(se, "_store_direction_model", store)
    result = se.train_and_validate([("TEST", "stock")])
    return result, stores


class TestStoragePolicy:
    def test_proven_model_always_stores_and_counts(self, monkeypatch):
        (m, ratio, ok), stores = _run_train(monkeypatch, passed=True,
                                            research_allowed=False)
        assert len(stores) == 1
        assert ok is True and ratio == 0.5   # 1 of 2 direction slots

    def test_research_model_stores_when_slot_free(self, monkeypatch):
        (m, ratio, ok), stores = _run_train(monkeypatch, passed=False,
                                            research_allowed=True)
        assert len(stores) == 1
        assert json.loads(stores[0][2])["tier"] == "research"
        # Pass ratio stays honest: research storage is NOT a pass.
        assert ratio == 0.0 and ok is False

    def test_research_model_refused_when_proven_model_present(self, monkeypatch):
        (m, ratio, ok), stores = _run_train(monkeypatch, passed=False,
                                            research_allowed=False)
        assert stores == []
        assert ratio == 0.0 and ok is False


class _TrainStubModel:
    def __init__(self, **kwargs): pass
    def fit(self, X, y, sample_weight=None): return self
    def predict(self, X):
        import numpy as np
        return np.zeros(len(X), dtype=int)
    def predict_proba(self, X):
        import numpy as np
        return np.tile([0.4, 0.6], (len(X), 1))


def test_train_one_direction_returns_false_with_research_artifact(monkeypatch):
    """A trained research artifact must not turn gate failure into success."""
    import sys
    import types

    fake_xgb = types.ModuleType("xgboost")
    fake_xgb.XGBClassifier = _TrainStubModel
    monkeypatch.setitem(sys.modules, "xgboost", fake_xgb)
    monkeypatch.setattr(se, "_min_train_rows", lambda: 999)  # deterministic gate failure
    monkeypatch.setattr(se, "_v3_feature_audit_enabled", lambda: False)
    monkeypatch.setattr(se, "_v3_ensemble_enabled", lambda: False)
    monkeypatch.setattr(se, "_maybe_calibrate", lambda model, X, y: (
        model, {"calibrated": False, "method": None, "ensemble": False},
    ))
    monkeypatch.setattr(se, "_evaluate_calibration_holdout", lambda model, X, y: {
        "holdout_acc": 0.9, "edge": 0.4, "natural_rate": 0.5,
        "no_skill_accuracy": 0.5, "gate_brier": 0.2,
        "reliability_bins": [], "gate_n": len(y),
    })
    monkeypatch.setattr(se, "_walk_forward_scores", lambda *args, **kwargs: {
        "fold_count": 5, "acc_mean": 0.9, "acc_min": 0.8,
        "edge_mean": 0.4, "edge_min": 0.3,
    })
    monkeypatch.setattr(se, "_v3_research_tier_enabled", lambda: True)
    rows = [
        {"features": {"a": float(i), "feature_asof_ts": 1_700_000_000 + i},
         "label": i % 2}
        for i in range(100)
    ]
    passed, detail, model_bytes, meta_json = se._train_one_direction(
        rows, "TEST", "UP", ["a"], [], [],
        {"enabled": False, "peer_sample_count": 0},
    )
    assert passed is False
    assert detail["passed"] is False
    assert model_bytes
    meta = json.loads(meta_json)
    assert meta["tier"] == "research"
    assert meta["gate_fail_reason"]


# ── Overwrite guard ──────────────────────────────────────────────────

class _Cur:
    def __init__(self, row):
        self._row = row
        self.executed = []
    def execute(self, sql, params=None): self.executed.append((sql, params))
    def fetchone(self): return self._row


class _Conn:
    def __init__(self, row):
        self._row = row
        self.cur = _Cur(row)
    def cursor(self): return self.cur
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _fresh_proven_meta():
    from tests.calibration_fixtures import serveable_meta
    return json.dumps(serveable_meta())


class TestOverwriteGuard:
    def _patch(self, monkeypatch, row):
        import core.db as db
        monkeypatch.setattr(db, "db_conn", lambda: _Conn(row))

    def test_empty_slot_allowed(self, monkeypatch):
        self._patch(monkeypatch, None)
        assert se._research_overwrite_allowed("X", "UP") is True

    def test_proven_serveable_model_protected(self, monkeypatch):
        self._patch(monkeypatch, (_fresh_proven_meta(),))
        assert se._research_overwrite_allowed("X", "UP") is False

    def test_research_model_replaceable(self, monkeypatch):
        meta = json.dumps({"tier": "research", "trained_at": time.time()})
        self._patch(monkeypatch, (meta,))
        assert se._research_overwrite_allowed("X", "UP") is True

    def test_expired_proven_model_replaceable(self, monkeypatch):
        meta = json.loads(_fresh_proven_meta())
        meta["trained_at"] = time.time() - 15 * 86400   # past 14-day expiry
        self._patch(monkeypatch, (json.dumps(meta),))
        assert se._research_overwrite_allowed("X", "UP") is True

    def test_db_error_fails_closed(self, monkeypatch):
        import core.db as db
        def boom():
            raise RuntimeError("pool down")
        monkeypatch.setattr(db, "db_conn", boom)
        assert se._research_overwrite_allowed("X", "UP") is False


class TestAtomicStore:
    def _patch(self, monkeypatch, row):
        import core.db as db
        conn = _Conn(row)
        monkeypatch.setattr(db, "db_conn", lambda: conn)
        monkeypatch.setattr(
            "core.precision_gate.invalidate_global_threshold_cache", lambda: None,
        )
        monkeypatch.setattr(
            "core.precision_gate.invalidate_global_threshold_persistent", lambda cur: None,
        )
        return conn

    def test_research_check_happens_after_transaction_lock(self, monkeypatch):
        conn = self._patch(monkeypatch, None)
        assert se._store_direction_model(
            "X", "UP", "BYTES", json.dumps({"tier": "research"}),
        ) is True
        statements = [sql for sql, _params in conn.cur.executed]
        lock_idx = next(i for i, sql in enumerate(statements)
                        if "pg_advisory_xact_lock" in sql)
        read_idx = next(i for i, sql in enumerate(statements)
                        if "SELECT value FROM ghost_v3_model" in sql)
        write_idx = next(i for i, sql in enumerate(statements)
                         if "INSERT INTO ghost_v3_model" in sql)
        assert lock_idx < read_idx < write_idx

    def test_atomic_store_refuses_serveable_proven_incumbent(self, monkeypatch):
        conn = self._patch(monkeypatch, (_fresh_proven_meta(),))
        assert se._store_direction_model(
            "X", "UP", "BYTES", json.dumps({"tier": "research"}),
        ) is False
        assert not any("INSERT INTO ghost_v3_model" in sql
                       for sql, _params in conn.cur.executed)

    def test_proven_writer_also_takes_same_lock(self, monkeypatch):
        from core.research_activation import _lock_name

        conn = self._patch(monkeypatch, None)
        assert se._store_direction_model(
            "X", "UP", "BYTES", json.dumps({"tier": "proven"}),
        ) is True
        assert any("pg_advisory_xact_lock" in sql
                   for sql, _params in conn.cur.executed)
        lock_params = next(
            params for sql, params in conn.cur.executed
            if "pg_advisory_xact_lock" in sql
        )
        assert lock_params == (_lock_name("X", "UP"),)
        assert not any("FROM ghost_research_activation_log" in sql
                       for sql, _params in conn.cur.executed)

    def test_proven_retrain_supersedes_active_lease(self, monkeypatch):
        import core.db as db

        class _LeaseCursor:
            def __init__(self):
                self.executed = []
                self.latest_event = ("ACTIVATED", "a" * 64)
                self.predecessor_exists = True

            def execute(self, sql, params=None):
                normalized = " ".join(sql.split())
                self.executed.append((normalized, params))
                if normalized.startswith("SELECT to_regclass"):
                    self._row = (
                        "ghost_research_activation_log",
                        "ghost_research_activation_predecessors",
                    )
                elif normalized.startswith("SELECT event_type, artifact_sha"):
                    self._row = self.latest_event
                elif normalized.startswith("INSERT INTO ghost_research_activation_log"):
                    self.latest_event = (params[0], params[3])
                    self._row = None
                elif normalized.startswith(
                    "DELETE FROM ghost_research_activation_predecessors"
                ):
                    self.predecessor_exists = False
                    self._row = None
                else:
                    self._row = None

            def fetchone(self):
                return self._row

        class _LeaseConnection:
            def __init__(self):
                self.cur = _LeaseCursor()

            def cursor(self):
                return self.cur

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        conn = _LeaseConnection()
        monkeypatch.setattr(db, "db_conn", lambda: conn)
        monkeypatch.setattr(
            "core.precision_gate.invalidate_global_threshold_cache", lambda: None,
        )
        monkeypatch.setattr(
            "core.precision_gate.invalidate_global_threshold_persistent",
            lambda cur: None,
        )

        assert se._store_direction_model(
            "X", "UP", "BYTES", json.dumps({"tier": "proven"}),
        ) is True

        assert conn.cur.latest_event == ("SUPERSEDED", "a" * 64)
        assert conn.cur.predecessor_exists is False
        event_params = next(
            params for sql, params in conn.cur.executed
            if sql.startswith("INSERT INTO ghost_research_activation_log")
        )
        assert event_params[4] == "ordinary_proven_model_retrained"


# ── Fire-path hard block + status honesty (source tripwires — the
#    checks live inside closures, same style as the doctrine tripwires) ──

def test_research_artifact_loads_scores_and_never_fires(monkeypatch):
    import base64
    import hashlib
    import pickle

    import core.db as db

    raw = pickle.dumps(_TrainStubModel())
    meta = json.loads(_fresh_proven_meta())
    meta.update({
        "tier": "research", "feature_cols": list(se.FEATURE_COLS),
        "model_sha256": hashlib.sha256(raw).hexdigest(),
    })
    values = {
        "meta_TEST_up": json.dumps(meta),
        "model_TEST_up": base64.b64encode(raw).decode("ascii"),
    }

    class _LoadCur:
        def execute(self, sql, params=None): self.key = params[0]
        def fetchone(self):
            value = values.get(self.key)
            return (value,) if value is not None else None
    class _LoadConn:
        def cursor(self): return _LoadCur()
        def __enter__(self): return self
        def __exit__(self, *args): return False

    monkeypatch.setattr(db, "db_conn", lambda: _LoadConn())
    se.invalidate_model_cache("TEST")
    model, cols, loaded_meta = se.load_model("TEST", "UP")
    assert model is not None and loaded_meta["tier"] == "research"

    rows = []
    for i in range(220):
        px = 100.0 + i * 0.1
        rows.append({
            "ts": f"2026-05-{(i % 20) + 1:02d}T21:00:00Z",
            "open": px, "high": px + 0.5, "low": px - 0.5,
            "close": px, "volume": 1000 + i,
        })
    monkeypatch.setattr(se, "_fetch_ohlcv", lambda *args, **kwargs: rows)
    original_load = se.load_model
    monkeypatch.setattr(
        se, "load_model",
        lambda symbol, direction="UP": (
            (model, cols, loaded_meta) if direction == "UP"
            else (None, None, None)
        ),
    )
    scores = {}
    signal, reason = se.predict_live_ex("TEST", "stock", scores=scores)
    assert signal is None and reason == "research_tier"
    assert scores["up_prob"] == 0.6
    assert scores["model_identity_by_direction"]["UP"]["model_sha256"] == meta["model_sha256"]
    monkeypatch.setattr(se, "load_model", original_load)


class TestFirePathTripwires:
    def _src(self):
        path = os.path.join(os.path.dirname(__file__), "..", "core",
                            "signal_engine.py")
        with open(path) as f:
            return f.read()

    def test_evaluate_lane_blocks_research_before_floors(self):
        src = self._src()
        lane = src.split("def _evaluate_lane")[1].split("def ")[0]
        tier_pos = lane.find('"research"')
        meta_gate_pos = lane.find('"meta_gate"')
        assert tier_pos != -1, "_evaluate_lane must hard-block research tier"
        assert 'return None, "research_tier"' in lane
        assert tier_pos < meta_gate_pos, "tier block must precede floor math"

    def test_status_carries_tier_and_research_counts(self):
        src = self._src()
        assert '"tier": m.get("tier", "proven")' in src
        assert '"serveable_research"' in src
        assert 'block = "research_tier"' in src

    def test_research_tier_default_on_env_off_switch(self, monkeypatch):
        monkeypatch.delenv("V3_RESEARCH_TIER", raising=False)
        assert se._v3_research_tier_enabled() is True
        monkeypatch.setenv("V3_RESEARCH_TIER", "0")
        assert se._v3_research_tier_enabled() is False


# ── Trainer returns research bytes when gates fail ───────────────────

class TestTrainerTierTag:
    def test_gate_fail_return_shape_by_env(self):
        """Source-level: the single failure return is env-gated and the meta
        carries tier + gate_fail_reason (full training run is exercised by
        the existing suite; this pins the contract)."""
        path = os.path.join(os.path.dirname(__file__), "..", "core",
                            "signal_engine.py")
        with open(path) as f:
            src = f.read()
        body = src.split("def _train_one_direction")[1].split("\ndef ")[0]
        assert "if not passes and not _v3_research_tier_enabled():" in body
        assert 'tier = "proven" if passes else "research"' in body
        assert '"tier": tier, "gate_fail_reason": fail_reason' in body
        assert "return passes, detail, model_bytes, meta" in body or \
               "return True, detail, model_bytes, meta" in body


# ── Options collector breaker fix (same PR) ──────────────────────────

class TestCollectorBreakerStop:
    def test_run_attempts_every_symbol_despite_misses(self, monkeypatch):
        """No early-stop: a per-symbol miss (rate-limit) must not abort the run;
        the loop attempts all symbols and stores the ones that succeed."""
        import core.options_snapshots as osnap
        import core.db as db

        class _C:
            def cursor(self): return self
            def execute(self, *a): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(db, "db_conn", lambda: _C())
        attempted = []

        def fake_snap(s):
            attempted.append(s)
            # B is a transient rate-limit miss; A and C succeed.
            if s == "B":
                return None
            return {"symbol": s, "snap_date": "2026-07-18", "ts": 1,
                    "nearest_expiry": None, "underlying": None,
                    "call_volume": 10, "put_volume": 5, "call_oi": None,
                    "put_oi": None, "pcr_volume": 0.5, "pcr_oi": None,
                    "atm_iv_call": None, "atm_iv_put": None, "available": True}

        monkeypatch.setattr(osnap, "snapshot_symbol", fake_snap)
        out = osnap.record_snapshots(["A", "B", "C"], delay_s=0)
        assert attempted == ["A", "B", "C"]      # never bailed early
        assert out["stored"] == 2 and out["failed"] == 1

    def test_delay_default_is_modest_for_alpaca(self):
        # Alpaca (primary source) has no yfinance-style 15/min cap, so the
        # per-symbol delay is small — the whole watchlist accrues in one run.
        import inspect
        import core.options_snapshots as osnap
        sig = inspect.signature(osnap.record_snapshots)
        assert sig.parameters["delay_s"].default == 0.5
