"""Tests for core/research_artifacts.py — immutable artifact registry."""
import json
import time
import pytest
from core.research_artifacts import (
    ArtifactMeta,
    ArtifactLifecycleEvent,
    compute_artifact_sha,
    ensure_research_artifact_tables,
    register_artifact,
    retire_artifact,
    get_artifact,
    list_artifacts,
    get_lifecycle_events,
)


def _make_meta(**overrides):
    kwargs = {
        "artifact_sha": "a" * 64,
        "contract_id": "abc123",
        "policy_lineage_id": "lineage_1",
        "policy_lineage_version": 1,
        "symbol_scope": ("WOLF",),
        "output_domain": ("UP", "DOWN"),
        "feature_schema": "test_v1",
        "evidence_schema": "test_evidence_v1",
        "validation_schema": "test_validation_v1",
        "horizon_bars": 3,
        "training_manifest_sha": "b" * 64,
        "calibration_proof": {"ok": True, "threshold": 0.55},
        "gate_proof": {"ok": True, "wilson_low": 0.72},
        "feature_order": ("rsi", "macd"),
    }
    kwargs.update(overrides)
    return ArtifactMeta(**kwargs)


# ── ArtifactMeta invariants ─────────────────────────────────────────────────

def test_artifact_meta_valid():
    m = _make_meta()
    assert m.artifact_sha == "a" * 64
    assert m.status == "ACTIVE"


def test_artifact_meta_rejects_invalid_sha():
    with pytest.raises(ValueError, match="artifact_sha"):
        _make_meta(artifact_sha="short")


def test_artifact_meta_rejects_empty_contract_id():
    with pytest.raises(ValueError, match="contract_id"):
        _make_meta(contract_id="")


def test_artifact_meta_rejects_zero_lineage_version():
    with pytest.raises(ValueError, match="policy_lineage_version"):
        _make_meta(policy_lineage_version=0)


def test_artifact_meta_is_frozen():
    m = _make_meta()
    with pytest.raises(Exception):
        m.status = "RETIRED"  # type: ignore


def test_artifact_lifecycle_event_is_frozen():
    e = ArtifactLifecycleEvent(artifact_sha="a" * 64, event_type="REGISTERED", event_ts=1000)
    with pytest.raises(Exception):
        e.event_type = "RETIRED"  # type: ignore


# ── SHA computation ─────────────────────────────────────────────────────────

def test_compute_artifact_sha_deterministic():
    sha1 = compute_artifact_sha("a" * 64, "abc", "UP", "lineage_1", 1, ("rsi", "macd"))
    sha2 = compute_artifact_sha("a" * 64, "abc", "UP", "lineage_1", 1, ("rsi", "macd"))
    assert sha1 == sha2
    assert len(sha1) == 64


def test_compute_artifact_sha_changes_on_different_model():
    sha1 = compute_artifact_sha("a" * 64, "abc", "UP", "lineage_1", 1, ("rsi",))
    sha2 = compute_artifact_sha("b" * 64, "abc", "UP", "lineage_1", 1, ("rsi",))
    assert sha1 != sha2


def test_compute_artifact_sha_feature_order_matters():
    """Feature order is preserved, not sorted — reordering changes identity."""
    sha1 = compute_artifact_sha("a" * 64, "abc", "UP", "lineage_1", 1, ("macd", "rsi"))
    sha2 = compute_artifact_sha("a" * 64, "abc", "UP", "lineage_1", 1, ("rsi", "macd"))
    assert sha1 != sha2


def test_compute_artifact_sha_schema_change_changes_identity():
    sha1 = compute_artifact_sha("a" * 64, "abc", "UP", "lineage_1", 1, ("rsi",), feature_schema="v1")
    sha2 = compute_artifact_sha("a" * 64, "abc", "UP", "lineage_1", 1, ("rsi",), feature_schema="v2")
    assert sha1 != sha2


def test_compute_model_sha256():
    from core.research_artifacts import compute_model_sha256
    raw = b"test model bytes"
    sha = compute_model_sha256(raw)
    assert len(sha) == 64
    # Same bytes = same hash
    assert compute_model_sha256(raw) == sha
    # Different bytes = different hash
    assert compute_model_sha256(b"different") != sha


# ── DB integration tests ───────────────────────────────────────────────────

@pytest.mark.integration
def test_register_and_get_artifact():
    from core.db import db_conn
    meta = _make_meta(artifact_sha=compute_artifact_sha("test_payload", "abc", "l1", 1, ("rsi",)))
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_research_artifact_tables(cur)
        assert register_artifact(meta, "test_payload", cur=cur)
        conn.commit()

    with db_conn() as conn:
        cur = conn.cursor()
        loaded = get_artifact(meta.artifact_sha, cur=cur)
        assert loaded is not None
        assert loaded["contract_id"] == "abc"
        assert loaded["status"] == "ACTIVE"


@pytest.mark.integration
def test_register_idempotent():
    from core.db import db_conn
    meta = _make_meta(artifact_sha=compute_artifact_sha("idem", "abc", "l1", 1, ("rsi",)))
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_research_artifact_tables(cur)
        assert register_artifact(meta, cur=cur)
        assert not register_artifact(meta, cur=cur)  # second call is no-op
        conn.commit()


@pytest.mark.integration
def test_retire_artifact():
    from core.db import db_conn
    meta = _make_meta(artifact_sha=compute_artifact_sha("retire_test", "abc", "l1", 1, ("rsi",)))
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_research_artifact_tables(cur)
        register_artifact(meta, cur=cur)
        conn.commit()

    with db_conn() as conn:
        cur = conn.cursor()
        assert retire_artifact(meta.artifact_sha, "obsolete", cur=cur)
        conn.commit()

    with db_conn() as conn:
        cur = conn.cursor()
        loaded = get_artifact(meta.artifact_sha, cur=cur)
        assert loaded["status"] == "RETIRED"
        assert loaded["retirement_reason"] == "obsolete"


@pytest.mark.integration
def test_lifecycle_events():
    from core.db import db_conn
    meta = _make_meta(artifact_sha=compute_artifact_sha("events_test", "abc", "l1", 1, ("rsi",)))
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_research_artifact_tables(cur)
        register_artifact(meta, cur=cur)
        retire_artifact(meta.artifact_sha, "done", cur=cur)
        conn.commit()

    with db_conn() as conn:
        cur = conn.cursor()
        events = get_lifecycle_events(meta.artifact_sha, cur=cur)
        assert len(events) == 2
        assert events[0]["event_type"] == "REGISTERED"
        assert events[1]["event_type"] == "RETIRED"


@pytest.mark.integration
def test_list_artifacts_filtered():
    from core.db import db_conn
    meta1 = _make_meta(artifact_sha=compute_artifact_sha("list1", "contract_a", "l1", 1, ("rsi",)),
                       contract_id="contract_a")
    meta2 = _make_meta(artifact_sha=compute_artifact_sha("list2", "contract_b", "l2", 1, ("rsi",)),
                       contract_id="contract_b")
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_research_artifact_tables(cur)
        register_artifact(meta1, cur=cur)
        register_artifact(meta2, cur=cur)
        conn.commit()

    with db_conn() as conn:
        cur = conn.cursor()
        all_active = list_artifacts(cur=cur)
        assert len(all_active) >= 2

        filtered = list_artifacts(contract_id="contract_a", cur=cur)
        assert len(filtered) == 1
        assert filtered[0]["contract_id"] == "contract_a"
