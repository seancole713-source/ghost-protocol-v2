"""Pytest bootstrap — allow STOCK_SYMBOLS env overrides in tests only."""
import os
import sys

import pytest

os.environ.setdefault("GHOST_ALLOW_ENV_WATCHLIST", "1")


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
