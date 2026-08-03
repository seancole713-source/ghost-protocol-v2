"""core/research_schema.py — single research schema owner.

All research tables are created here in dependency order. Called additively
from core.db._migrate_schema() at startup. Never called from GET handlers.

Tables (dependency order):
  1. ghost_research_contracts       — canonical frozen contract snapshots
  2. ghost_research_datasets        — point-in-time dataset manifests
  3. ghost_research_dataset_samples — individual samples in a manifest
  4. ghost_research_artifacts       — immutable candidate model registry
  5. ghost_research_artifact_events — append-only lifecycle events
  6. ghost_research_predictions     — isolated prediction evidence
  7. ghost_research_resolutions     — resolution evidence (FK → predictions)
  8. ghost_research_outbox          — post-resolution processing queue
  9. ghost_research_registrations   — immutable forward experiment registrations
  10. ghost_research_promotions     — immutable champion/challenger reviews
  11. ghost_research_promotion_gates — per-gate evaluation rows
  12. ghost_research_activation_log — activation, lease, and rollback events
"""
from __future__ import annotations

import logging

LOGGER = logging.getLogger("ghost.research_schema")

RESEARCH_SCHEMA_VERSION = 1


def ensure_research_schema(cur) -> None:
    """Create all research tables if they don't exist. Idempotent and additive.

    Called from core.db._migrate_schema() at startup. Never called from
    GET handlers or per-request paths.
    """
    _create_contracts(cur)
    _create_datasets(cur)
    _create_artifacts(cur)
    _create_predictions(cur)
    _create_resolutions(cur)
    _create_outbox(cur)
    _create_registrations(cur)
    _create_promotions(cur)
    _create_activation_log(cur)
    LOGGER.info("Research schema v%s ensured", RESEARCH_SCHEMA_VERSION)


# ── 1. Contracts ────────────────────────────────────────────────────────────

def _create_contracts(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_research_contracts (
            id SERIAL PRIMARY KEY,
            contract_sha TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            version TEXT NOT NULL,
            label_type TEXT NOT NULL,
            live_compatible BOOLEAN NOT NULL DEFAULT FALSE,
            definition JSONB NOT NULL,
            created_at BIGINT NOT NULL,
            UNIQUE (name, version)
        )
    """)


# ── 2. Datasets ─────────────────────────────────────────────────────────────

def _create_datasets(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_research_datasets (
            id SERIAL PRIMARY KEY,
            manifest_sha TEXT NOT NULL UNIQUE,
            contract_sha TEXT NOT NULL REFERENCES ghost_research_contracts(contract_sha),
            source_ids TEXT[] NOT NULL,
            universe TEXT[] NOT NULL,
            date_start TEXT NOT NULL,
            date_end TEXT NOT NULL,
            sample_count INT NOT NULL,
            feature_names TEXT[] NOT NULL,
            code_revision TEXT NOT NULL,
            metadata JSONB NOT NULL DEFAULT '{}',
            created_at BIGINT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_research_dataset_samples (
            id SERIAL PRIMARY KEY,
            manifest_sha TEXT NOT NULL REFERENCES ghost_research_datasets(manifest_sha),
            sample_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            available_ts BIGINT NOT NULL,
            event_ts BIGINT NOT NULL,
            label INT,
            content_digest TEXT,
            UNIQUE (manifest_sha, sample_id)
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_research_samples_manifest
        ON ghost_research_dataset_samples (manifest_sha)
    """)


# ── 3. Artifacts ─────────────────────────────────────────────────────────────

def _create_artifacts(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_research_artifacts (
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
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_research_artifact_events (
            id SERIAL PRIMARY KEY,
            artifact_sha TEXT NOT NULL REFERENCES ghost_research_artifacts(artifact_sha),
            event_type TEXT NOT NULL,
            event_payload JSONB NOT NULL DEFAULT '{}',
            created_at BIGINT NOT NULL
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_research_artifact_events_sha
        ON ghost_research_artifact_events (artifact_sha, created_at DESC)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_research_artifacts_contract
        ON ghost_research_artifacts (contract_sha, direction)
    """)


# ── 4. Predictions ───────────────────────────────────────────────────────────

def _create_predictions(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_research_predictions (
            id SERIAL PRIMARY KEY,
            contract_sha TEXT NOT NULL REFERENCES ghost_research_contracts(contract_sha),
            artifact_sha TEXT NOT NULL REFERENCES ghost_research_artifacts(artifact_sha),
            policy_lineage_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            issued_ts BIGINT NOT NULL,
            feature_available_ts BIGINT NOT NULL,
            output TEXT NOT NULL,
            calibrated_prob DOUBLE PRECISION,
            threshold DOUBLE PRECISION,
            source_snapshot_sha TEXT,
            feature_snapshot_sha TEXT,
            selector_decision JSONB,
            context JSONB,
            created_at BIGINT NOT NULL,
            UNIQUE (contract_sha, artifact_sha, symbol, issued_ts)
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_research_pred_contract_artifact
        ON ghost_research_predictions (contract_sha, artifact_sha, issued_ts DESC)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_research_pred_symbol_time
        ON ghost_research_predictions (symbol, issued_ts DESC)
    """)
    # Pending predictions: those without a resolution. Use NOT EXISTS
    # anti-join instead of a partial index with a subquery (PostgreSQL
    # does not allow subqueries in WHERE clauses of indexes).
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_research_pred_id
        ON ghost_research_predictions (id)
    """)


# ── 5. Resolutions ───────────────────────────────────────────────────────────

def _create_resolutions(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_research_resolutions (
            id SERIAL PRIMARY KEY,
            prediction_id INT NOT NULL UNIQUE
                REFERENCES ghost_research_predictions(id),
            resolver_id TEXT NOT NULL,
            resolver_version TEXT NOT NULL,
            outcome TEXT NOT NULL,
            observed_value DOUBLE PRECISION,
            resolved_ts BIGINT NOT NULL,
            evidence_available_ts BIGINT NOT NULL,
            evidence_payload JSONB,
            evidence_sha TEXT,
            reason TEXT DEFAULT '',
            created_at BIGINT NOT NULL
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_research_res_artifact_outcome
        ON ghost_research_resolutions (prediction_id, outcome)
    """)


# ── 6. Outbox ────────────────────────────────────────────────────────────────

def _create_outbox(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_research_outbox (
            id SERIAL PRIMARY KEY,
            prediction_id INT NOT NULL UNIQUE
                REFERENCES ghost_research_predictions(id),
            resolution_id INT NOT NULL
                REFERENCES ghost_research_resolutions(id),
            status TEXT NOT NULL DEFAULT 'PENDING',
            attempt_count INT NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at BIGINT NOT NULL,
            processed_at BIGINT
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_research_outbox_pending
        ON ghost_research_outbox (status, created_at)
        WHERE status = 'PENDING'
    """)


# ── 7. Registrations ─────────────────────────────────────────────────────────

def _create_registrations(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_research_registrations (
            id SERIAL PRIMARY KEY,
            registration_id TEXT NOT NULL UNIQUE,
            contract_sha TEXT NOT NULL REFERENCES ghost_research_contracts(contract_sha),
            artifact_sha TEXT NOT NULL REFERENCES ghost_research_artifacts(artifact_sha),
            direction TEXT NOT NULL,
            threshold DOUBLE PRECISION NOT NULL,
            output_rule TEXT NOT NULL,
            symbol_universe TEXT[] NOT NULL,
            slice_spec JSONB,
            source_manifest_sha TEXT,
            feature_manifest_sha TEXT,
            resolver_id TEXT NOT NULL,
            confirmatory_n INT NOT NULL DEFAULT 50,
            max_calendar_days INT NOT NULL DEFAULT 120,
            min_issuance_dates INT NOT NULL DEFAULT 20,
            max_symbol_concentration DOUBLE PRECISION NOT NULL DEFAULT 0.20,
            family_size INT,
            family_correction TEXT,
            selection_evidence JSONB,
            status TEXT NOT NULL DEFAULT 'COLLECTING',
            registered_at_ts BIGINT NOT NULL,
            closed_at_ts BIGINT,
            metadata JSONB NOT NULL DEFAULT '{}'
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_research_reg_status
        ON ghost_research_registrations (status, registered_at_ts DESC)
    """)


# ── 8. Promotions ────────────────────────────────────────────────────────────

def _create_promotions(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_research_promotions (
            id SERIAL PRIMARY KEY,
            review_id TEXT NOT NULL UNIQUE,
            registration_id TEXT NOT NULL
                REFERENCES ghost_research_registrations(registration_id),
            artifact_sha TEXT NOT NULL REFERENCES ghost_research_artifacts(artifact_sha),
            champion_artifact_sha TEXT REFERENCES ghost_research_artifacts(artifact_sha),
            direction TEXT NOT NULL,
            symbol_scope TEXT[] NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT,
            reviewed_at_ts BIGINT NOT NULL,
            metadata JSONB NOT NULL DEFAULT '{}'
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_research_promotion_gates (
            id SERIAL PRIMARY KEY,
            review_id TEXT NOT NULL
                REFERENCES ghost_research_promotions(review_id),
            gate_name TEXT NOT NULL,
            gate_result JSONB NOT NULL,
            passed BOOLEAN NOT NULL
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_research_promo_gates_review
        ON ghost_research_promotion_gates (review_id)
    """)


# ── 9. Activation Log ────────────────────────────────────────────────────────

def _create_activation_log(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_research_activation_log (
            id SERIAL PRIMARY KEY,
            event_type TEXT NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            artifact_sha TEXT NOT NULL REFERENCES ghost_research_artifacts(artifact_sha),
            registration_id TEXT,
            review_id TEXT,
            predecessor_artifact_sha TEXT,
            lease_expires_at BIGINT,
            proof_snapshot JSONB,
            reason TEXT,
            created_at BIGINT NOT NULL
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_research_activation_symbol_dir
        ON ghost_research_activation_log (symbol, direction, created_at DESC)
    """)
