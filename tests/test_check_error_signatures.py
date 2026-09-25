import json

import pytest
import requests

import scripts.check_error_signatures as sig


class _Resp:
    def __init__(self, status_code=404, text='{"detail":"Not Found"}'):
        self.status_code = status_code
        self.text = text


def _http_error(status=404):
    return requests.HTTPError(response=_Resp(status))


def test_diagnostics_errors_gated_404_is_not_collected(monkeypatch):
    def fake_fetch(url, **kwargs):
        raise _http_error(404)

    monkeypatch.setattr(sig, "_fetch_json", fake_fetch)
    # None = "could not check", which must never be read as "no errors".
    assert sig._diagnostics_errors("https://example.test") is None


def test_audit_failures_not_collected_without_cron(monkeypatch):
    calls = []
    monkeypatch.setattr(sig, "_fetch_json", lambda url, **kw: calls.append(url))
    assert sig._audit_failures("https://example.test", "") is None
    assert calls == []  # no request at all without the secret


def test_audit_failures_raises_gated_with_cron(monkeypatch):
    def fake_fetch(url, **kwargs):
        raise _http_error(403)

    monkeypatch.setattr(sig, "_fetch_json", fake_fetch)
    try:
        sig._audit_failures("https://example.test", "secret")
    except requests.HTTPError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected HTTPError")


# ── main(): PASS / FAIL / SKIPPED ───────────────────────────────────────────

@pytest.fixture
def baseline(tmp_path, monkeypatch):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"allowed_patterns": []}))
    monkeypatch.setenv("ERROR_SIGNATURE_BASELINE", str(path))
    monkeypatch.setenv("BASE_URL", "https://example.test")
    return path


def _route(monkeypatch, *, audit_payload=None):
    calls = []

    def fake_fetch(url, *, method="GET", headers=None):
        calls.append((method, url, dict(headers or {})))
        if url.endswith("/api/diagnostics"):
            raise _http_error(404)
        if "/api/health/audit" in url:
            return audit_payload
        raise AssertionError(url)

    monkeypatch.setattr(sig, "_fetch_json", fake_fetch)
    return calls


def test_main_without_cron_secret_is_skipped_not_pass(monkeypatch, capsys, baseline):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    calls = _route(monkeypatch)
    assert sig.main() == 0
    out = capsys.readouterr().out
    last = out.strip().splitlines()[-1]
    assert last.startswith("SKIPPED:")
    assert "PASS" not in out
    assert all("/api/health/audit" not in url for _m, url, _h in calls)


def test_main_uses_read_only_audit_and_passes_when_clean(monkeypatch, capsys, baseline):
    monkeypatch.setenv("CRON_SECRET", "s")
    payload = {"ok": True, "audit": {"findings": [
        {"status": "PASS", "location": "route:/health", "evidence": "ok"},
    ]}}
    calls = _route(monkeypatch, audit_payload=payload)
    assert sig.main() == 0
    out = capsys.readouterr().out
    assert out.strip().splitlines()[-1].startswith("PASS:")
    audit_calls = [(m, u) for m, u, _h in calls if "/api/health/audit" in u]
    assert audit_calls, "audit was not called"
    for method, url in audit_calls:
        assert method == "POST"
        assert "auto_fix=false" in url and "persist=false" in url
        assert "auto_fix=true" not in url


def test_main_fails_on_new_signature(monkeypatch, capsys, baseline):
    monkeypatch.setenv("CRON_SECRET", "s")
    payload = {"ok": True, "audit": {"findings": [
        {"status": "FAIL", "location": "runtime:x", "evidence": "boom 42", "impact": "critical"},
    ]}}
    _route(monkeypatch, audit_payload=payload)
    assert sig.main() == 1
    assert "FAIL: new error signatures detected" in capsys.readouterr().out


def test_main_fails_closed_on_empty_audit(monkeypatch, capsys, baseline):
    monkeypatch.setenv("CRON_SECRET", "s")
    _route(monkeypatch, audit_payload={"ok": True, "audit": {"findings": []}})
    assert sig.main() == 1
    out = capsys.readouterr().out
    assert "PASS" not in out


def test_main_known_signature_passes(monkeypatch, capsys, baseline):
    baseline.write_text(json.dumps({"allowed_patterns": [
        {"check": "audit:runtime:x", "detail_regex": "boom"},
    ]}))
    monkeypatch.setenv("CRON_SECRET", "s")
    payload = {"ok": True, "audit": {"findings": [
        {"status": "FAIL", "location": "runtime:x", "evidence": "boom 42"},
    ]}}
    _route(monkeypatch, audit_payload=payload)
    assert sig.main() == 0
    assert "PASS: only known error signatures detected" in capsys.readouterr().out
