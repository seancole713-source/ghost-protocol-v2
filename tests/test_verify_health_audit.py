"""F30: the zero-critical health-audit gate must never PASS vacuously, and its
audit call must be read-only."""
import pytest
import requests

import scripts.verify_health_audit as vha


class _Resp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://example.test")


def _capture(monkeypatch, payload=None, exc=None):
    calls = []

    def fake_fetch(url, *, method="GET", headers=None):
        calls.append((method, url, dict(headers or {})))
        if exc is not None:
            raise exc
        return payload

    monkeypatch.setattr(vha, "_fetch_json", fake_fetch)
    return calls


def test_without_cron_secret_reports_skipped_and_makes_no_call(monkeypatch, capsys):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    calls = _capture(monkeypatch)
    assert vha.main() == 0
    out = capsys.readouterr().out
    assert out.strip().splitlines()[-1].startswith("SKIPPED:")
    assert "PASS" not in out
    assert calls == []


def test_blank_cron_secret_is_also_skipped(monkeypatch, capsys):
    monkeypatch.setenv("CRON_SECRET", "   ")
    _capture(monkeypatch)
    assert vha.main() == 0
    assert "PASS" not in capsys.readouterr().out


def test_audit_call_is_read_only(monkeypatch, capsys):
    monkeypatch.setenv("CRON_SECRET", "s")
    calls = _capture(monkeypatch, payload={"ok": True, "audit": {"status": "PASS", "findings": [
        {"status": "PASS", "impact": "critical", "location": "route:/health"},
    ]}})
    assert vha.main() == 0
    assert capsys.readouterr().out.strip().splitlines()[-1].startswith("PASS:")
    assert len(calls) == 1
    method, url, headers = calls[0]
    assert method == "POST"
    assert "auto_fix=false" in url and "persist=false" in url
    assert "auto_fix=true" not in url
    assert headers.get("X-Cron-Secret") == "s"


def test_seeded_critical_finding_turns_gate_red(monkeypatch, capsys):
    monkeypatch.setenv("CRON_SECRET", "s")
    _capture(monkeypatch, payload={"ok": True, "audit": {"findings": [
        {"status": "PASS", "impact": "low", "location": "a"},
        {"status": "FAIL", "impact": "critical", "location": "runtime:/api/diagnostics.errors",
         "evidence": "x"},
    ]}})
    assert vha.main() == 1
    out = capsys.readouterr().out
    assert "FAIL: critical unresolved" in out
    assert "PASS" not in out


def test_empty_findings_fail_closed(monkeypatch, capsys):
    monkeypatch.setenv("CRON_SECRET", "s")
    _capture(monkeypatch, payload={"ok": True, "audit": {"findings": []}})
    assert vha.main() == 1
    assert "PASS" not in capsys.readouterr().out


def test_403_with_secret_fails(monkeypatch, capsys):
    monkeypatch.setenv("CRON_SECRET", "wrong")
    _capture(monkeypatch, exc=requests.HTTPError(response=_Resp(403, "")))
    assert vha.main() == 1
    assert "FAIL" in capsys.readouterr().out


def test_error_payload_fails(monkeypatch, capsys):
    monkeypatch.setenv("CRON_SECRET", "s")
    _capture(monkeypatch, payload={"ok": False, "error": "boom"})
    assert vha.main() == 1


def test_health_audit_persist_false_skips_history_write(monkeypatch):
    import core.health_audit as ha
    persisted = []
    monkeypatch.setattr(ha, "_persist_run", lambda db_conn, report: persisted.append(report))

    class _Cur:
        def execute(self, *a, **k):
            pass

        def fetchone(self):
            return None

    class _Conn:
        def cursor(self):
            return _Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    kwargs = dict(app=None, db_conn=lambda: _Conn(), health_payload={},
                  diagnostics_payload={}, stats_payload={}, cockpit_payload={})
    ha.run_health_audit(auto_fix=False, persist=False, **kwargs)
    assert persisted == []
    ha.run_health_audit(auto_fix=False, **kwargs)
    assert len(persisted) == 1
