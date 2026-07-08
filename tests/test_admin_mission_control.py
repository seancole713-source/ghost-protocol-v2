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


def test_admin_watchlist_count_is_45():
    from config.symbols import OFFICIAL_WATCHLIST
    assert len(OFFICIAL_WATCHLIST) == 45
