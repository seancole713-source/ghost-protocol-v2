"""Tests for core/research_ledger.py — isolated research evidence ledger."""
import time
import pytest
from core.research_ledger import (
    log_research_prediction,
    resolve_research_prediction,
    get_pending_predictions,
    get_resolved_predictions,
    get_outbox_pending,
    mark_outbox_processed,
)


# ── unit tests (no DB) ─────────────────────────────────────────────────────

def test_import_does_not_touch_live_tables():
    """Module import must not reference live prediction tables in SQL."""
    import ast
    import core.research_ledger as rl
    source = open(rl.__file__).read()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = node.value
            if not any(kw in s.upper() for kw in ("CREATE ", "INSERT ", "SELECT ", "UPDATE ", "DELETE ", "ALTER ")):
                continue
            lower = s.lower()
            for forbidden in ("predictions", "ghost_shadow_outcomes", "ghost_v3_model",
                              "ghost_paper_trades", "paper_trades", "super_ghost_predictions"):
                if forbidden in lower and f"ghost_research_{forbidden}" not in lower:
                    raise AssertionError(f"Found live table reference: {forbidden}")


# ── DB integration tests ───────────────────────────────────────────────────

@pytest.mark.integration
def test_log_and_resolve_prediction():
    from core.db import db_conn
    from core.research_schema import ensure_research_schema
    now = int(time.time())
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_research_schema(cur)
        conn.commit()

    with db_conn() as conn:
        cur = conn.cursor()
        pred_id = log_research_prediction(
            contract_id="csha1",
            artifact_sha="a" * 64,
            policy_lineage_id="lineage_1",
            symbol="WOLF",
            direction="UP",
            issued_ts=now,
            feature_available_ts=now,
            output="UP",
            calibrated_prob=0.72,
            threshold=0.55,
            cur=cur,
        )
        conn.commit()

    assert pred_id is not None
    assert pred_id > 0

    with db_conn() as conn:
        cur = conn.cursor()
        resolved = resolve_research_prediction(
            prediction_id=pred_id,
            resolver_id="tp_sl_bar_path/v1",
            resolver_version="1.0.0",
            outcome="WIN",
            observed_value=3.5,
            resolved_ts=now + 86400 * 3,
            evidence_available_ts=now + 86400 * 3,
            cur=cur,
        )
        conn.commit()
        assert resolved is True

    # Verify outbox row was created
    with db_conn() as conn:
        cur = conn.cursor()
        pending = get_outbox_pending(cur=cur)
        assert len(pending) >= 1
        assert pending[0]["prediction_id"] == pred_id


@pytest.mark.integration
def test_log_prediction_idempotent():
    from core.db import db_conn
    from core.research_schema import ensure_research_schema
    now = int(time.time())
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_research_schema(cur)
        conn.commit()

    with db_conn() as conn:
        cur = conn.cursor()
        id1 = log_research_prediction(
            contract_id="csha2", artifact_sha="b" * 64,
            policy_lineage_id="l1", symbol="WOLF", direction="UP",
            issued_ts=now, feature_available_ts=now, output="UP", cur=cur,
        )
        id2 = log_research_prediction(
            contract_id="csha2", artifact_sha="b" * 64,
            policy_lineage_id="l1", symbol="WOLF", direction="UP",
            issued_ts=now, feature_available_ts=now, output="UP", cur=cur,
        )
        conn.commit()
        assert id1 == id2


@pytest.mark.integration
def test_resolution_idempotent():
    from core.db import db_conn
    from core.research_schema import ensure_research_schema
    now = int(time.time())
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_research_schema(cur)
        conn.commit()

    with db_conn() as conn:
        cur = conn.cursor()
        pred_id = log_research_prediction(
            contract_id="csha3", artifact_sha="c" * 64,
            policy_lineage_id="l1", symbol="WOLF", direction="UP",
            issued_ts=now, feature_available_ts=now, output="UP", cur=cur,
        )
        conn.commit()

    with db_conn() as conn:
        cur = conn.cursor()
        assert resolve_research_prediction(
            prediction_id=pred_id, resolver_id="r1", resolver_version="1.0",
            outcome="WIN", resolved_ts=now + 1,
            evidence_available_ts=now + 1, cur=cur,
        )
        assert not resolve_research_prediction(
            prediction_id=pred_id, resolver_id="r1", resolver_version="1.0",
            outcome="LOSS", resolved_ts=now + 1,
            evidence_available_ts=now + 1, cur=cur,
        )
        conn.commit()


@pytest.mark.integration
def test_get_pending_and_resolved():
    from core.db import db_conn
    from core.research_schema import ensure_research_schema
    now = int(time.time())
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_research_schema(cur)
        conn.commit()

    with db_conn() as conn:
        cur = conn.cursor()
        p1 = log_research_prediction(
            contract_id="csha4", artifact_sha="d" * 64,
            policy_lineage_id="l1", symbol="WOLF", direction="UP",
            issued_ts=now, feature_available_ts=now, output="UP", cur=cur,
        )
        p2 = log_research_prediction(
            contract_id="csha4", artifact_sha="d" * 64,
            policy_lineage_id="l1", symbol="WOLF", direction="UP",
            issued_ts=now + 1, feature_available_ts=now + 1, output="DOWN", cur=cur,
        )
        resolve_research_prediction(
            prediction_id=p1, resolver_id="r1", resolver_version="1.0",
            outcome="WIN", resolved_ts=now + 2,
            evidence_available_ts=now + 2, cur=cur,
        )
        conn.commit()

    with db_conn() as conn:
        cur = conn.cursor()
        pending = get_pending_predictions(contract_id="csha4", cur=cur)
        assert len(pending) == 1
        assert pending[0]["id"] == p2

        resolved = get_resolved_predictions(contract_id="csha4", cur=cur)
        assert len(resolved) == 1
        assert resolved[0]["id"] == p1
        assert resolved[0]["outcome"] == "WIN"


@pytest.mark.integration
def test_outbox_lifecycle():
    from core.db import db_conn
    from core.research_schema import ensure_research_schema
    now = int(time.time())
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_research_schema(cur)
        conn.commit()

    with db_conn() as conn:
        cur = conn.cursor()
        pred_id = log_research_prediction(
            contract_id="csha5", artifact_sha="e" * 64,
            policy_lineage_id="l1", symbol="WOLF", direction="UP",
            issued_ts=now, feature_available_ts=now, output="UP", cur=cur,
        )
        resolve_research_prediction(
            prediction_id=pred_id, resolver_id="r1", resolver_version="1.0",
            outcome="WIN", resolved_ts=now + 1,
            evidence_available_ts=now + 1, cur=cur,
        )
        conn.commit()

    with db_conn() as conn:
        cur = conn.cursor()
        pending = get_outbox_pending(cur=cur)
        assert len(pending) >= 1
        ob = pending[0]
        assert ob["status"] == "PENDING"

        assert mark_outbox_processed(ob["id"], cur=cur)
        conn.commit()

    with db_conn() as conn:
        cur = conn.cursor()
        pending2 = get_outbox_pending(cur=cur)
        assert all(p["id"] != ob["id"] for p in pending2)
