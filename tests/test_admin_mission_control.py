"""PR #115 — Mission Control board + Super Ghost panels in the admin console.

Static-content tests in the style of test_ghost_console_ui.py: the admin
console is a single HTML file with inline JS, so we assert the sections,
API wiring, and JS functions exist rather than rendering the page.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADMIN = (ROOT / "admin.html").read_text(encoding="utf-8")


def test_admin_has_mission_control_board():
    assert 'class="sec-label">Mission Control</div>' in ADMIN
    assert 'id="mc-board"' in ADMIN
    assert "function loadMissionControl()" in ADMIN.replace("async function", "function")
    # Board tiles cover the core subsystems
    for label in ("System health", "v3 engine", "Kill switch", "Research mode",
                  "Breakers", "Degraded mode", "Squeeze radar", "Truth ledger"):
        assert label in ADMIN, f"missing mission-control tile: {label}"


def test_admin_mission_control_fetches_expected_apis():
    for url in (
        "/admin/health",
        "/api/research/status",
        "/api/system/breakers",
        "/api/wolf/kill-status",
        "/api/squeeze/picks",
        "/api/wolf/super-ghost/accuracy",
    ):
        assert url in ADMIN, f"mission control missing API call: {url}"


def test_admin_has_super_ghost_section():
    assert 'class="sec-label">Super Ghost</div>' in ADMIN
    for el_id in ("sg-accuracy", "sg-learning", "sg-toppick", "sg-shadow",
                  "sg-promotion", "sg-action-result", "sg-token", "sg-horizon"):
        assert f'id="{el_id}"' in ADMIN, f"missing super-ghost element: {el_id}"
    for url in (
        "/api/wolf/super-ghost/learning",
        "/api/wolf/super-ghost/top-pick-gate",
        "/api/wolf/super-ghost/shadow/models",
        "/api/wolf/super-ghost/promotion",
    ):
        assert url in ADMIN, f"missing super-ghost API call: {url}"


def test_admin_super_ghost_maintenance_uses_mcp_token_not_cron_secret():
    # Maintenance POSTs authenticate with the MCP token header.
    assert "X-Ghost-Mcp-Token" in ADMIN
    for url in (
        "/api/wolf/super-ghost/resolve",
        "/api/wolf/super-ghost/learn",
        "/api/wolf/super-ghost/precision/score",
        "/api/wolf/super-ghost/range-calibration/rebuild",
        "/api/wolf/super-ghost/regime-calibration/rebuild",
        "/api/wolf/super-ghost/shadow/resolve",
    ):
        assert url in ADMIN, f"missing maintenance action: {url}"


def test_admin_has_research_mode_panel():
    assert 'id="research-status"' in ADMIN
    assert "loadResearchStatus" in ADMIN


def test_admin_breakers_panel_uses_rich_endpoint():
    # Upgraded from /api/system/degraded summary to per-breaker status.
    assert "'/api/system/breakers'" in ADMIN
    assert "HALF-OPEN" in ADMIN


def test_admin_polling_pauses_when_tab_hidden():
    assert "document.hidden" in ADMIN


def test_admin_watchlist_count_is_100():
    from config.symbols import OFFICIAL_WATCHLIST
    assert len(OFFICIAL_WATCHLIST) == 100


# ── Mission Control honesty + robustness fixes (forensic audit 2026-07-18) ──

def test_v3_tile_headlines_fireable_not_total_models():
    """MC-1: the v3 tile must headline fireable_now, not the stored-model count.
    After the research tier, total models is inflated by unfireable research
    models; green must mean 'can fire', not merely 'is trained'."""
    assert "fleet_summary" in ADMIN
    assert "fireable_now" in ADMIN
    assert "fireable" in ADMIN and "serveable_research" in ADMIN
    # Green class must be gated on fireable > 0, never on v3.trained alone.
    assert "fireable > 0 ? 'mc-ok' : 'mc-warn'" in ADMIN
    # The old always-green-when-trained headline is gone.
    assert "v3.trained ? models + ' model'" not in ADMIN


def test_mctile_escapes_internally_and_callers_pass_raw():
    """MC-3: escaping is centralized in mcTile; callers must NOT double-escape."""
    # mcTile escapes value + sub itself.
    assert "e(String(value))" in ADMIN and "e(String(sub))" in ADMIN
    # The previously double-escaping caller sites now pass raw strings.
    assert "escHtml(health.status" not in ADMIN
    assert "bOpen.map(escHtml)" not in ADMIN
    assert "'mc-bad', escHtml(String(e))" not in ADMIN


def test_mctick_has_inflight_overlap_guard():
    """MC-4: the 60s refresh must skip a tick while the prior run is in flight."""
    assert "busy" in ADMIN
    assert "Promise.resolve(fn())" in ADMIN


def test_fetchjson_has_abort_timeout():
    """MC-2: _fetchJson must bound the wait so a hung endpoint can't stall a
    Promise.all dashboard batch."""
    GHOST_JS = (ROOT / "static" / "ghost.js").read_text(encoding="utf-8")
    assert "AbortController" in GHOST_JS
    assert "setTimeout" in GHOST_JS and "abort()" in GHOST_JS
