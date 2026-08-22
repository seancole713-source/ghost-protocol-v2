import logging

from core.access_log_filters import PeacefulAccessFilter, _redact_mcp_path_token


def _record(method, path, status):
    r = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/1.1" %s',
        args=("127.0.0.1:1", method, path, "1.1", status),
        exc_info=None,
    )
    return r


def test_redact_mcp_path_token():
    assert _redact_mcp_path_token("/mcp/secret-token-123") == "/mcp/[REDACTED]"
    assert _redact_mcp_path_token("/mcp/abc?x=1") == "/mcp/[REDACTED]?x=1"
    assert _redact_mcp_path_token("/api/picks") == "/api/picks"


def test_mcp_path_token_redacted_in_access_log():
    f = PeacefulAccessFilter()
    rec = _record("POST", "/mcp/super-secret-token", 200)
    assert f.filter(rec) is True  # POSTs are always kept
    # The secret must be scrubbed from the logged path.
    assert "/mcp/[REDACTED]" in rec.args[2]
    assert "super-secret-token" not in rec.args[2]


def test_peaceful_access_filter_suppresses_old_successful_super_ghost_reads():
    f = PeacefulAccessFilter()
    assert f.filter(_record("GET", "/api/wolf/super-ghost/history?symbol=WOLF&limit=30", 200)) is False
    assert f.filter(_record("GET", "/api/wolf/super-ghost/precision?symbol=WOLF&horizon=5", 200)) is False
    assert f.filter(_record("GET", "/api/ghost/doctrine/WOLF", 200)) is False


def test_peaceful_access_filter_keeps_important_logs():
    f = PeacefulAccessFilter()
    # New peaceful endpoint still visible.
    assert f.filter(_record("GET", "/api/wolf/super-ghost/snapshot?symbol=WOLF&horizon=5", 200)) is True
    # Urgent/liveness endpoints still visible.
    assert f.filter(_record("GET", "/api/_version", 200)) is True
    assert f.filter(_record("GET", "/api/wolf/kill-status", 200)) is True
    assert f.filter(_record("GET", "/api/squeeze/picks", 200)) is True
    # Errors and writes must never be hidden.
    assert f.filter(_record("GET", "/api/wolf/super-ghost/history?symbol=WOLF", 500)) is True
    assert f.filter(_record("POST", "/api/wolf/super-ghost/learn", 200)) is True
