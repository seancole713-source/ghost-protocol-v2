"""PostgreSQL integration coverage for the isolated research platform."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import pickle
import time
import uuid
from contextlib import contextmanager

import psycopg2
import pytest

from core.research_forward import (
    evaluate_forward_proof,
    register_forward_experiment,
    update_forward_proof_status,
)
from core.research_ledger import log_research_evaluation
from core.research_ledger import (
    get_outbox_pending,
    get_pending_predictions,
    log_research_prediction,
    mark_outbox_processed,
    resolve_research_prediction,
)
from core.research_resolvers import resolve_pending_tp_sl_prediction
from core.research_schema import ensure_research_schema


def _integration_enabled() -> bool:
    return (
        bool(os.getenv("TEST_DATABASE_URL"))
        and os.getenv("GHOST_INTEGRATION_TESTS", "0") in ("1", "true", "TRUE")
    )


@contextmanager
def _isolated_schema(prefix: str):
    if not _integration_enabled():
        pytest.skip(
            "Integration DB tests disabled. Set TEST_DATABASE_URL and "
            "GHOST_INTEGRATION_TESTS=1."
        )
    schema = f"{prefix}_{uuid.uuid4().hex[:12]}"
    conn = psycopg2.connect(os.environ["TEST_DATABASE_URL"])
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
        yield conn, schema
    finally:
        conn.rollback()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.close()


@pytest.mark.integration
def test_clean_schema_is_idempotent_and_scoped():
    with _isolated_schema("research_clean") as (conn, schema):
        with conn.cursor() as cur:
            ensure_research_schema(cur)
            ensure_research_schema(cur)
            cur.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema=%s AND table_name LIKE 'ghost_research_%%'",
                (schema,),
            )
            assert cur.fetchone()[0] == 14
            cur.execute(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname=%s AND indexname='idx_research_eval_artifact_time'",
                (schema,),
            )
            assert "(contract_id, artifact_sha, evaluated_ts)" in cur.fetchone()[0]


@pytest.mark.integration
def test_standalone_artifact_schema_adds_model_sha_to_existing_table():
    from core.research_artifacts import ensure_research_artifact_tables

    with _isolated_schema("artifact_legacy") as (conn, schema):
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE ghost_research_artifacts (
                    artifact_sha TEXT PRIMARY KEY,
                    contract_id TEXT NOT NULL,
                    policy_lineage_id TEXT NOT NULL,
                    policy_lineage_version INT NOT NULL,
                    symbol_scope TEXT NOT NULL,
                    output_domain TEXT NOT NULL,
                    feature_schema TEXT NOT NULL,
                    evidence_schema TEXT NOT NULL,
                    validation_schema TEXT NOT NULL,
                    horizon_bars INT NOT NULL,
                    training_manifest_sha TEXT NOT NULL,
                    calibration_proof JSONB,
                    gate_proof JSONB,
                    feature_order TEXT NOT NULL,
                    payload_bytes TEXT,
                    created_at BIGINT NOT NULL,
                    trained_at BIGINT NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    retired_at BIGINT NOT NULL DEFAULT 0,
                    retirement_reason TEXT DEFAULT ''
                )
                """
            )
            ensure_research_artifact_tables(cur)
            cur.execute(
                "SELECT column_default,is_nullable FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name='ghost_research_artifacts' "
                "AND column_name='model_sha256'",
                (schema,),
            )
            assert cur.fetchone() == ("''::text", "NO")
            cur.execute("SELECT to_regclass('ghost_research_artifact_events')")
            assert cur.fetchone()[0] == "ghost_research_artifact_events"


@pytest.mark.integration
def test_legacy_upgrade_preserves_rows_and_replaces_stale_indexes():
    with _isolated_schema("research_legacy") as (conn, schema):
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE ghost_research_contracts (
                    id SERIAL PRIMARY KEY,
                    contract_sha TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    label_type TEXT NOT NULL,
                    live_compatible BOOLEAN NOT NULL DEFAULT FALSE,
                    definition JSONB NOT NULL,
                    created_at BIGINT NOT NULL,
                    UNIQUE(name, version)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE ghost_research_artifacts (
                    id SERIAL PRIMARY KEY,
                    artifact_sha TEXT NOT NULL UNIQUE,
                    model_sha256 TEXT NOT NULL,
                    contract_sha TEXT NOT NULL REFERENCES ghost_research_contracts(contract_sha),
                    direction TEXT NOT NULL,
                    lineage_version TEXT NOT NULL,
                    feature_names TEXT[] NOT NULL,
                    label_schema TEXT NOT NULL,
                    validation_schema TEXT NOT NULL,
                    hold_bars INT NOT NULL,
                    training_manifest_sha TEXT,
                    calibration_proof JSONB,
                    gate_proof JSONB,
                    metadata JSONB NOT NULL DEFAULT '{}',
                    created_at BIGINT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE ghost_research_artifact_events (
                    id SERIAL PRIMARY KEY,
                    artifact_sha TEXT NOT NULL REFERENCES ghost_research_artifacts(artifact_sha),
                    event_type TEXT NOT NULL,
                    event_payload JSONB NOT NULL DEFAULT '{}',
                    created_at BIGINT NOT NULL
                )
                """
            )
            cur.execute(
                "CREATE INDEX idx_research_artifacts_contract "
                "ON ghost_research_artifacts(contract_sha,direction)"
            )
            cur.execute(
                "CREATE INDEX idx_research_artifact_events_sha "
                "ON ghost_research_artifact_events(artifact_sha,created_at DESC)"
            )
            cur.execute(
                "INSERT INTO ghost_research_contracts "
                "VALUES (DEFAULT,'legacy-contract','tp','v1','tp_sl',true,'{}',10)"
            )
            cur.execute(
                """
                INSERT INTO ghost_research_artifacts
                    (artifact_sha,model_sha256,contract_sha,direction,lineage_version,
                     feature_names,label_schema,validation_schema,hold_bars,
                     training_manifest_sha,calibration_proof,gate_proof,metadata,created_at)
                VALUES (%s,%s,'legacy-contract','UP','7',ARRAY['rsi','macd'],
                        'label-v1','validation-v1',3,NULL,'{}','{}','{}',11)
                """,
                ("a" * 64, "b" * 64),
            )
            cur.execute(
                "INSERT INTO ghost_research_artifact_events "
                "(artifact_sha,event_type,event_payload,created_at) "
                "VALUES (%s,'REGISTERED','{\"source\":\"legacy\"}',12)",
                ("a" * 64,),
            )
            ensure_research_schema(cur)
            ensure_research_schema(cur)
            cur.execute(
                "SELECT contract_id,policy_lineage_version,output_domain,evidence_schema,"
                "horizon_bars,feature_order,training_manifest_sha "
                "FROM ghost_research_artifacts WHERE artifact_sha=%s",
                ("a" * 64,),
            )
            assert cur.fetchone() == (
                "legacy-contract", 7, '["UP"]', "label-v1", 3,
                '["rsi","macd"]', "",
            )
            cur.execute(
                "SELECT event_ts,metadata FROM ghost_research_artifact_events "
                "WHERE artifact_sha=%s",
                ("a" * 64,),
            )
            assert cur.fetchone() == (12, {"source": "legacy"})
            cur.execute(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname=%s AND indexname='idx_research_artifacts_contract'",
                (schema,),
            )
            assert "(contract_id, status)" in cur.fetchone()[0]
            cur.execute(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname=%s AND indexname='idx_research_artifact_events_sha'",
                (schema,),
            )
            assert "(artifact_sha, event_ts DESC)" in cur.fetchone()[0]


@pytest.mark.integration
def test_fixed_50_lifecycle_proves_only_complete_evidence():
    with _isolated_schema("research_lifecycle") as (conn, _schema):
        from core.research_artifacts import compute_artifact_sha

        now = int(time.time()) - 60 * 86400
        contract_id = "tp-sl-contract"
        raw_model = pickle.dumps(
            {"generation": "fixed-proof"}, protocol=pickle.HIGHEST_PROTOCOL,
        )
        payload = base64.b64encode(raw_model).decode("ascii")
        model_sha = hashlib.sha256(raw_model).hexdigest()
        calibration_proof = {"threshold": 0.70}
        gate_proof = {"feature_inversions": []}
        artifact_sha = compute_artifact_sha(
            model_sha256=model_sha,
            contract_id=contract_id,
            direction="UP",
            policy_lineage_id="WOLF/UP",
            policy_lineage_version=1,
            feature_order=("rsi",),
            feature_schema="features-v1",
            label_schema="labels-v1",
            validation_schema="validation-v1",
            hold_bars=3,
            calibration_proof=calibration_proof,
            gate_proof=gate_proof,
            symbol_scope=("WOLF",),
            trained_at=now,
        )
        with conn.cursor() as cur:
            ensure_research_schema(cur)
            cur.execute(
                """
                INSERT INTO ghost_research_artifacts
                    (artifact_sha,model_sha256,contract_id,policy_lineage_id,
                     policy_lineage_version,symbol_scope,output_domain,
                     feature_schema,evidence_schema,validation_schema,
                     horizon_bars,training_manifest_sha,calibration_proof,
                     gate_proof,feature_order,payload_bytes,created_at,trained_at,status)
                VALUES (%s,%s,%s,'WOLF/UP',1,'["WOLF"]','["UP"]',
                        'features-v1','labels-v1','validation-v1',3,'',
                        %s,%s,'["rsi"]',%s,%s,%s,'ACTIVE')
                """,
                (
                    artifact_sha, model_sha, contract_id,
                    json.dumps(calibration_proof), json.dumps(gate_proof),
                    payload, now, now,
                ),
            )
            registration_id = register_forward_experiment(
                contract_id=contract_id,
                artifact_sha=artifact_sha,
                direction="UP",
                threshold=0.70,
                symbol_universe=["WOLF"],
                round_trip_slippage_bps=10.0,
                round_trip_commission_bps=0.0,
                cur=cur,
            )
            assert register_forward_experiment(
                contract_id=contract_id,
                artifact_sha=artifact_sha,
                direction="UP",
                threshold=0.70,
                symbol_universe=["WOLF"],
                round_trip_slippage_bps=10.0,
                round_trip_commission_bps=0.0,
                cur=cur,
            ) == registration_id
            with pytest.raises(ValueError, match="different_parameters"):
                register_forward_experiment(
                    contract_id=contract_id,
                    artifact_sha=artifact_sha,
                    direction="UP",
                    threshold=0.70,
                    symbol_universe=["WOLF"],
                    round_trip_slippage_bps=20.0,
                    round_trip_commission_bps=0.0,
                    cur=cur,
                )
            with pytest.raises(ValueError, match="threshold_mismatch"):
                register_forward_experiment(
                    contract_id=contract_id,
                    artifact_sha=artifact_sha,
                    direction="UP",
                    threshold=0.71,
                    symbol_universe=["WOLF"],
                    round_trip_slippage_bps=10.0,
                    round_trip_commission_bps=0.0,
                    cur=cur,
                )
            cur.execute(
                "SELECT count(*) FROM ghost_research_registrations "
                "WHERE contract_id=%s AND artifact_sha=%s AND direction='UP'",
                (contract_id, artifact_sha),
            )
            assert cur.fetchone()[0] == 1
            cur.execute(
                "UPDATE ghost_research_registrations SET registered_at_ts=%s "
                "WHERE registration_id=%s",
                (now - 10, registration_id),
            )
            outcomes = (["WIN"] * 5 + ["LOSS"]) * 8 + ["WIN", "WIN"]
            for index, outcome in enumerate(outcomes):
                issued_ts = now + index * 86400
                evaluation_date = time.strftime("%Y-%m-%d", time.gmtime(issued_ts))
                assert log_research_evaluation(
                    contract_id=contract_id,
                    artifact_sha=artifact_sha,
                    symbol="WOLF",
                    direction="UP",
                    evaluation_date=evaluation_date,
                    evaluated_ts=issued_ts,
                    feature_available_ts=issued_ts,
                    calibrated_prob=0.84,
                    threshold=0.70,
                    eligible=True,
                    fired=True,
                    reason="threshold_pass",
                    cur=cur,
                )
                cur.execute(
                    """
                    INSERT INTO ghost_research_predictions
                        (contract_id,artifact_sha,policy_lineage_id,symbol,direction,
                         issued_ts,feature_available_ts,output,calibrated_prob,threshold,
                         source_snapshot_sha,feature_snapshot_sha,selector_decision,
                         context,created_at)
                    VALUES (%s,%s,'WOLF/UP','WOLF','UP',%s,%s,'UP',0.84,0.70,
                            '','',%s,%s,%s)
                    RETURNING id
                    """,
                    (
                        contract_id, artifact_sha, issued_ts, issued_ts,
                        '{"passed":true}',
                        '{"entry_price":100.0,"target_price":103.0,'
                        '"stop_price":99.0,"hold_bars":3}',
                        issued_ts,
                    ),
                )
                prediction_id = cur.fetchone()[0]
                exit_price = 103.0 if outcome == "WIN" else 99.0
                cur.execute(
                    """
                    INSERT INTO ghost_research_resolutions
                        (prediction_id,resolver_id,resolver_version,outcome,
                         observed_value,resolved_ts,evidence_available_ts,
                         evidence_payload,evidence_sha,reason,created_at)
                    VALUES (%s,'tp_sl_bar_path/v1','1.0.0',%s,%s,%s,%s,
                            %s,'','bar_path',%s)
                    """,
                    (
                        prediction_id, outcome, exit_price, issued_ts + 1,
                        issued_ts + 1, '{}', issued_ts + 1,
                    ),
                )

            before = evaluate_forward_proof(registration_id, cur=cur)
            assert before["status"] == "PROVEN"
            assert before["persisted_status"] == "COLLECTING"
            assert before["n"] == 50
            assert before["wins"] == 42
            assert before["data_invalid"] == 0
            assert before["freeze_ts"] is not None
            assert all(gate["passed"] for gate in before["secondary_gates"].values())

            transitioned = update_forward_proof_status(registration_id, cur=cur)
            assert transitioned["status"] == "PROVEN"
            cur.execute(
                "SELECT status,closed_at_ts FROM ghost_research_registrations "
                "WHERE registration_id=%s",
                (registration_id,),
            )
            status, closed_at = cur.fetchone()
            assert status == "PROVEN"
            assert closed_at is not None

            assert not log_research_evaluation(
                contract_id=contract_id,
                artifact_sha=artifact_sha,
                symbol="WOLF",
                direction="UP",
                evaluation_date=time.strftime("%Y-%m-%d", time.gmtime(now)),
                evaluated_ts=now + 99,
                feature_available_ts=now + 99,
                calibrated_prob=0.99,
                threshold=0.70,
                eligible=True,
                fired=True,
                reason="threshold_pass",
                cur=cur,
            )


@pytest.mark.integration
def test_proven_artifact_activation_lease_and_rollback_lifecycle(monkeypatch):
    from core.research_activation import (
        activate_artifact,
        compute_evidence_lease,
        get_activation_history,
        rollback_if_degraded,
    )
    from core.research_artifacts import (
        ArtifactMeta,
        compute_artifact_sha,
        register_artifact,
    )
    from core.research_contracts import CURRENT_LIVE_CONTRACT_VERSION, get_contract
    import core.db as db
    import core.research_activation as activation
    import core.signal_engine as signal_engine

    with _isolated_schema("research_activation") as (conn, _schema):
        proof_start = int(time.time()) - 60 * 86400
        contract = get_contract("tp_sl_swing", CURRENT_LIVE_CONTRACT_VERSION)
        assert contract is not None
        contract_id = contract.contract_id()
        precision_proof = {
            "ok": True,
            "target": 0.70,
            "threshold": 0.70,
            "proof_schema": "effective_market_sessions_v1",
            "calib": {"support": 20, "wins": 20},
            "gate": {
                "support": 60,
                "wins": 60,
                "distinct_sessions": 60,
                "hold_bars": signal_engine.V3_LABEL_HOLD_BARS,
                "effective_support": 20,
                "effective_wins": 20,
            },
            "feature_inversions": [],
        }
        gate_proof = {
            "holdout_acc": 0.80,
            "natural_rate": 0.50,
            "no_skill_accuracy": 0.50,
            "edge": 0.30,
            "wf_fold_count": 5,
            "wf_acc_mean": 0.75,
            "wf_acc_min": 0.70,
            "wf_edge_mean": 0.25,
            "wf_edge_min": 0.20,
            "gate_brier": 0.16,
            "gate_n": 20,
            "calibrated": True,
            "calibration_status": "valid",
            "calibration_schema": "chronological_bakeoff_v1",
            "calibration_method": "sigmoid",
            "calibration_winner": "sigmoid",
            "calibration_n": 30,
            "calibration_fit_n": 18,
            "calibration_purge_n": 2,
            "calibration_selection_n": 10,
            "calibration_refit_n": 30,
            "calibration_candidates": [
                {"method": "raw_identity", "valid": True, "brier": 0.20,
                 "log_loss": 0.60, "reliability_gap": 0.10},
                {"method": "sigmoid", "valid": True, "brier": 0.16,
                 "log_loss": 0.52, "reliability_gap": 0.06},
            ],
            "conformal": {
                "ok": True,
                "samples": 10,
                "q_hat": 0.20,
                "alpha": 0.10,
            },
        }
        candidate_raw = pickle.dumps(
            {"generation": "candidate"}, protocol=pickle.HIGHEST_PROTOCOL,
        )
        candidate_payload = base64.b64encode(candidate_raw).decode("ascii")
        candidate_model_sha = hashlib.sha256(candidate_raw).hexdigest()
        artifact_sha = compute_artifact_sha(
            model_sha256=candidate_model_sha,
            contract_id=contract_id,
            direction="UP",
            policy_lineage_id="WOLF/UP",
            policy_lineage_version=1,
            feature_order=("rsi",),
            feature_schema=contract.feature_schema,
            label_schema=contract.evidence_schema,
            validation_schema=contract.validation_schema,
            hold_bars=contract.horizon_bars,
            calibration_proof=precision_proof,
            gate_proof=gate_proof,
            symbol_scope=("WOLF",),
            trained_at=proof_start,
        )
        assert artifact_sha != candidate_model_sha

        incumbent_raw = pickle.dumps(
            {"generation": "incumbent"}, protocol=pickle.HIGHEST_PROTOCOL,
        )
        incumbent_payload = base64.b64encode(incumbent_raw).decode("ascii")
        incumbent_model_sha = hashlib.sha256(incumbent_raw).hexdigest()
        incumbent_meta = {
            "tier": "proven",
            "direction": "UP",
            "model_sha256": incumbent_model_sha,
            "label_type": signal_engine.LABEL_TYPE,
            "label_schema": signal_engine._v3_label_schema(),
            "feature_schema": signal_engine._v3_feature_schema(),
            "validation_schema": signal_engine._v3_validation_schema(),
            "label_hold_bars": signal_engine.V3_LABEL_HOLD_BARS,
            "feature_cols": ["rsi"],
            "trained_at": int(time.time()),
            "accuracy": 0.72,
            "natural_rate": 0.50,
            "no_skill_accuracy": 0.50,
            "edge": 0.22,
            "wf_fold_count": 5,
            "wf_acc_mean": 0.70,
            "wf_edge_mean": 0.20,
            "precision_gate": precision_proof,
            "calibrated": True,
            "calibration_status": "valid",
            "calibration_schema": "chronological_bakeoff_v1",
            "calibration_method": "sigmoid",
            "calibration_winner": "sigmoid",
            "calibration_n": 30,
            "calibration_fit_n": 18,
            "calibration_purge_n": 2,
            "calibration_selection_n": 10,
            "calibration_refit_n": 30,
            "calibration_candidates": [
                {"method": "raw_identity", "valid": True, "brier": 0.20,
                 "log_loss": 0.60, "reliability_gap": 0.10},
                {"method": "sigmoid", "valid": True, "brier": 0.18,
                 "log_loss": 0.55, "reliability_gap": 0.08},
            ],
            "gate_n": 20,
            "gate_brier": 0.20,
            "conformal_ok": True,
            "conformal_samples": 10,
            "conformal_q_hat": 0.20,
            "conformal_alpha": 0.10,
        }
        incumbent_meta_json = json.dumps(incumbent_meta, sort_keys=True)

        monkeypatch.setenv("RESEARCH_AUTO_ACTIVATION", "1")
        monkeypatch.setenv("RESEARCH_LEASE_WINDOW_S", "86400")
        monkeypatch.setenv("V3_PRECISION_TARGET", "0.70")
        with conn.cursor() as cur:
            ensure_research_schema(cur)
            cur.execute(
                "CREATE TABLE ghost_v3_model ("
                "key TEXT PRIMARY KEY, value TEXT, updated_at BIGINT)"
            )
            cur.execute(
                "CREATE TABLE ghost_state (key TEXT PRIMARY KEY, val TEXT)"
            )
            cur.execute(
                "INSERT INTO ghost_v3_model(key,value,updated_at) VALUES(%s,%s,%s)",
                ("model_WOLF_up", incumbent_payload, int(time.time())),
            )
            cur.execute(
                "INSERT INTO ghost_v3_model(key,value,updated_at) VALUES(%s,%s,%s)",
                ("meta_WOLF_up", incumbent_meta_json, int(time.time())),
            )
            assert register_artifact(
                ArtifactMeta(
                    artifact_sha=artifact_sha,
                    contract_id=contract_id,
                    policy_lineage_id="WOLF/UP",
                    policy_lineage_version=1,
                    symbol_scope=("WOLF",),
                    output_domain=("UP",),
                    feature_schema=contract.feature_schema,
                    evidence_schema=contract.evidence_schema,
                    validation_schema=contract.validation_schema,
                    horizon_bars=contract.horizon_bars,
                    training_manifest_sha="",
                    calibration_proof=precision_proof,
                    gate_proof=gate_proof,
                    feature_order=("rsi",),
                    trained_at=proof_start,
                ),
                payload_bytes=candidate_payload,
                cur=cur,
            )
            registration_id = register_forward_experiment(
                contract_id=contract_id,
                artifact_sha=artifact_sha,
                direction="UP",
                threshold=0.70,
                symbol_universe=["WOLF"],
                round_trip_slippage_bps=10.0,
                round_trip_commission_bps=0.0,
                cur=cur,
            )
            assert registration_id is not None
            cur.execute(
                "UPDATE ghost_research_registrations SET registered_at_ts=%s "
                "WHERE registration_id=%s",
                (proof_start - 10, registration_id),
            )
            outcomes = (["WIN"] * 5 + ["LOSS"]) * 8 + ["WIN", "WIN"]
            for index, outcome in enumerate(outcomes):
                issued_ts = proof_start + index * 86400
                evaluation_date = time.strftime(
                    "%Y-%m-%d", time.gmtime(issued_ts),
                )
                assert log_research_evaluation(
                    contract_id=contract_id,
                    artifact_sha=artifact_sha,
                    symbol="WOLF",
                    direction="UP",
                    evaluation_date=evaluation_date,
                    evaluated_ts=issued_ts,
                    feature_available_ts=issued_ts,
                    calibrated_prob=0.84,
                    threshold=0.70,
                    eligible=True,
                    fired=True,
                    reason="threshold_pass",
                    cur=cur,
                )
                cur.execute(
                    """
                    INSERT INTO ghost_research_predictions
                        (contract_id,artifact_sha,policy_lineage_id,symbol,direction,
                         issued_ts,feature_available_ts,output,calibrated_prob,threshold,
                         source_snapshot_sha,feature_snapshot_sha,selector_decision,
                         context,created_at)
                    VALUES (%s,%s,'WOLF/UP','WOLF','UP',%s,%s,'UP',0.84,0.70,
                            '','',%s,%s,%s)
                    RETURNING id
                    """,
                    (
                        contract_id, artifact_sha, issued_ts, issued_ts,
                        '{"passed":true}',
                        '{"entry_price":100.0,"target_price":103.0,'
                        '"stop_price":99.0,"hold_bars":3}',
                        issued_ts,
                    ),
                )
                prediction_id = cur.fetchone()[0]
                exit_price = 103.0 if outcome == "WIN" else 99.0
                cur.execute(
                    """
                    INSERT INTO ghost_research_resolutions
                        (prediction_id,resolver_id,resolver_version,outcome,
                         observed_value,resolved_ts,evidence_available_ts,
                         evidence_payload,evidence_sha,reason,created_at)
                    VALUES (%s,'tp_sl_bar_path/v1','1.0.0',%s,%s,%s,%s,
                            %s,'','bar_path',%s)
                    """,
                    (
                        prediction_id, outcome, exit_price, issued_ts + 1,
                        issued_ts + 1, '{}', issued_ts + 1,
                    ),
                )

            proof = update_forward_proof_status(registration_id, cur=cur)
            assert proof["status"] == "PROVEN"
            activation_result = activate_artifact(
                symbol="WOLF",
                direction="UP",
                artifact_sha=artifact_sha,
                registration_id=registration_id,
                cur=cur,
            )
            assert activation_result["ok"] is True, activation_result
            assert activation_result["predecessor_sha"] == incumbent_model_sha

            @contextmanager
            def _same_connection():
                yield conn

            monkeypatch.setattr(db, "db_conn", _same_connection)
            signal_engine.invalidate_model_cache("WOLF")
            loaded, _features, activated_meta = signal_engine.load_model(
                "WOLF", "UP",
            )
            assert loaded == {"generation": "candidate"}
            assert activated_meta["model_sha256"] == candidate_model_sha
            assert activated_meta["activation_artifact_sha"] == artifact_sha
            assert signal_engine.model_serve_guard(activated_meta) is None

            lease = compute_evidence_lease(
                artifact_sha=artifact_sha,
                symbol="WOLF",
                direction="UP",
                cur=cur,
            )
            assert lease["active"] is True
            assert lease["reason"] == "lease_active_collecting"

            activated_at = int(activated_meta["activated_at"])
            forced_expiry = activated_at + 1
            activated_meta["activation_lease_expires_at"] = forced_expiry
            cur.execute(
                "UPDATE ghost_v3_model SET value=%s WHERE key='meta_WOLF_up'",
                (json.dumps(activated_meta),),
            )
            cur.execute(
                "UPDATE ghost_research_activation_log SET lease_expires_at=%s "
                "WHERE event_type='ACTIVATED' AND artifact_sha=%s",
                (forced_expiry, artifact_sha),
            )
            monkeypatch.setattr(
                activation.time, "time", lambda: float(forced_expiry + 1),
            )

            assert signal_engine.load_model("WOLF", "UP") == (None, None, None)
            rollback_result = rollback_if_degraded(
                symbol="WOLF", direction="UP", cur=cur,
            )
            assert rollback_result["ok"] is True
            assert rollback_result["reason"] == "activation_lease_expired"
            assert rollback_result["restored_artifact_sha"] == incumbent_model_sha

            signal_engine.invalidate_model_cache("WOLF")
            restored, _features, restored_meta = signal_engine.load_model(
                "WOLF", "UP",
            )
            assert restored == {"generation": "incumbent"}
            assert restored_meta == incumbent_meta
            assert signal_engine.model_serve_guard(restored_meta) is None
            assert compute_evidence_lease(
                artifact_sha=artifact_sha,
                symbol="WOLF",
                direction="UP",
                cur=cur,
            )["reason"] == "not_current_activation"

            history = get_activation_history(
                symbol="WOLF", direction="UP", cur=cur,
            )
            assert [event["event_type"] for event in history[:2]] == [
                "ROLLED_BACK", "ACTIVATED",
            ]
            cur.execute(
                "SELECT artifact_sha,predecessor_artifact_sha FROM "
                "ghost_research_activation_log WHERE event_type='ROLLED_BACK'"
            )
            assert cur.fetchone() == (artifact_sha, incumbent_model_sha)
            cur.execute(
                "SELECT val FROM ghost_state WHERE key='engine_paused'"
            )
            assert cur.fetchone() is None

            second_activation = activate_artifact(
                symbol="WOLF",
                direction="UP",
                artifact_sha=artifact_sha,
                registration_id=registration_id,
                cur=cur,
            )
            assert second_activation == {
                "ok": False,
                "reason": "artifact_activation_lease_already_used",
            }


@pytest.mark.integration
def test_proven_retrain_supersedes_active_activation_lease(monkeypatch):
    from core.research_activation import review_active_leases
    import core.db as db
    import core.signal_engine as signal_engine

    with _isolated_schema("research_activation_supersession") as (conn, _schema):
        now = int(time.time())
        activated_sha = "a" * 64
        with conn.cursor() as cur:
            ensure_research_schema(cur)
            cur.execute(
                "CREATE TABLE ghost_v3_model ("
                "key TEXT PRIMARY KEY, value TEXT, updated_at BIGINT)"
            )
            cur.execute(
                "INSERT INTO ghost_v3_model(key,value,updated_at) VALUES(%s,%s,%s)",
                ("model_WOLF_up", "activated-payload", now),
            )
            cur.execute(
                "INSERT INTO ghost_v3_model(key,value,updated_at) VALUES(%s,%s,%s)",
                (
                    "meta_WOLF_up",
                    json.dumps({
                        "tier": "proven",
                        "activation_artifact_sha": activated_sha,
                        "activation_proof": {"status": "PROVEN"},
                    }),
                    now,
                ),
            )
            cur.execute(
                """INSERT INTO ghost_research_activation_log
                   (event_type,symbol,direction,artifact_sha,lease_expires_at,created_at)
                   VALUES ('ACTIVATED','WOLF','UP',%s,%s,%s)""",
                (activated_sha, now + 86400, now),
            )
            cur.execute(
                """INSERT INTO ghost_research_activation_predecessors
                   (symbol,direction,artifact_sha,model_bytes,meta_json,stored_at)
                   VALUES ('WOLF','UP',%s,%s,%s,%s)""",
                ("b" * 64, "old-model", '{"tier":"proven"}', now),
            )
        conn.commit()

        @contextmanager
        def _same_connection():
            yield conn

        monkeypatch.setattr(db, "db_conn", _same_connection)
        monkeypatch.setattr(
            "core.precision_gate.invalidate_global_threshold_cache", lambda: None,
        )
        monkeypatch.setattr(
            "core.precision_gate.invalidate_global_threshold_persistent",
            lambda cur: None,
        )

        assert signal_engine._store_direction_model(
            "WOLF", "UP", "retrained-payload",
            json.dumps({"tier": "proven", "trained_at": now}),
        ) is True

        with conn.cursor() as cur:
            cur.execute(
                """SELECT event_type,artifact_sha,reason
                   FROM ghost_research_activation_log
                   WHERE symbol='WOLF' AND direction='UP'
                   ORDER BY id DESC LIMIT 1"""
            )
            assert cur.fetchone() == (
                "SUPERSEDED", activated_sha, "ordinary_proven_model_retrained",
            )
            cur.execute(
                """SELECT COUNT(*) FROM ghost_research_activation_predecessors
                   WHERE symbol='WOLF' AND direction='UP'"""
            )
            assert cur.fetchone()[0] == 0
        assert review_active_leases()["reviewed"] == 0


@pytest.mark.integration
def test_prediction_resolution_outbox_lifecycle_is_fail_closed(monkeypatch):
    with _isolated_schema("research_outbox") as (conn, _schema):
        issued_ts = 1_720_000_000
        with conn.cursor() as cur:
            ensure_research_schema(cur)
            prediction_id = log_research_prediction(
                contract_id="tp-sl-contract",
                artifact_sha="d" * 64,
                policy_lineage_id="WOLF/UP",
                symbol="WOLF",
                direction="UP",
                issued_ts=issued_ts,
                feature_available_ts=issued_ts,
                output="UP",
                calibrated_prob=0.84,
                threshold=0.70,
                context={
                    "asset_type": "stock",
                    "entry_price": 100.0,
                    "target_price": 103.0,
                    "stop_price": 99.0,
                    "hold_bars": 1,
                },
                cur=cur,
            )
            assert prediction_id is not None
            pending = get_pending_predictions(cur=cur)
            assert len(pending) == 1
            bars = [{
                "ts": issued_ts + 86400,
                "open": 100.0,
                "high": 104.0,
                "low": 100.0,
                "close": 103.0,
            }]
            resolution = resolve_pending_tp_sl_prediction(
                pending[0], daily_bars=bars, now=issued_ts + 2 * 86400,
            )
            assert resolution is not None
            assert resolution.outcome == "WIN"
            assert resolve_research_prediction(
                prediction_id=prediction_id,
                resolver_id="tp_sl_bar_path/v1",
                resolver_version="1.0.0",
                outcome=resolution.outcome,
                observed_value=resolution.observed_value,
                resolved_ts=resolution.resolved_ts,
                evidence_available_ts=resolution.available_ts,
                evidence_payload=resolution.evidence,
                reason=resolution.reason,
                cur=cur,
            )
            assert not resolve_research_prediction(
                prediction_id=prediction_id,
                resolver_id="tp_sl_bar_path/v1",
                resolver_version="1.0.0",
                outcome="LOSS",
                resolved_ts=resolution.resolved_ts,
                evidence_available_ts=resolution.available_ts,
                cur=cur,
            )
            assert get_pending_predictions(cur=cur) == []
            outbox = get_outbox_pending(cur=cur)
            assert len(outbox) == 1
            assert outbox[0]["prediction_id"] == prediction_id
            assert mark_outbox_processed(outbox[0]["id"], cur=cur)
            assert get_outbox_pending(cur=cur) == []
            cur.execute(
                "SELECT count(*) FROM ghost_research_resolutions "
                "WHERE prediction_id=%s",
                (prediction_id,),
            )
            assert cur.fetchone()[0] == 1

        from core.research_activation import auto_activation_enabled

        monkeypatch.setenv("RESEARCH_AUTO_ACTIVATION", "0")
        assert auto_activation_enabled() is False
