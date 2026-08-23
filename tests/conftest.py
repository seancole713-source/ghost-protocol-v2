"""Pytest bootstrap — allow STOCK_SYMBOLS env overrides in tests only."""
import os
import sys

import pytest

os.environ.setdefault("GHOST_ALLOW_ENV_WATCHLIST", "1")


def _integration_enabled():
    return bool(os.getenv("TEST_DATABASE_URL")) and os.getenv(
        "GHOST_INTEGRATION_TESTS", "0",
    ) in ("1", "true", "TRUE")


if _integration_enabled():
    # prediction_filters freezes its SQL fragments at import time.
    os.environ.setdefault("WATCHLIST_FILTER_ENABLED", "0")


@pytest.fixture(scope="session")
def _integration_database_session():
    if not _integration_enabled():
        pytest.skip(
            "Integration DB tests disabled. Set TEST_DATABASE_URL and "
            "GHOST_INTEGRATION_TESTS=1.",
        )

    from core import db as core_db

    test_dsn = os.environ["TEST_DATABASE_URL"]
    previous_dsn = os.environ.get("DATABASE_URL")
    try:
        if core_db._pool:
            core_db._pool.closeall()
    except Exception:
        pass
    core_db._pool = None
    os.environ["DATABASE_URL"] = test_dsn
    core_db.init_db()
    try:
        yield
    finally:
        try:
            if core_db._pool:
                core_db._pool.closeall()
        finally:
            core_db._pool = None
            if previous_dsn is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous_dsn


@pytest.fixture(autouse=True)
def _integration_database_for_marked_test(request, monkeypatch):
    if request.node.get_closest_marker("integration") is not None:
        request.getfixturevalue("_integration_database_session")
        monkeypatch.setenv("GHOST_TEST_MODE", "1")
        monkeypatch.setenv("GHOST_MCP_TOKEN", "itest-token")
        monkeypatch.setenv("WATCHLIST_FILTER_ENABLED", "0")
    yield


@pytest.fixture
def integration_db(request):
    request.getfixturevalue("_integration_database_session")


@pytest.fixture(autouse=True)
def _clear_module_caches():
    """Module-level caches (model cache, login throttle) must not leak state
    between tests."""
    se = sys.modules.get("core.signal_engine")
    if se is not None:
        try:
            se.invalidate_model_cache()
            se._SIP_FORBIDDEN["until"] = 0.0
        except Exception:
            pass
    px = sys.modules.get("core.prices")
    if px is not None:
        try:
            px._SIP_FORBIDDEN["until"] = 0.0
        except Exception:
            pass
    pg = sys.modules.get("core.precision_gate")
    if pg is not None:
        try:
            pg.invalidate_global_threshold_cache()
        except Exception:
            pass
    wa = sys.modules.get("wolf_app")
    if wa is not None:
        try:
            wa._LOGIN_ATTEMPTS.clear()
        except Exception:
            pass
    # Circuit-breaker singletons are module-global; a test that trips one
    # (record_failure) leaks OPEN state into any later test that reads the
    # breaker without patching it — e.g. options_snapshots.record_snapshots now
    # consults _yfinance_cb.allow() and stops early when it is open. Reset all
    # breakers to CLOSED before each test so order can never change outcomes.
    cb = sys.modules.get("core.circuit_breaker")
    if cb is not None:
        try:
            for _name in dir(cb):
                _obj = getattr(cb, _name)
                # Instances only — the CircuitBreaker CLASS also has .reset and
                # the field, but calling it unbound would throw and abort the loop.
                if isinstance(_obj, cb.CircuitBreaker):
                    _obj.reset()
        except Exception:
            pass
    yield


@pytest.fixture(autouse=True)
def _hermetic_premarket(monkeypatch):
    """Kill the live premarket overlay for every test by default.

    predict_live_ex's premarket path makes REAL market-data calls during
    4:00-9:30 AM CT and stomps synthetic fixtures' last bar with the live
    symbol price — a time-of-day flake where the suite fails only when CI
    happens to run in that window. Premarket-specific tests re-enable via
    their own monkeypatch (delenv restores the default-on behavior; setenv
    forces a value) — both override this autouse default."""
    monkeypatch.setenv("GHOST_PREMARKET_SCAN", "0")
    # PR #125: tests run in dev mode — _cron_ok requires explicit GHOST_DEV_MODE=1
    monkeypatch.setenv("GHOST_DEV_MODE", "1")


def _assert_pinned_ml_versions():
    """Tier 3 hygiene: the dev environment must match the pinned production
    requirements. A silent sklearn/numpy/xgboost bump already bit once
    (sklearn >=1.6 removed cv="prefit", silently uncalibrating the engine —
    forensic SE-3/ST-5). Fail fast at collection time instead of letting a
    drifted interpreter produce subtly wrong results."""
    import importlib.metadata as _md

    pinned = {
        "scikit-learn": "1.5.2",
        "numpy": "1.26.4",
        "xgboost": "2.1.1",
    }
    for dist, want in pinned.items():
        try:
            got = _md.version(dist)
        except _md.PackageNotFoundError:
            got = None
        if got != want:
            raise RuntimeError(
                f"ML dependency drift: {dist} is {got!r}, expected {want!r}. "
                "Reinstall from requirements.txt (pip install -r requirements.txt) "
                "or update this pin deliberately."
            )


_assert_pinned_ml_versions()
