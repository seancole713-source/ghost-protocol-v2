"""Consumer UI must keep squeeze alerts and outcomes semantically honest."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PICKS = (ROOT / "picks.html").read_text(encoding="utf-8")


def test_squeeze_alert_uses_exact_alert_only_copy():
    assert "unusual volume on '+alertSymbol+', worth a look." in PICKS
    assert "p.reason" not in PICKS
    assert "p.note" not in PICKS


def test_active_cards_load_immutable_prediction_checklist():
    today_source = PICKS[PICKS.index("function loadToday") : PICKS.index("async function loadMyStocks")]
    assert "/api/ghost/checklist/prediction/" in today_source
    assert "getJson('/api/ghost/checklist/'+encodeURIComponent(p.symbol)" not in today_source


def test_squeeze_requires_fresh_successful_active_scan_only():
    today_source = PICKS[PICKS.index("function loadToday") : PICKS.index("async function loadMyStocks")]
    assert "squeeze.enabled === true" in today_source
    assert "squeeze.radar_active === true" in today_source
    assert "squeeze.scan_ok === true" in today_source
    assert "squeeze.snapshot_stale !== true" in today_source
    assert "scanAgeMs >= 0 && scanAgeMs < 300000" in today_source
    assert "anyProvenPick" not in today_source
    assert "var found =" not in today_source
    assert "Active directional picks" in today_source
    assert "Today's picks" not in today_source


def test_safety_uncertainty_and_refresh_are_visible():
    assert "Safety status unknown" in PICKS
    assert "New-pick availability cannot be confirmed" in PICKS
    assert PICKS.count("Unknown — treat as unavailable") >= 3
    assert "setInterval(refreshVisibleTab, 60000)" in PICKS
    assert "visibilitychange" in PICKS
    assert "guardedLoad('today'" in PICKS
    assert "guardedLoad('system'" in PICKS


def test_record_scope_is_disclosed():
    assert "Last 25 finished calls" in PICKS
    assert "most recent 25 finished calls" in PICKS


def test_record_distinguishes_expired_from_stop_loss():
    assert "var expiredCall = s.outcome === 'EXPIRED';" in PICKS
    assert "Expired." in PICKS
    assert "reached neither target nor get-out price before the watch window ended" in PICKS
    assert "hit the get-out price" in PICKS
