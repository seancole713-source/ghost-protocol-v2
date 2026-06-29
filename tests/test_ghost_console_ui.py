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
    # Section routing remains one stable data-section contract.
    assert "data-section=\"top\"" in text
    assert "data-section=\"bullish\"" in text
    assert "data-section=\"h48\"" in text
    assert "data-section=\"week\"" in text
    # PR #91 polish: duplicated top-tab chrome is hidden; sidebar nav is the
    # single visible navigation source.
    assert ".tabs{display:none}" in text
    assert "id=\"topTabs\" aria-hidden=\"true\"" in text


def test_console_contains_live_market_mirror_and_score_language():
    text = _html()
    for phrase in (
        "Open / reference",
        "Low / stop",
        "High / target",
        "Live now",
        "Mirror score",
        "Live market truth",
        "End-of-day mirror",
        "Live market mirror",
    ):
        assert phrase in text
    # The user specifically wanted UP and DOWN both counted as wins when direction is right.
    assert "DOWN call counts as a win when price falls" in text or "DOWN call also counts as a win when price falls" in text
    assert "A win means Ghost got the direction right" in text
    # PR #87: predicted-vs-live mirror must use real session OHLC (open/high/low),
    # not just the spot price, so the user can compare predicted open/low/high to
    # what the live market actually printed.
    assert "m3row" in text and "live_open" in text and "live_low" in text and "live_high" in text


def test_console_contains_pool_management_and_top_pick_gate():
    text = _html()
    assert "Prediction pool" in text
    assert "poolInput" in text
    assert "poolAdd" in text
    assert "Remove" in text
    assert "Top Picks locked" in text
    assert "≥70%" in text or "&ge;70%" in text
    assert "proven directional win rate" in text
    assert "at least 5 completed predictions" in text
    assert "Current completed predictions" in text


def test_console_surfaces_post_falsification_state_outside_health_tab():
    text = _html()
    assert "id=\"killBanner\"" in text
    assert "function renderTrustBanner" in text
    assert "Post-falsification mode: old 80% claim abandoned." in text
    assert "Truth gate active" in text
    assert "stays NO EDGE until coverage, risk, and truth-ledger gates" in text


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
        "/api/market/session/",
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


def test_market_session_endpoint_serves_live_ohlc(monkeypatch):
    """PR #87: /api/market/session/{symbol} exposes real today open/high/low so
    the console mirror can compare predicted open/low/high vs live market truth."""
    import core.prices as prices

    monkeypatch.setattr(prices, "get_intraday_session", lambda sym: {
        "symbol": sym, "price": 45.35, "previous_close": 44.0,
        "session": "rth", "session_label": "Market open", "market_date": "2026-06-29",
        "today_open": 44.5, "today_high": 46.1, "today_low": 43.9,
        "change_abs": 1.35, "change_pct": 3.07, "feed": "alpaca", "as_of_ts": 1782753278,
    })
    client = TestClient(wolf_app.APP)
    r = client.get("/api/market/session/WOLF")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["symbol"] == "WOLF"
    assert d["live_open"] == 44.5
    assert d["live_high"] == 46.1
    assert d["live_low"] == 43.9
    assert d["price"] == 45.35
    assert d["session_label"] == "Market open"


def test_market_session_endpoint_degrades_to_spot(monkeypatch):
    """If intraday OHLC fails, the endpoint still returns a spot-price fallback
    rather than raising, so the console never blanks."""
    import core.prices as prices

    def _boom(sym):
        raise RuntimeError("feed down")

    monkeypatch.setattr(prices, "get_intraday_session", _boom)
    monkeypatch.setattr(prices, "get_price", lambda sym: 12.34)
    client = TestClient(wolf_app.APP)
    r = client.get("/api/market/session/WOLF")
    assert r.status_code == 200
    d = r.json()
    assert d["price"] == 12.34
    assert d["live_open"] is None


def test_console_money_is_null_safe_and_shows_no_intraday_data():
    """PR #92: missing live OHLC must never render as $0.00.

    Root cause of the IQ/LCID '$0.00' artifact was JS Number(null) === 0, so
    money(null) returned '$0.00'. money() must guard null/'' and the mirror row
    must show an explicit 'No intraday data' instead of a fake price.
    """
    text = _html()
    assert "function money(v){if(v==null||v==='')return '—'" in text
    assert "No intraday data" in text


def test_console_explains_coverage_ab_gate_in_overview():
    """PR #92: Overview coverage metric must explain the >=18/25 A/B-grade gate,
    not just show a bare 21/25 count."""
    text = _html()
    assert "mCoverageNote" in text
    assert "A/B-grade evidence gate" in text or "A/B-grade gate" in text
    assert "min_for_ab_grade" in text


def test_console_has_favicon_link():
    """PR #92: page declares a favicon so the browser's /favicon.ico request
    resolves instead of 404ing."""
    text = _html()
    assert "rel=\"icon\"" in text


def test_favicon_route_serves_icon():
    """PR #92: /favicon.ico (and /favicon.svg) return a real icon, not 404."""
    client = TestClient(wolf_app.APP)
    for path in ("/favicon.ico", "/favicon.svg"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "svg" in r.headers.get("content-type", "").lower()
        assert b"<svg" in r.content
