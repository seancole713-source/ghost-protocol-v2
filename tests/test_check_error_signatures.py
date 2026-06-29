import requests

import scripts.check_error_signatures as sig


class _Resp:
    def __init__(self, status_code=404, text='{"detail":"Not Found"}'):
        self.status_code = status_code
        self.text = text


def _http_error(status=404):
    return requests.HTTPError(response=_Resp(status))


def test_diagnostics_errors_skips_gated_404(monkeypatch):
    def fake_fetch(url, **kwargs):
        raise _http_error(404)

    monkeypatch.setattr(sig, "_fetch_json", fake_fetch)
    assert sig._diagnostics_errors("https://example.test") == []


def test_audit_failures_skips_gated_without_cron(monkeypatch):
    def fake_fetch(url, **kwargs):
        raise _http_error(403)

    monkeypatch.setattr(sig, "_fetch_json", fake_fetch)
    assert sig._audit_failures("https://example.test", "") == []


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
