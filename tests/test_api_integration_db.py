import logging
import time
import uuid

import pytest
from fastapi.testclient import TestClient

import wolf_app
from core import db as core_db


def _seed_prediction(symbol, outcome, predicted_at, resolved_at=None, expires_at=None):
    with core_db.db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO predictions
            (symbol, direction, confidence, entry_price, target_price, stop_price,
             run_at, predicted_at, expires_at, resolved_at, outcome, exit_price, pnl_pct, asset_type)
            VALUES (%s, 'BUY', 0.90, 100.0, 103.0, 97.0, %s, %s, %s, %s, %s, 101.0, 1.0, 'stock')
            """,
            (
                symbol,
                predicted_at,
                predicted_at,
                expires_at if expires_at is not None else predicted_at + 3600,
                resolved_at,
                outcome,
            ),
        )


def _delete_seeded(symbol_prefix):
    with core_db.db_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM predictions WHERE symbol LIKE %s", (f"{symbol_prefix}%",))
        cur.execute("DELETE FROM user_portfolio WHERE symbol LIKE %s", (f"{symbol_prefix}%",))


@pytest.mark.integration
def test_integration_stats_deltas_after_seed(integration_db):
    prefix = f"TST{uuid.uuid4().hex[:6].upper()}"
    now = int(time.time())

    with TestClient(wolf_app.APP) as client:
        baseline = client.get("/api/stats").json()
        _seed_prediction(prefix + "W", "WIN", predicted_at=now - 3000, resolved_at=now - 2000, expires_at=now + 7200)
        _seed_prediction(prefix + "L", "LOSS", predicted_at=now - 2500, resolved_at=now - 1500, expires_at=now + 7200)
        _seed_prediction(prefix + "O", None, predicted_at=now - 500, resolved_at=None, expires_at=now + 7200)
        after = client.get("/api/stats").json()

    try:
        assert after["wins"] == baseline["wins"] + 1
        assert after["losses"] == baseline["losses"] + 1
        assert after["open_positions"] == baseline["open_positions"] + 1
    finally:
        _delete_seeded(prefix)


@pytest.mark.integration
def test_integration_portfolio_crud_flow(integration_db):
    symbol = f"TST{uuid.uuid4().hex[:6].upper()}"
    payload = {
        "symbol": symbol,
        "asset_type": "stock",
        "quantity": 5,
        "buy_price": 10.5,
        "buy_date": "2026-04-01",
        "notes": "integration test",
    }

    with TestClient(wolf_app.APP) as client:
        headers = {"X-Ghost-Mcp-Token": "itest-token"}
        created = client.post("/api/portfolio", json=payload, headers=headers)
        assert created.status_code == 200
        created_body = created.json()
        assert created_body["ok"] is True
        pos_id = created_body["id"]

        listed = client.get("/api/portfolio", headers=headers)
        assert listed.status_code == 200
        listed_body = listed.json()
        assert any(p["id"] == pos_id and p["symbol"] == symbol for p in listed_body["positions"])

        deleted = client.delete(f"/api/portfolio/{pos_id}", headers=headers)
        assert deleted.status_code == 200
        assert deleted.json()["ok"] is True

    _delete_seeded("TST")


@pytest.mark.integration
def test_integration_cockpit_context_matches_stats_and_activity(integration_db):
    prefix = f"TST{uuid.uuid4().hex[:6].upper()}"
    now = int(time.time())

    # Seed one WIN, one LOSS, one open prediction for deterministic deltas.
    _seed_prediction(prefix + "W", "WIN", predicted_at=now - 2400, resolved_at=now - 1800, expires_at=now + 7200)
    _seed_prediction(prefix + "L", "LOSS", predicted_at=now - 2200, resolved_at=now - 1600, expires_at=now + 7200)
    _seed_prediction(prefix + "O", None, predicted_at=now - 300, resolved_at=None, expires_at=now + 7200)

    try:
        # Avoid stale cache in repeated test runs.
        wolf_app._bump_cockpit_db_cache()

        with TestClient(wolf_app.APP) as client:
            stats = client.get("/api/stats")
            ctx = client.get("/api/cockpit/context")

        assert stats.status_code == 200
        assert ctx.status_code == 200

        stats_body = stats.json()
        ctx_body = ctx.json()

        assert ctx_body["ok"] is True
        assert ctx_body["stats"]["wins"] == stats_body["wins"]
        assert ctx_body["stats"]["losses"] == stats_body["losses"]
        assert ctx_body["stats"]["open_positions"] == stats_body["open_positions"]
        assert ctx_body["activity"]["open_predictions"] >= 1
    finally:
        _delete_seeded(prefix)


@pytest.mark.integration
def test_clean_database_treats_absent_legacy_outcomes_as_zero(integration_db):
    from core.prediction import (
        _legacy_signal,
        _objective_symbol_stats,
        get_objective_status,
    )

    with core_db.db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('ghost_prediction_outcomes')")
        assert cur.fetchone()[0] is None

    symbol_stats = _objective_symbol_stats("WOLF", "UP")
    objective = get_objective_status()

    assert symbol_stats["gpo_total"] == 0
    assert symbol_stats["gpo_wins"] == 0
    assert objective["legacy_recent"]["total"] == 0
    assert _legacy_signal("WOLF", 100.0) is None


@pytest.mark.integration
def test_clean_database_migration_has_no_missing_legacy_column_warning(
    integration_db,
    caplog,
):
    with core_db.db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema=current_schema()
              AND table_name='predictions'
              AND column_name IN ('method', 'horizon_h')
            """
        )
        assert cur.fetchall() == []

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="ghost.db"):
        core_db._migrate_schema()

    assert not [
        record.message for record in caplog.records
        if record.name == "ghost.db" and record.message.startswith("Migration:")
    ]


@pytest.mark.integration
def test_present_legacy_outcomes_count_hit_direction_as_win(integration_db):
    from core.prediction import _objective_symbol_stats, get_objective_status

    with core_db.db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE ghost_prediction_outcomes (
                id SERIAL PRIMARY KEY,
                symbol TEXT NOT NULL,
                predicted_direction TEXT NOT NULL,
                hit_direction INTEGER,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            INSERT INTO ghost_prediction_outcomes
                (symbol, predicted_direction, hit_direction)
            VALUES ('WOLF', 'UP', 1), ('WOLF', 'UP', 0)
            """
        )

    try:
        symbol_stats = _objective_symbol_stats("WOLF", "UP")
        objective = get_objective_status()

        assert (symbol_stats["gpo_wins"], symbol_stats["gpo_total"]) == (1, 2)
        assert objective["legacy_recent"] == {
            "wins": 1,
            "losses": 1,
            "total": 2,
            "win_rate_pct": 50.0,
        }
    finally:
        with core_db.db_conn() as conn:
            cur = conn.cursor()
            cur.execute("DROP TABLE IF EXISTS ghost_prediction_outcomes")
