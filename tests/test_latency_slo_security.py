"""SEC-01 / OPS-01: latency telemetry must not leak path credentials, must
report only in-window samples, and must keep a bounded key set."""
from fastapi.testclient import TestClient

import core.latency_slo as slo
import wolf_app

SYNTHETIC_TOKEN = "synthetic-mcp-token-7f3a9c2e"


def _client(monkeypatch):
    monkeypatch.setenv("GHOST_TEST_MODE", "1")
    monkeypatch.setenv("GHOST_MCP_TOKEN", SYNTHETIC_TOKEN)
    monkeypatch.setenv("CRON_SECRET", "cron-test-secret")
    slo.reset_for_tests()
    return TestClient(wolf_app.APP)


def test_mcp_path_token_never_reaches_latency_telemetry(monkeypatch):
    client = _client(monkeypatch)
    r = client.get(f"/mcp/{SYNTHETIC_TOKEN}")
    assert r.status_code == 200

    # Stored keys are route templates, not raw paths.
    keys = set(slo._WINDOW.keys())
    assert "/mcp/{path_token}" in keys
    assert not any(SYNTHETIC_TOKEN in k for k in keys)

    anon = client.get("/api/system/latency")
    assert anon.status_code == 200
    assert SYNTHETIC_TOKEN not in anon.text
    body = anon.json()
    assert body["ok"] is True and "overall" in body
    # Per-route detail is operator-only.
    assert "routes" not in body and "slowest_routes" not in body

    authed = client.get("/api/system/latency", headers={"X-Cron-Secret": "cron-test-secret"})
    assert authed.status_code == 200
    assert SYNTHETIC_TOKEN not in authed.text
    assert "/mcp/{path_token}" in authed.json()["routes"]


def test_wrong_cron_secret_gets_only_the_public_summary(monkeypatch):
    client = _client(monkeypatch)
    client.get(f"/mcp/{SYNTHETIC_TOKEN}")
    r = client.get("/api/system/latency", headers={"X-Cron-Secret": "wrong"})
    assert r.status_code == 200
    assert "routes" not in r.json()


def test_unmatched_paths_collapse_to_one_bucket(monkeypatch):
    client = _client(monkeypatch)
    for i in range(5):
        client.get(f"/no-such-route/{SYNTHETIC_TOKEN}-{i}")
    keys = set(slo._WINDOW.keys())
    assert slo.UNMATCHED_ROUTE in keys
    assert not any(SYNTHETIC_TOKEN in k for k in keys)


def test_route_label_never_returns_raw_path():
    assert slo.route_label({"path": f"/mcp/{SYNTHETIC_TOKEN}"}) == slo.UNMATCHED_ROUTE
    assert slo.route_label({"root_path": "/static", "app_root_path": ""}) == "/static/{path:path}"


def test_stale_samples_are_pruned_on_read():
    slo.reset_for_tests()
    t0 = 1_000_000.0
    slo.record("/api/old", 50.0, now=t0)
    # A day later, without any new record on that route, the read must not
    # report the day-old sample under the 5-minute window.
    later = t0 + 86_400
    assert slo.route_stats("/api/old", now=later)["samples"] == 0
    stats = slo.all_stats(now=later)
    assert stats["overall"]["samples"] == 0
    assert "/api/old" not in stats["routes"]
    assert slo.slowest_routes(5, now=later) == []
    assert "/api/old" not in slo._WINDOW  # empty key evicted


def test_route_key_set_is_hard_capped(monkeypatch):
    slo.reset_for_tests()
    monkeypatch.setattr(slo, "_MAX_ROUTES", 8)
    t0 = 1_000_000.0
    for i in range(50):
        slo.record(f"/r/{i}", 1.0, now=t0 + i)  # all within the window
    assert len(slo._WINDOW) <= 8
    # Newest routes survive; oldest were evicted.
    assert "/r/49" in slo._WINDOW and "/r/0" not in slo._WINDOW
    slo.reset_for_tests()
