"""PR #131: the global ValueError handler must distinguish client input from bugs.

A ValueError raised in a route-handler file (hand-parsed params) → 422.
A ValueError raised deeper (core/, libraries) → 500 + logged traceback, so an
internal bug can never masquerade as "invalid_input".
"""
import asyncio
import json
from types import SimpleNamespace

import wolf_app


def _exc_raised_in(filename: str) -> ValueError:
    """Raise-and-catch a ValueError whose deepest frame carries `filename`."""
    code = compile("def boom():\n    raise ValueError('synthetic')", filename, "exec")
    ns = {}
    exec(code, ns)
    try:
        ns["boom"]()
    except ValueError as e:
        return e
    raise AssertionError("did not raise")


def _run_handler(exc: ValueError):
    request = SimpleNamespace(url=SimpleNamespace(path="/test-path"))
    resp = asyncio.run(wolf_app._value_error_handler(request, exc))
    return resp.status_code, json.loads(bytes(resp.body))


def test_route_origin_valueerror_returns_422():
    exc = _exc_raised_in("/app/api/routes_data.py")
    status, body = _run_handler(exc)
    assert status == 422
    assert body["error"] == "invalid_input"


def test_wolf_app_origin_valueerror_returns_422():
    exc = _exc_raised_in("/app/wolf_app.py")
    status, body = _run_handler(exc)
    assert status == 422


def test_core_origin_valueerror_returns_500_internal():
    exc = _exc_raised_in("/app/core/signal_engine.py")
    status, body = _run_handler(exc)
    assert status == 500
    assert body["error"] == "internal_error"
    assert "synthetic" not in json.dumps(body)  # no internal detail leaked


def test_library_origin_valueerror_returns_500():
    exc = _exc_raised_in("/site-packages/numpy/core/whatever.py")
    status, body = _run_handler(exc)
    assert status == 500
