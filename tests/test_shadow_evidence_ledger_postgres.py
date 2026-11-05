"""Candidate-specific PostgreSQL checks; only the disposable local CI database.

Override the root session bootstrap so its init_db cannot run before our target
checks. Each test owns a random schema; no shared tables are truncated. These
checks do not certify concurrent writers or change the existing retry policy.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import ipaddress
import os
import uuid

import psycopg2
from psycopg2 import sql
from psycopg2.extras import Json
import pytest

from core import shadow_evidence_ledger as ledger

pytestmark = pytest.mark.integration


def _disposable_backend(name, address, params, environ):
    """A loopback DSN can terminate at a private Docker service in GitHub CI."""
    if name != "ghost_ci_test" or not address:
        return False
    try:
        server = ipaddress.ip_address(address)
    except ValueError:
        return False
    if server.is_loopback:
        return True
    # Only the known GitHub integration job may use a bridged backend. Keep
    # loopback-only DSN validation above; never allow a remote DSN/private host.
    known_ci = (
        environ.get("GITHUB_ACTIONS") == "true"
        and environ.get("GITHUB_REPOSITORY") == "seancole713-source/ghost-protocol-v2"
        and environ.get("GITHUB_JOB") == "integration"
        and params.get("user") == "ghost_ci"
        and params.get("port", "5432") == "5432"
    )
    return known_ci and server.version == 4 and any(
        server in ipaddress.ip_network(network)
        for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    )


@pytest.fixture(scope="session")
def _integration_database_session():
    """Fail closed before any DDL, including the inherited pytest bootstrap."""
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn or os.getenv("GHOST_INTEGRATION_TESTS") != "1":
        pytest.skip("requires TEST_DATABASE_URL and GHOST_INTEGRATION_TESTS=1")
    params = psycopg2.extensions.parse_dsn(dsn)
    # Deliberately match the established PostgreSQL CI service. No fallback to
    # DATABASE_URL, service files, remote hosts, or a developer's default DB.
    if (params.get("dbname") != "ghost_ci_test"
            or params.get("host") not in {"127.0.0.1", "localhost", "::1"}
            or any(key in params for key in ("service", "hostaddr", "options"))):
        pytest.fail("requires the disposable loopback ghost_ci_test database")
    with closing(psycopg2.connect(dsn, connect_timeout=3)) as conn, conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), host(inet_server_addr())")
            name, address = cur.fetchone()
            if not _disposable_backend(name, address, params, os.environ):
                pytest.fail("database identity is not the disposable local CI target")
    yield dsn


@pytest.fixture
def pg_ledger(_integration_database_session, monkeypatch):
    dsn = _integration_database_session
    schema = "ghost_score_test_" + uuid.uuid4().hex

    @contextmanager
    def connection():
        conn = psycopg2.connect(dsn, connect_timeout=3)
        try:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("SET search_path TO {}, pg_catalog").format(sql.Identifier(schema)))
                cur.execute("SET statement_timeout='5s'")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    with closing(psycopg2.connect(dsn, connect_timeout=3)) as conn, conn:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        import core.db as core_db
        monkeypatch.setattr(core_db, "db_conn", connection)
        monkeypatch.setattr(ledger, "_now", lambda: 200)
        with connection() as conn:
            with conn.cursor() as cur:
                # Only the input columns read by this ledger. The score table
                # and indexes below use actual production DDL, never a mock.
                cur.execute("CREATE TABLE ghost_agent_tasks (task_id TEXT PRIMARY KEY, status TEXT, symbol TEXT)")
                cur.execute("""CREATE TABLE ghost_agent_evidence (
                    evidence_id TEXT PRIMARY KEY, task_id TEXT, agent_id TEXT,
                    validation_status TEXT, claims JSONB, source_refs JSONB,
                    submitted_at BIGINT)""")
                ledger.ensure_shadow_evidence_tables(cur)
        yield connection
    finally:
        with closing(psycopg2.connect(dsn, connect_timeout=3)) as conn, conn:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _seed(connection, evidence_id, *, task_id=None, claims=None,
          validation="ACCEPTED", status="COMPLETED", submitted_at=1):
    task_id = task_id or evidence_id
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO ghost_agent_tasks VALUES (%s,%s,'TEST') ON CONFLICT DO NOTHING",
                        (task_id, status))
            cur.execute("INSERT INTO ghost_agent_evidence VALUES (%s,%s,'agent',%s,%s,%s,%s)",
                        (evidence_id, task_id, validation,
                         Json({"verdict": "supports"} if claims is None else claims),
                         Json([]), submitted_at))


def _rows(connection):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ghost_shadow_evidence_scores ORDER BY id")
            return cur.fetchall()


@pytest.mark.parametrize("field,value,sibling", [
    ("verdict", ["supports"], False),
    ("classification", {"value": "gap"}, False),
    ("verdict", ["supports"], True),
    ("classification", {"value": "gap"}, True),
])
def test_jsonb_malformed_own_and_sibling_conclusions(pg_ledger, field, value, sibling):
    own = {"verdict": "supports", "classification": "gap"}
    _seed(pg_ledger, "self", claims=own if sibling else {**own, field: value})
    if sibling:
        _seed(pg_ledger, "other", task_id="self", claims={**own, field: value}, submitted_at=2)
    with pg_ledger() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT jsonb_typeof(claims->%s) FROM ghost_agent_evidence WHERE evidence_id=%s",
                        (field, "other" if sibling else "self"))
            assert cur.fetchone()[0] == ("array" if isinstance(value, list) else "object")
    result = ledger.score_pending_evidence()
    assert result == {"ok": True, "scored": 2 if sibling else 1, "failed": 0, "scanned": 2 if sibling else 1}
    with pg_ledger() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT scoring_version, dimensions_json, detail_json FROM ghost_shadow_evidence_scores WHERE evidence_id='self'")
            version, dimensions, detail = cur.fetchone()
            assert version == "evidence_score_v1"
            assert dimensions["contradiction"] == 0.2
            assert detail["contradiction"]["invalid_conclusion_fields"] == 1


def test_v1_history_eligibility_and_idempotency(pg_ledger, monkeypatch):
    _seed(pg_ledger, "stored")
    ledger.score_and_store_evidence(evidence_id="stored", task_id="stored", symbol="TEST",
                                  claims={"verdict": "supports"}, source_refs=[], now_ts=100)
    original = _rows(pg_ledger)
    _seed(pg_ledger, "broken", claims={"verdict": ["supports"]}, submitted_at=2)
    _seed(pg_ledger, "rejected", validation="REJECTED", submitted_at=3)
    _seed(pg_ledger, "incomplete", status="RUNNING", submitted_at=4)
    assert ledger.score_pending_evidence() == {"ok": True, "scored": 1, "failed": 0, "scanned": 1}
    before = _rows(pg_ledger)
    assert before[:1] == original
    _seed(pg_ledger, "sibling", task_id="broken", claims={"verdict": "rejects"}, submitted_at=5)
    monkeypatch.setattr(ledger, "_now", lambda: 900)
    result = ledger.score_and_store_evidence(evidence_id="broken", task_id="broken", symbol="TEST",
                                           claims={"verdict": ["supports"]}, source_refs=[])
    assert result["idempotent"] is True
    assert result["scored_at"] == 200
    assert result["sibling_count"] == 0
    assert _rows(pg_ledger) == before
    assert ledger.score_pending_evidence() == {"ok": True, "scored": 1, "failed": 0, "scanned": 1}
    assert ledger.score_pending_evidence()["scanned"] == 0
    assert {r["scoring_version"] for r in ledger.recent_scores()} == {"evidence_score_v1"}


@pytest.mark.parametrize("limit,size,first", [(0, 2, 1), (2, 3, 2), (None, 51, 50), (999, 501, 500)])
def test_pending_batch_limits_across_repeated_passes(pg_ledger, limit, size, first):
    with pg_ledger() as conn:
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO ghost_agent_tasks VALUES (%s,'COMPLETED','TEST')",
                            [(f"row_{i}",) for i in range(size)])
            cur.executemany("INSERT INTO ghost_agent_evidence VALUES (%s,%s,'agent','ACCEPTED',%s,%s,%s)",
                            [(f"row_{i}", f"row_{i}", Json({"verdict": "supports"}), Json([]), i) for i in range(size)])
    result = ledger.score_pending_evidence() if limit is None else ledger.score_pending_evidence(limit=limit)
    assert result == {"ok": True, "scored": first, "failed": 0, "scanned": first}
    with pg_ledger() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT evidence_id FROM ghost_shadow_evidence_scores")
            assert {r[0] for r in cur.fetchall()} == {f"row_{i}" for i in range(first)}
    assert ledger.score_pending_evidence()["scored"] == size - first
    assert ledger.score_pending_evidence()["scanned"] == 0


def test_failed_row_isolated_and_still_pending(pg_ledger, monkeypatch):
    _seed(pg_ledger, "failing", submitted_at=1)
    _seed(pg_ledger, "good", submitted_at=2)
    real_score = ledger.score_and_store_evidence

    def fail_one(**kwargs):
        if kwargs["evidence_id"] == "failing":
            raise RuntimeError("synthetic per-row failure")
        return real_score(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(ledger, "score_and_store_evidence", fail_one)
        assert ledger.score_pending_evidence(limit=2) == {"ok": True, "scored": 1, "failed": 1, "scanned": 2}
        assert ledger.score_pending_evidence(limit=2) == {"ok": True, "scored": 0, "failed": 1, "scanned": 1}
    # No retry cap is claimed: when the failure goes away, only that row remains.
    assert ledger.score_pending_evidence(limit=2) == {"ok": True, "scored": 1, "failed": 0, "scanned": 1}
    assert ledger.score_pending_evidence()["scanned"] == 0


def test_real_ddl_version_uniqueness_and_schema_isolation(pg_ledger):
    with pg_ledger() as conn:
        with conn.cursor() as cur:
            ledger.ensure_shadow_evidence_tables(cur)  # idempotent real DDL
            cur.execute("SELECT current_schema()")
            assert cur.fetchone()[0].startswith("ghost_score_test_")
    _seed(pg_ledger, "one")
    ledger.score_and_store_evidence(evidence_id="one", task_id="one", symbol="TEST",
                                  claims={"verdict": "supports"}, source_refs=[])
    # Test the database constraint itself, independently of application lookup.
    insert = """INSERT INTO ghost_shadow_evidence_scores
        (evidence_id,task_id,scoring_version,composite_score,dimensions_json,detail_json,
         weights_json,scored_at,created_at)
        VALUES ('one','one',%s,0.1,'{}','{}','{}',300,300)"""
    with pytest.raises(psycopg2.errors.UniqueViolation):
        with pg_ledger() as conn:
            conn.cursor().execute(insert, ("evidence_score_v1",))
    with pg_ledger() as conn:
        conn.cursor().execute(insert, ("constraint_fixture_other_version",))
    assert len(_rows(pg_ledger)) == 2  # constraint permits distinct versions; no v2 model used


@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_portfolio_mutation_auth_only_in_disposable_app(pg_ledger, monkeypatch, method):
    """No live BASE_URL: actual handlers, test-only credentials, owned PG schema.

    Positive controls prove these requests reach writes when authorized, so an
    auth regression cannot silently pass a test that only exercises a stub.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core import portfolio_routes

    monkeypatch.setenv("GHOST_TEST_MODE", "1")
    monkeypatch.setenv("GHOST_MCP_TOKEN", "disposable-portfolio-auth-token")
    monkeypatch.setenv("CRON_SECRET", "disposable-portfolio-cookie-secret")
    monkeypatch.setenv("GHOST_DEV_MODE", "0")
    monkeypatch.setattr(portfolio_routes, "db_conn", pg_ledger)
    with pg_ledger() as conn:
        with conn.cursor() as cur:
            cur.execute(portfolio_routes._CREATE_TABLE)
            cur.execute("""INSERT INTO user_portfolio
                (symbol,asset_type,quantity,buy_price,buy_date,notes)
                VALUES ('TEST_OWNED','stock',1,1,'2026-01-15','isolated auth fixture')
                RETURNING id""")
            position_id = cur.fetchone()[0]

    def positions():
        with pg_ledger() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM user_portfolio ORDER BY id")
                return cur.fetchall()

    app = FastAPI()
    app.include_router(portfolio_routes.portfolio_router)
    payload = {"symbol": "ZZE2E_ISOLATED", "asset_type": "stock", "quantity": 1,
               "buy_price": 1, "buy_date": "2026-01-15"}
    path = "/api/portfolio" if method == "POST" else f"/api/portfolio/{position_id}"
    kwargs = {"json": payload} if method == "POST" else {}
    before = positions()
    # Minimal app: no production startup, scheduler, broker or live HTTP calls.
    with TestClient(app, base_url="https://testserver") as client:
        denied = client.request(method, path, **kwargs)
        assert denied.status_code == 401
        assert positions() == before
        allowed = client.request(method, path, headers={"x-ghost-mcp-token": "disposable-portfolio-auth-token"}, **kwargs)
        assert allowed.status_code == 200
        assert allowed.json()["ok"] is True
    after = positions()
    assert len(after) == len(before) + (1 if method == "POST" else -1)
