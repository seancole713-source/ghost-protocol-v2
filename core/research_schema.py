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

RESEARCH_SCHEMA_VERSION = 2


def ensure_research_schema(cur) -> None:
    """Create all research tables if they don't exist. Idempotent and additive.

    Called from core.db._migrate_schema() at startup. Never called from
    GET handlers or per-request paths.
    """
    # Contracts and datasets retain their original schema. Creating them first
    # gives a clean database the FK roots needed by the remaining tables.
    _create_contracts(cur)
    _create_datasets(cur)
    # Upgrade legacy artifact/prediction/registration tables before the latest
    # CREATE INDEX statements reference canonical column names.
    _migrate_research_schema(cur)
    _create_artifacts(cur)
    _create_evaluations(cur)
    _create_predictions(cur)
    _create_resolutions(cur)
    _create_outbox(cur)
    _create_registrations(cur)
    _create_promotions(cur)
    _create_activation_log(cur)
    _create_activation_predecessors(cur)
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
            artifact_sha TEXT PRIMARY KEY,
            model_sha256 TEXT NOT NULL DEFAULT '',
            contract_id TEXT NOT NULL,
            policy_lineage_id TEXT NOT NULL DEFAULT '',
            policy_lineage_version INT NOT NULL DEFAULT 1,
            symbol_scope TEXT NOT NULL DEFAULT '[]',
            output_domain TEXT NOT NULL DEFAULT '[]',
            feature_schema TEXT NOT NULL DEFAULT '',
            evidence_schema TEXT NOT NULL DEFAULT '',
            validation_schema TEXT NOT NULL DEFAULT '',
            horizon_bars INT NOT NULL DEFAULT 3,
            training_manifest_sha TEXT NOT NULL DEFAULT '',
            calibration_proof JSONB,
            gate_proof JSONB,
            feature_order TEXT NOT NULL DEFAULT '[]',
            payload_bytes TEXT,
            created_at BIGINT NOT NULL,
            trained_at BIGINT NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            retired_at BIGINT NOT NULL DEFAULT 0,
            retirement_reason TEXT DEFAULT ''
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_research_artifact_events (
            id SERIAL PRIMARY KEY,
            artifact_sha TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_ts BIGINT NOT NULL,
            reason TEXT DEFAULT '',
            metadata JSONB,
            created_at BIGINT NOT NULL
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_artifacts_contract "
        "ON ghost_research_artifacts (contract_id, status)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_artifacts_lineage "
        "ON ghost_research_artifacts (policy_lineage_id, policy_lineage_version DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_artifact_events_sha "
        "ON ghost_research_artifact_events (artifact_sha, event_ts DESC)"
    )


# ── 4. Predictions ───────────────────────────────────────────────────────────

def _create_evaluations(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_research_evaluations (
            id SERIAL PRIMARY KEY,
            contract_id TEXT NOT NULL,
            artifact_sha TEXT NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            evaluation_date TEXT NOT NULL,
            evaluated_ts BIGINT NOT NULL,
            feature_available_ts BIGINT NOT NULL,
            calibrated_prob DOUBLE PRECISION NOT NULL,
            threshold DOUBLE PRECISION NOT NULL,
            eligible BOOLEAN NOT NULL,
            fired BOOLEAN NOT NULL,
            reason TEXT NOT NULL,
            metadata JSONB NOT NULL DEFAULT '{}',
            created_at BIGINT NOT NULL,
            UNIQUE (contract_id, artifact_sha, symbol, evaluation_date)
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_research_eval_artifact_time
        ON ghost_research_evaluations (contract_id, artifact_sha, evaluated_ts)
    """)


# ── 5. Predictions ───────────────────────────────────────────────────────────

def _create_predictions(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_research_predictions (
            id SERIAL PRIMARY KEY,
            contract_id TEXT NOT NULL,
            artifact_sha TEXT NOT NULL,
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
            UNIQUE (contract_id, artifact_sha, symbol, issued_ts)
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_research_pred_contract_artifact
        ON ghost_research_predictions (contract_id, artifact_sha, issued_ts DESC)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_research_pred_symbol_time
        ON ghost_research_predictions (symbol, issued_ts DESC)
    """)
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
            contract_id TEXT NOT NULL,
            artifact_sha TEXT NOT NULL,
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
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_research_reg_artifact_once_v2
        ON ghost_research_registrations (contract_id, artifact_sha, direction)
        WHERE metadata->>'registration_schema' = 'fixed50/v2'
    """)


# ── 8. Promotions ────────────────────────────────────────────────────────────

def _create_promotions(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_research_promotions (
            id SERIAL PRIMARY KEY,
            review_id TEXT NOT NULL UNIQUE,
            registration_id TEXT NOT NULL,
            artifact_sha TEXT NOT NULL,
            champion_artifact_sha TEXT,
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
            review_id TEXT NOT NULL,
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
            artifact_sha TEXT NOT NULL,
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


# ── 10. Activation Predecessors ─────────────────────────────────────────────

def _create_activation_predecessors(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_research_activation_predecessors (
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            artifact_sha TEXT NOT NULL,
            model_bytes TEXT NOT NULL,
            meta_json TEXT NOT NULL,
            stored_at BIGINT NOT NULL,
            UNIQUE (symbol, direction)
        )
    """)


# ── Schema migration (upgrade from old column names) ────────────────────────

def _migrate_research_schema(cur) -> None:
    """Migrate existing research tables from old column names to current schema.

    CREATE TABLE IF NOT EXISTS cannot rename columns or add missing ones.
    This function handles upgrades from the pre-Phase-7 schema where:
      - contract_sha was used instead of contract_id
      - model_sha256 was missing from artifacts
      - Many new columns were added (status, policy_lineage_id, etc.)
      - activation_predecessors table did not exist
    All statements are idempotent. Each table upgrade uses a savepoint so a
    failed optional migration cannot leave the startup transaction aborted.
    """
    def migrate_artifacts() -> None:
        if not _table_exists(cur, "ghost_research_artifacts"):
            return
        cur.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema=current_schema()"
            " AND table_name='ghost_research_artifacts' AND column_name='contract_sha'"
        )
        if cur.fetchone():
            # Old schema detected — rename contract_sha → contract_id
            cur.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema=current_schema()"
                " AND table_name='ghost_research_artifacts' AND column_name='contract_id'"
            )
            if not cur.fetchone():
                cur.execute("ALTER TABLE ghost_research_artifacts RENAME COLUMN contract_sha TO contract_id")
                LOGGER.info("Research schema: renamed artifacts.contract_sha → contract_id")

        _add_column_if_missing(cur, "ghost_research_artifacts", "model_sha256", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(cur, "ghost_research_artifacts", "policy_lineage_id", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(cur, "ghost_research_artifacts", "policy_lineage_version", "INT NOT NULL DEFAULT 1")
        _add_column_if_missing(cur, "ghost_research_artifacts", "symbol_scope", "TEXT NOT NULL DEFAULT '[]'")
        _add_column_if_missing(cur, "ghost_research_artifacts", "output_domain", "TEXT NOT NULL DEFAULT '[]'")
        _add_column_if_missing(cur, "ghost_research_artifacts", "feature_schema", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(cur, "ghost_research_artifacts", "evidence_schema", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(cur, "ghost_research_artifacts", "horizon_bars", "INT NOT NULL DEFAULT 3")
        _add_column_if_missing(cur, "ghost_research_artifacts", "feature_order", "TEXT NOT NULL DEFAULT '[]'")
        _add_column_if_missing(cur, "ghost_research_artifacts", "payload_bytes", "TEXT")
        _add_column_if_missing(cur, "ghost_research_artifacts", "trained_at", "BIGINT NOT NULL DEFAULT 0")
        _add_column_if_missing(cur, "ghost_research_artifacts", "status", "TEXT NOT NULL DEFAULT 'ACTIVE'")
        _add_column_if_missing(cur, "ghost_research_artifacts", "retired_at", "BIGINT NOT NULL DEFAULT 0")
        _add_column_if_missing(cur, "ghost_research_artifacts", "retirement_reason", "TEXT DEFAULT ''")
        columns = _table_columns(cur, "ghost_research_artifacts")
        if "direction" in columns:
            cur.execute(
                "UPDATE ghost_research_artifacts SET output_domain = to_json(ARRAY[direction])::text"
                " WHERE output_domain = '[]' AND direction IN ('UP', 'DOWN')"
            )
            cur.execute("ALTER TABLE ghost_research_artifacts ALTER COLUMN direction DROP NOT NULL")
        if "lineage_version" in columns:
            cur.execute(
                "UPDATE ghost_research_artifacts SET policy_lineage_version ="
                " CASE WHEN lineage_version ~ '^[0-9]+$' THEN GREATEST(1, lineage_version::int) ELSE 1 END"
            )
            cur.execute("ALTER TABLE ghost_research_artifacts ALTER COLUMN lineage_version DROP NOT NULL")
        if "feature_names" in columns:
            cur.execute(
                "UPDATE ghost_research_artifacts SET feature_order = to_json(feature_names)::text"
                " WHERE feature_order = '[]'"
            )
            cur.execute("ALTER TABLE ghost_research_artifacts ALTER COLUMN feature_names DROP NOT NULL")
        if "label_schema" in columns:
            cur.execute(
                "UPDATE ghost_research_artifacts SET evidence_schema = label_schema"
                " WHERE evidence_schema = ''"
            )
            cur.execute("ALTER TABLE ghost_research_artifacts ALTER COLUMN label_schema DROP NOT NULL")
        if "hold_bars" in columns:
            cur.execute(
                "UPDATE ghost_research_artifacts SET horizon_bars = hold_bars"
                " WHERE hold_bars > 0"
            )
            cur.execute("ALTER TABLE ghost_research_artifacts ALTER COLUMN hold_bars DROP NOT NULL")
        if "training_manifest_sha" in columns:
            cur.execute(
                "UPDATE ghost_research_artifacts SET training_manifest_sha = ''"
                " WHERE training_manifest_sha IS NULL"
            )
            cur.execute(
                "ALTER TABLE ghost_research_artifacts ALTER COLUMN training_manifest_sha SET DEFAULT ''"
            )
            cur.execute(
                "ALTER TABLE ghost_research_artifacts ALTER COLUMN training_manifest_sha SET NOT NULL"
            )
        # The legacy index has this name but targets (contract_id, direction).
        # Drop it so _create_artifacts can build the canonical status index.
        cur.execute("DROP INDEX IF EXISTS idx_research_artifacts_contract")
        LOGGER.info("Research schema: upgraded artifact columns")

    def migrate_artifact_events() -> None:
        if not _table_exists(cur, "ghost_research_artifact_events"):
            return
        _add_column_if_missing(cur, "ghost_research_artifact_events", "event_ts", "BIGINT NOT NULL DEFAULT 0")
        _add_column_if_missing(cur, "ghost_research_artifact_events", "reason", "TEXT DEFAULT ''")
        _add_column_if_missing(cur, "ghost_research_artifact_events", "metadata", "JSONB")
        columns = _table_columns(cur, "ghost_research_artifact_events")
        if "event_payload" in columns:
            cur.execute(
                "UPDATE ghost_research_artifact_events SET metadata = event_payload"
                " WHERE metadata IS NULL"
            )
        if "created_at" in columns:
            cur.execute(
                "UPDATE ghost_research_artifact_events SET event_ts = created_at"
                " WHERE event_ts = 0"
            )
            # The legacy index orders by created_at rather than immutable event_ts.
            cur.execute("DROP INDEX IF EXISTS idx_research_artifact_events_sha")

    def migrate_predictions() -> None:
        if not _table_exists(cur, "ghost_research_predictions"):
            return
        _rename_column_if_needed(
            cur, "ghost_research_predictions", "contract_sha", "contract_id",
        )

    def migrate_registrations() -> None:
        if not _table_exists(cur, "ghost_research_registrations"):
            return
        _rename_column_if_needed(
            cur, "ghost_research_registrations", "contract_sha", "contract_id",
        )

    _run_migration_step(cur, "artifacts", migrate_artifacts)
    _run_migration_step(cur, "artifact_events", migrate_artifact_events)
    _run_migration_step(cur, "predictions", migrate_predictions)
    _run_migration_step(cur, "registrations", migrate_registrations)


def _run_migration_step(cur, name: str, fn) -> None:
    """Run one optional upgrade without poisoning the surrounding transaction."""
    cur.execute("SAVEPOINT research_schema_step")
    try:
        fn()
    except Exception as exc:
        cur.execute("ROLLBACK TO SAVEPOINT research_schema_step")
        LOGGER.warning("Research schema %s migration: %s", name, str(exc)[:120])
    finally:
        cur.execute("RELEASE SAVEPOINT research_schema_step")


def _table_exists(cur, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (table,))
    row = cur.fetchone()
    return bool(row and row[0])


def _table_columns(cur, table: str) -> set[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema=current_schema() AND table_name=%s",
        (table,),
    )
    return {str(row[0]) for row in cur.fetchall()}


def _rename_column_if_needed(cur, table: str, old: str, new: str) -> None:
    columns = _table_columns(cur, table)
    if old in columns and new not in columns:
        cur.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
        LOGGER.info("Research schema: renamed %s.%s to %s", table, old, new)


def _add_column_if_missing(cur, table: str, column: str, col_type: str) -> None:
    """Add a column when its table exists and the column does not."""
    if not _table_exists(cur, table):
        return
    columns = _table_columns(cur, table)
    if column not in columns:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
