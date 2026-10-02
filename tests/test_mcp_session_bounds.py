"""SEC-02: unauthenticated MCP initialize must not grow session state without bound."""
import mcp.jsonrpc as jr


def _init(i):
    return {"jsonrpc": "2.0", "id": i, "method": "initialize", "params": {}}


def setup_function(_fn):
    jr.clear_sessions_for_tests()


def teardown_function(_fn):
    jr.clear_sessions_for_tests()


def test_batch_of_initialize_mints_at_most_one_session():
    payload, sid = jr.process_jsonrpc_body([_init(i) for i in range(jr.MAX_BATCH_SIZE)])
    assert sid is not None
    assert isinstance(payload, list) and len(payload) == jr.MAX_BATCH_SIZE
    assert jr.session_count() == 1
    assert jr.session_known(sid)


def test_oversized_batch_is_rejected_without_creating_sessions():
    payload, sid = jr.process_jsonrpc_body([_init(i) for i in range(100)])
    assert sid is None
    assert isinstance(payload, dict) and payload["error"]["code"] == -32600
    assert jr.session_count() == 0


def test_session_registry_is_capped(monkeypatch):
    monkeypatch.setattr(jr, "_MAX_SESSIONS", 16)
    first = jr.create_session_id(now=0.0)
    for i in range(200):
        jr.create_session_id(now=float(i + 1))
    assert jr.session_count() <= 16
    assert not jr.session_known(first, now=201.0)


def test_sessions_expire_after_ttl():
    sid = jr.create_session_id(now=1000.0)
    assert jr.session_known(sid, now=1000.0 + jr._SESSION_TTL_S - 1)
    assert not jr.session_known(sid, now=1000.0 + jr._SESSION_TTL_S + 1)
    assert jr.session_count() == 0
