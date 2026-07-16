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
    monkeypatch.setattr(se, "_research_overwrite_allowed",
                        lambda sym, d: research_allowed)
    monkeypatch.setattr(se, "_store_direction_model",
                        lambda sym, d, b, m: stores.append((sym, d, m)))
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


# ── Overwrite guard ──────────────────────────────────────────────────

class _Cur:
    def __init__(self, row): self._row = row
    def execute(self, sql, params=None): pass
    def fetchone(self): return self._row


class _Conn:
    def __init__(self, row): self._row = row
    def cursor(self): return _Cur(self._row)
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _fresh_proven_meta():
    return json.dumps({
        "tier": "proven", "label_type": se.LABEL_TYPE,
        "label_schema": se._v3_label_schema(),
        "feature_schema": se._v3_feature_schema(),
        "trained_at": time.time(),
    })


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


# ── Fire-path hard block + status honesty (source tripwires — the
#    checks live inside closures, same style as the doctrine tripwires) ──

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
    def test_breaker_open_stops_early_with_honest_count(self, monkeypatch):
        import core.options_snapshots as osnap
        import core.yfinance_client as yfc
        import core.db as db

        class _C:
            def cursor(self): return self
            def execute(self, *a): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(db, "db_conn", lambda: _C())
        monkeypatch.setattr(yfc, "_gate", lambda: False)
        calls = []
        monkeypatch.setattr(osnap, "snapshot_symbol",
                            lambda s: calls.append(s))
        out = osnap.record_snapshots(["A", "B", "C"], delay_s=0)
        assert out["skipped_breaker"] == 3
        assert out["stored"] == 0 and calls == []

    def test_delay_default_respects_breaker_budget(self):
        import inspect
        import core.options_snapshots as osnap
        sig = inspect.signature(osnap.record_snapshots)
        assert sig.parameters["delay_s"].default == 4.0
