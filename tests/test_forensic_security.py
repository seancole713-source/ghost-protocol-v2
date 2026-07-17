"""Forensic security round (PR #127) — regression tripwires.

Findings verified against main@ace0998 by the security/route-auth audit:
  P2-1  build_ask_context lost its include_portfolio parameter; the public
        /ask path excluded holdings only via a swallowed NameError, and both
        auth-gated callers (ask/context, MCP ghost_context) 500'd.
  P0-1  POST /api/wolf/ask was the lone unauthenticated mutating endpoint
        (spends the paid Anthropic budget).
  P2-2  _admin_token_valid failed OPEN with no CRON_SECRET on any non-Railway
        host (opens /admin, /api/portfolio, /api/my-picks).
  P1-1  /api/v2/recent?symbol=ALL bypassed the documented WOLF-only default
        with no auth.
  P0    record_pick_resolution failures were swallowed with a bare pass —
        silent P&L accounting divergence.
  P0    stats-era watermark 223438 hardcoded in 6 places.
"""
import inspect


# ── P2-1: the PII gate is a real parameter, not a swallowed exception ────

def test_build_ask_context_has_explicit_portfolio_flag():
    from core.ghost_ask import build_ask_context
    sig = inspect.signature(build_ask_context)
    assert "include_portfolio" in sig.parameters
    assert sig.parameters["include_portfolio"].default is False


def test_public_ask_path_passes_include_portfolio_false():
    import core.ghost_ask as ga
    src = inspect.getsource(ga.ask_ghost)
    assert "build_ask_context(include_portfolio=False)" in src


def test_gated_callers_request_portfolio_context():
    # These two callers are auth-gated and were TypeError-broken by the
    # signature regression — keep them pinned to the kwarg form.
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    for rel in ("api/wolf_endpoints.py", "mcp/ghost_server.py"):
        src = (root / rel).read_text()
        assert "build_ask_context(include_portfolio=True)" in src, rel


# ── P0-1: /api/wolf/ask is auth-gated ────────────────────────────────────

def test_post_ghost_ask_requires_portfolio_auth():
    import api.wolf_endpoints as we
    src = inspect.getsource(we.post_ghost_ask)
    assert "require_portfolio_auth" in src
    # And it degrades to a renderable JSON error, not a bare detail payload.
    assert '"ok": False' in src and "401" in src


# ── P2-2: admin cookie validation fails closed without CRON_SECRET ───────

def test_admin_token_valid_fails_closed_without_secret(monkeypatch):
    import wolf_app
    monkeypatch.delenv("CRON_SECRET", raising=False)
    monkeypatch.delenv("GHOST_DEV_MODE", raising=False)
    assert wolf_app._admin_token_valid("") is False
    assert wolf_app._admin_token_valid("anything.sig") is False


def test_admin_token_valid_dev_mode_is_explicit(monkeypatch):
    import wolf_app
    monkeypatch.delenv("CRON_SECRET", raising=False)
    monkeypatch.setenv("GHOST_DEV_MODE", "1")
    assert wolf_app._admin_token_valid("") is True


# ── P1-1: /api/v2/recent ALL mode is auth-gated ──────────────────────────

def test_v2_recent_all_mode_requires_auth():
    import core.portfolio_routes as pr
    src = inspect.getsource(pr.v2_recent)
    assert "require_portfolio_auth" in src
    # The gate must sit inside the ALL branch, before any query runs.
    assert src.index("require_portfolio_auth") < src.index("db_conn")


# ── P0: resolution accounting failures are logged, never silently dropped ─

def test_record_pick_resolution_failures_are_logged():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    for rel in ("core/prediction.py", "core/watchdog.py"):
        src = (root / rel).read_text()
        for block in _swallow_blocks_around(src, "record_pick_resolution"):
            assert "LOGGER.error" in block, f"{rel}: perf-log failure swallowed silently"


def _swallow_blocks_around(src: str, marker: str):
    """Yield the ~10 lines following each call to `marker` (its except block)."""
    lines = src.splitlines()
    for i, ln in enumerate(lines):
        if marker + "(" in ln and "import" not in ln and "def " not in ln:
            yield "\n".join(lines[i:i + 10])


# ── P0: stats-era watermark has a single source of truth ─────────────────

def test_v32_watermark_centralized():
    from core.prediction_filters import V32_ERA_MIN_ID
    assert V32_ERA_MIN_ID == 223438
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    for rel in ("core/prediction.py", "core/stats_direction.py", "wolf_app.py",
                "api/wolf_endpoints.py", "core/performance_log.py", "core/ghost_ask.py"):
        src = (root / rel).read_text()
        assert "223438" not in src, f"{rel}: hardcoded watermark literal reintroduced"
        assert "V32_ERA_MIN_ID" in src, rel


# ── CORS: origin list is operator-configurable ────────────────────────────

def test_cors_origins_env_knob():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "wolf_app.py").read_text()
    assert "GHOST_CORS_ORIGINS" in src
    assert 'allow_origins=_CORS_ORIGINS' in src


# ── kill-status shows the enforcement window, not just all-time ──────────

def test_kill_status_surfaces_enforcement_window():
    """The all-time dashboard can show triggered=red while the enforcer
    (window reset since last manual resume) correctly stays unpaused — that
    mismatch misread as 'kill switch broken' during the July 3 live review.
    The endpoint must surface the enforcement-window view alongside."""
    import inspect
    import wolf_app
    src = inspect.getsource(wolf_app.wolf_kill_status)
    assert "enforcement_window" in src
    assert "engine_pause_resume_ts" in src
