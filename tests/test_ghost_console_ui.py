"""PR #86 unified Liquid Glass UI contract tests.

The user requested a full UI redesign that merges the current Ghost Picks and
full dashboard surfaces into one clean sidebar console. These tests keep the
static page and routes from regressing.
"""
from pathlib import Path

from fastapi.testclient import TestClient

import wolf_app

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "ghost_console.html"


def _html() -> str:
    return HTML.read_text(encoding="utf-8")


def test_console_file_exists_and_uses_liquid_glass_material():
    text = _html()
    assert "Unified Prediction Console" in text
    # The glass material foundation: translucent background + blur + saturation.
    assert ".glass" in text
    assert "backdrop-filter:blur" in text or "backdrop-filter: blur" in text
    assert "saturate(190%)" in text or "saturate(180%)" in text
    assert "rgba(" in text
    # Floating chrome layers.
    assert "position:sticky" in text
    assert "glass-clear" in text


def test_console_contains_required_sidebar_and_prediction_tabs():
    text = _html()
    for label in (
        "Top stocks",
        "Bullish",
        "Today",
        "48 hour",
        "This week",
        "Live mirror",
        "Health",
    ):
        assert label in text
    # Top horizontal tabs mirror the sidebar.
    assert "data-section=\"top\"" in text
    assert "data-section=\"bullish\"" in text
    assert "data-section=\"h48\"" in text
    assert "data-section=\"week\"" in text


def test_console_contains_live_market_mirror_and_score_language():
    text = _html()
    for phrase in (
        "Prediction open/ref",
        "Live now",
        "Predicted low / stop",
        "Predicted high / target",
        "Mirror",
        "End-of-day mirror",
        "Live market mirror",
    ):
        assert phrase in text
    # The user specifically wanted UP and DOWN both counted as wins when direction is right.
    assert "DOWN predictions count as wins when price falls" in text
    assert "Win means Ghost got direction correct" in text


def test_console_contains_pool_management_and_top_pick_gate():
    text = _html()
    assert "Prediction pool" in text
    assert "poolInput" in text
    assert "poolAdd" in text
    assert "Remove" in text
    assert "Top Picks locked" in text
    assert "≥70%" in text or "&ge;70%" in text
    assert "proven directional win rate" in text


def test_console_fetches_required_existing_and_new_apis():
    text = _html()
    for endpoint in (
        "/api/_version",
        "/health",
        "/api/health",
        "/api/system/degraded",
        "/api/wolf/kill-status",
        "/api/wolf/super-ghost?symbol=",
        "/api/wolf/super-ghost/history?symbol=",
        "/api/wolf/super-ghost/accuracy?symbol=",
        "/api/wolf/super-ghost/if-followed?symbol=",
        "/api/picks?limit=50",
        "/api/squeeze/picks",
        "/api/squeeze/daily-log?days=7",
        "/api/price/",
    ):
        assert endpoint in text


def test_console_routes_serve_new_and_legacy_pages():
    client = TestClient(wolf_app.APP)
    root = client.get("/")
    picks = client.get("/picks")
    legacy = client.get("/legacy-picks")
    cockpit = client.get("/cockpit")
    assert root.status_code == 200
    assert picks.status_code == 200
    assert legacy.status_code == 200
    assert cockpit.status_code == 200
    assert "Prediction command center" in root.text
    assert "Prediction command center" in picks.text
    assert "Ghost Picks" in legacy.text
    assert "WOLF Command Center" in cockpit.text


def test_console_inline_javascript_has_required_functions():
    text = _html()
    for fn in (
        "function renderMirror",
        "function mirrorScore",
        "function addPool",
        "function removePool",
        "function logPrediction",
        "function healthHtml",
    ):
        assert fn in text
