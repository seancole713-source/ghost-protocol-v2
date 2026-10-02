"""F07: authorization codes are consumed by one atomic DELETE ... RETURNING."""
import hashlib
import json
import time

import pytest

import core.db as db
from mcp import oauth_server as osrv


class _Cur:
    def __init__(self, rows, rowcount=None):
        self._rows = rows
        self.rowcount = len(rows) if rowcount is None else rowcount
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchall(self):
        return self._rows

    def fetchone(self):  # pragma: no cover - must not be used
        raise AssertionError("consume must not SELECT then DELETE")


class _Conn:
    def __init__(self, cur):
        self.cur = cur
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self.cur


def _ctx(conn):
    class _C:
        def __enter__(self):
            return conn

        def __exit__(self, exc_type, *_):
            if exc_type:
                conn.rolled_back = True
            else:
                conn.committed = True
            return False

    return _C


def _payload(**over):
    data = {"client_id": "https://claude.ai/client", "redirect_uri": "http://127.0.0.1/cb",
            "code_challenge": "c", "scope": "ghost:read", "exp": int(time.time()) + 60}
    data.update(over)
    return json.dumps(data)


def test_consume_is_one_delete_returning(monkeypatch):
    cur = _Cur([(_payload(),)])
    conn = _Conn(cur)
    monkeypatch.setattr(db, "db_conn", _ctx(conn))
    out = osrv.pop_auth_code("abc")
    assert out["client_id"] == "https://claude.ai/client"
    assert len(cur.sql) == 1
    sql, params = cur.sql[0]
    assert sql.startswith("DELETE FROM ghost_state WHERE key=%s RETURNING val")
    assert params == ("oauth_code_abc",)
    assert conn.committed


def test_second_redemption_gets_nothing(monkeypatch):
    conn = _Conn(_Cur([], rowcount=0))  # the concurrent winner already deleted it
    monkeypatch.setattr(db, "db_conn", _ctx(conn))
    assert osrv.pop_auth_code("abc") is None


def test_more_than_one_row_is_rejected_and_rolled_back(monkeypatch):
    conn = _Conn(_Cur([(_payload(),), (_payload(),)]))
    monkeypatch.setattr(db, "db_conn", _ctx(conn))
    with pytest.raises(RuntimeError):
        osrv.pop_auth_code("abc")
    assert conn.rolled_back and not conn.committed


def test_expired_code_is_consumed_but_not_returned(monkeypatch):
    cur = _Cur([(_payload(exp=int(time.time()) - 1),)])
    monkeypatch.setattr(db, "db_conn", _ctx(_Conn(cur)))
    assert osrv.pop_auth_code("abc") is None
    assert cur.sql[0][0].startswith("DELETE")


def test_empty_code_never_touches_the_database(monkeypatch):
    def _boom():
        raise AssertionError("no DB access for an empty code")

    monkeypatch.setattr(db, "db_conn", _boom)
    assert osrv.pop_auth_code("") is None


def test_malformed_payload_returns_none(monkeypatch):
    monkeypatch.setattr(db, "db_conn", _ctx(_Conn(_Cur([("not json",)]))))
    assert osrv.pop_auth_code("abc") is None


def _token_request(client, code, *, verifier, client_id="https://claude.ai/client",
                   redirect_uri="http://127.0.0.1/cb"):
    return client.post(
        "/oauth/token",
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
              "client_id": client_id, "code_verifier": verifier},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


@pytest.mark.parametrize("override,expect", [
    ({"client_id": "https://evil.example/client"}, "client_id mismatch"),
    ({"redirect_uri": "https://evil.example/cb"}, "redirect_uri mismatch"),
    ({"verifier": "wrong-verifier"}, "invalid code_verifier"),
])
def test_token_route_keeps_binding_checks(monkeypatch, override, expect):
    from fastapi.testclient import TestClient
    from wolf_app import APP

    monkeypatch.setenv("GHOST_OAUTH_SIGNING_KEY", "unit-signing-key")
    verifier = "right-verifier-123"
    challenge = osrv._b64url(hashlib.sha256(verifier.encode()).digest())
    monkeypatch.setattr(db, "db_conn", _ctx(_Conn(_Cur([(_payload(code_challenge=challenge),)]))))
    issued = []
    monkeypatch.setattr("mcp.oauth_routes.store_refresh_token", lambda t: issued.append(t))
    kwargs = {"verifier": verifier}
    kwargs.update(override)
    r = _token_request(TestClient(APP), "abc", **kwargs)
    assert r.status_code == 400
    assert r.json()["detail"] == expect
    assert issued == []
