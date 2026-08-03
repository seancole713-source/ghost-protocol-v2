"""Tests for core/research_activation.py — auto-activation and rollback."""
import json
import time
import pytest
from core.research_activation import (
    ensure_activation_tables,
    compute_evidence_lease,
    can_activate,
    activate_artifact,
    rollback_if_degraded,
    get_activation_history,
    _lease_window_s,
    _lease_min_observations,
)


# ── lease configuration ────────────────────────────────────────────────────

def test_lease_window_default():
    assert _lease_window_s() >= 86400


def test_lease_min_observations_default():
    assert _lease_min_observations() >= 1


# ── can_activate rejects non-TP/SL ─────────────────────────────────────────

def test_can_activate_rejects_non_live_contract(monkeypatch):
    """can_activate must reject contracts that aren't live-compatible."""
    from core.research_contracts import get_contract
    c = get_contract("volatility_expansion", "v1")
    assert c is not None

    monkeypatch.setattr(
        "core.research_contracts.get_contract_by_id",
        lambda cid: c,
    )
    monkeypatch.setattr(
        "core.research_contracts.is_live_compatible",
        lambda contract: False,
    )
    monkeypatch.setattr(
        "core.research_artifacts.get_artifact",
        lambda sha, cur=None: {
            "contract_id": "test", "status": "ACTIVE",
            "output_domain": ("EXPAND", "CONTRACT"),
            "symbol_scope": ("WOLF",),
            "calibration_proof": {"ok": True},
            "gate_proof": {"ok": True},
            "feature_schema": "v1",
            "evidence_schema": "v1",
            "validation_schema": "v1",
            "horizon_bars": 5,
            "trained_at": int(time.time()),
            "feature_order": ("rsi",),
            "payload_bytes": "test",
        },
    )

    # Pass a fake cursor to bypass DB connection
    eligible, reason, _ = can_activate(
        artifact_sha="a" * 64, symbol="WOLF", direction="UP",
        cur=_FakeCur(),
    )
    assert eligible is False
    assert "not_live_compatible" in reason


class _FakeCur:
    def execute(self, sql, params=None):
        pass
    def fetchone(self):
        return None
    def fetchall(self):
        return []


# ── rollback_if_degraded ──────────────────────────────────────────────────

def test_rollback_no_active_model(monkeypatch):
    """Rollback returns 'none' when there's no active model."""
    class _FakeCur:
        def execute(self, sql, params=None):
            self._sql = sql
        def fetchone(self):
            return None

    result = rollback_if_degraded(symbol="WOLF", direction="UP", cur=_FakeCur())
    assert result["action"] == "none"


def test_rollback_not_activated_artifact(monkeypatch):
    """Rollback skips models that weren't activated by the research system."""
    class _FakeCur:
        def execute(self, sql, params=None):
            pass
        def fetchone(self):
            # Return a meta row without _activation_artifact_sha
            return (json.dumps({"tier": "proven", "direction": "UP",
                                "model_sha256": "b" * 64}),)

    result = rollback_if_degraded(symbol="WOLF", direction="UP", cur=_FakeCur())
    assert result["action"] == "none"
    assert "not_an_activated" in result["reason"]


# ── activation history ─────────────────────────────────────────────────────

@pytest.mark.integration
def test_activation_history_empty():
    from core.db import db_conn
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_activation_tables(cur)
        history = get_activation_history(cur=cur)
        assert isinstance(history, list)
