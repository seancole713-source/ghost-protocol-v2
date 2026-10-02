"""PostgreSQL integration tests: an authorization code is redeemable exactly once (F07).

Run with TEST_DATABASE_URL and GHOST_INTEGRATION_TESTS=1 (``-m integration``).
Uses two real connections so the row-lock behaviour of the single
``DELETE ... RETURNING`` consume is exercised, not emulated.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import closing
import os
import threading
import time

import psycopg2
import pytest
from fastapi.testclient import TestClient

from mcp import oauth_server as osrv

pytestmark = pytest.mark.integration

CLIENT = "https://claude.ai/client"
REDIRECT = "http://127.0.0.1/cb"
VERIFIER = "integration-verifier-0123456789"
CHALLENGE = osrv._b64url(hashlib.sha256(VERIFIER.encode()).digest())


def _url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url or os.getenv("GHOST_INTEGRATION_TESTS") != "1":
        pytest.skip("OAuth PostgreSQL tests require TEST_DATABASE_URL and GHOST_INTEGRATION_TESTS=1")
    return url


@pytest.fixture(autouse=True)
def oauth_database(monkeypatch):
    url = _url()

    class _DbContext:
        def __enter__(self):
            self.conn = psycopg2.connect(url)
            return self.conn

        def __exit__(self, exc_type, *_args):
            try:
                if exc_type:
                    self.conn.rollback()
                else:
                    self.conn.commit()
            finally:
                self.conn.close()

    import core.db as db

    monkeypatch.setattr(db, "db_conn", _DbContext)
    monkeypatch.setenv("GHOST_OAUTH_SIGNING_KEY", "integration-signing-key")
    monkeypatch.setenv("GHOST_PUBLIC_URL", "https://ghost.test")

    def _clean():
        with _DbContext() as conn:
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
            cur.execute("DELETE FROM ghost_state WHERE key LIKE 'oauth\\_%%'")

    _clean()
    yield url
    _clean()


def _store(code: str, **over) -> None:
    kwargs = {"client_id": CLIENT, "redirect_uri": REDIRECT,
              "code_challenge": CHALLENGE, "scope": "ghost:read"}
    kwargs.update(over)
    osrv.store_auth_code(code, **kwargs)


def _count(url: str, prefix: str) -> int:
    with closing(psycopg2.connect(url)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM ghost_state WHERE key LIKE %s", (prefix + "%",))
        return int(cur.fetchone()[0])


def _redeem(client, code, *, verifier=VERIFIER, client_id=CLIENT, redirect_uri=REDIRECT):
    return client.post(
        "/oauth/token",
        data={"grant_type": "authorization_code", "code": code, "client_id": client_id,
              "redirect_uri": redirect_uri, "code_verifier": verifier},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


def test_blocked_second_redemption_gets_nothing_after_first_commits(oauth_database):
    """Connection A consumes inside an open transaction; B blocks, then sees no row."""
    url = oauth_database
    _store("race-code")
    key = osrv._state_key("code", "race-code")
    conn_a = psycopg2.connect(url)
    result = {}
    try:
        cur_a = conn_a.cursor()
        cur_a.execute(osrv._CONSUME_AUTH_CODE_SQL, (key,))
        rows_a = cur_a.fetchall()
        assert cur_a.rowcount == 1 and len(rows_a) == 1

        def _b():
            result["b"] = osrv.pop_auth_code("race-code")

        t = threading.Thread(target=_b)
        t.start()
        # Wait until B is genuinely blocked on A's row lock.
        deadline = time.time() + 10
        blocked = False
        with closing(psycopg2.connect(url)) as probe:
            probe.autocommit = True
            pc = probe.cursor()
            while time.time() < deadline and not blocked:
                pc.execute(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND query LIKE 'DELETE FROM ghost_state%%'"
                )
                blocked = pc.fetchone()[0] >= 1
                if not blocked:
                    time.sleep(0.05)
        assert blocked, "second redemption never waited on the first one's row lock"
        assert t.is_alive()
        conn_a.commit()
        t.join(10)
        assert not t.is_alive()
    finally:
        conn_a.close()
    assert json.loads(rows_a[0][0])["client_id"] == CLIENT
    assert result["b"] is None
    assert _count(url, "oauth_code_") == 0


def test_exactly_one_of_two_concurrent_redemptions_succeeds(oauth_database):
    url = oauth_database
    from wolf_app import APP

    for i in range(10):
        code = f"concurrent-{i}"
        _store(code)
        barrier = threading.Barrier(2)
        statuses = []

        def _go():
            client = TestClient(APP)
            barrier.wait()
            statuses.append(_redeem(client, code).status_code)

        threads = [threading.Thread(target=_go) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert sorted(statuses) == [200, 400], statuses
    assert _count(url, "oauth_code_") == 0
    assert _count(url, "oauth_refresh_") == 10  # one token pair per code


def test_expired_code_issues_no_token(oauth_database):
    url = oauth_database
    from wolf_app import APP

    _store("expired-code")
    key = osrv._state_key("code", "expired-code")
    with closing(psycopg2.connect(url)) as conn:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT val FROM ghost_state WHERE key=%s", (key,))
        data = json.loads(cur.fetchone()[0])
        data["exp"] = int(time.time()) - 1
        cur.execute("UPDATE ghost_state SET val=%s WHERE key=%s", (json.dumps(data), key))
    r = _redeem(TestClient(APP), "expired-code")
    assert r.status_code == 400
    assert "access_token" not in r.text
    assert _count(url, "oauth_refresh_") == 0
    assert _count(url, "oauth_code_") == 0  # consumed, never redeemable later


@pytest.mark.parametrize("override", [
    {"client_id": "https://evil.example/client"},
    {"redirect_uri": "https://evil.example/cb"},
    {"verifier": "wrong-verifier-0123456789"},
])
def test_wrong_binding_issues_no_token_and_burns_the_code(oauth_database, override):
    url = oauth_database
    from wolf_app import APP

    client = TestClient(APP)
    _store("bound-code")
    r = _redeem(client, "bound-code", **override)
    assert r.status_code == 400
    assert "access_token" not in r.text
    # The legitimate holder cannot redeem a code an attacker already presented.
    assert _redeem(client, "bound-code").status_code == 400
    assert _count(url, "oauth_refresh_") == 0


def test_valid_redemption_issues_one_token(oauth_database):
    url = oauth_database
    from wolf_app import APP

    client = TestClient(APP)
    _store("good-code")
    r = _redeem(client, "good-code")
    assert r.status_code == 200
    assert osrv.verify_access_token(r.json()["access_token"], "https://ghost.test")
    assert _count(url, "oauth_refresh_") == 1
    assert _redeem(client, "good-code").status_code == 400
