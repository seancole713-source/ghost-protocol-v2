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
    assert "Status unknown" in PICKS
    assert "New directional-pick availability cannot be confirmed" in PICKS
    assert PICKS.count("Unknown — treat as unavailable") >= 3
    assert "setInterval(refreshVisibleTab, 60000)" in PICKS
    assert "visibilitychange" in PICKS
    assert "guardedLoad('today'" in PICKS
    assert "guardedLoad('system'" in PICKS


def test_pause_copy_separates_issuance_from_active_analysis():
    assert "Official directional-pick issuance" in PICKS
    assert "Safety-paused" in PICKS
    assert "This blocks only new official directional picks" in PICKS
    assert "Analysis, squeeze scanning, monitoring, and learning remain active" in PICKS
    assert "Analysis and monitoring" in PICKS
    assert "Only new official directional picks are blocked; the rest of Ghost remains active" in PICKS
    assert "brier->degrade_watching" not in PICKS
    assert "Ghost has been wrong too often recently, so it shut itself off" not in PICKS


def test_pause_reason_and_header_are_user_facing():
    assert "function pauseExplanation(kill)" in PICKS
    assert "Recent probability calibration is outside Ghost\\'s reliability limit" in PICKS
    assert "Brier score" in PICKS
    assert "App online" in PICKS
    assert "App offline" in PICKS
    assert ">Live<" not in PICKS


def test_missing_prices_and_wallet_scope_are_explicit():
    assert "Live price unavailable" in PICKS
    assert "Separate paper wallet" in PICKS
    assert "Simulated balance, separate from the finished-call count above" in PICKS


def test_external_fonts_are_not_blocked_by_page_csp():
    assert "fonts.googleapis.com" not in PICKS
    assert "fonts.gstatic.com" not in PICKS


def test_external_discovery_is_visible_but_never_called_a_prediction():
    today_source = PICKS[PICKS.index("function loadToday") : PICKS.index("async function loadMyStocks")]
    assert "squeeze.external_discovery" in today_source
    assert "squeeze.external_radar" in today_source
    assert "Externally discovered activity — observed by Ghost" in today_source
    assert "External source coverage — advisory only" in today_source
    assert "not a prediction, candidate, alert, trade recommendation" in today_source
    assert "cannot trigger candidates, alerts, outcomes, or wallet entries" in today_source
    assert "decision eligible: no" in today_source
    assert "item.advisory_only === true" in today_source
    assert "item.decision_eligible === false" in today_source
    assert "externalRows.sort" in today_source
    assert "externalRadarRows.sort" in today_source
    assert "Number(b.move_pct||0) - Number(a.move_pct||0)" in today_source
    for prohibited in ("confidence_pct", "item.buy", "item.sell", "item.stop"):
        assert prohibited not in today_source[today_source.index("if(externalRadarRows.length)") : today_source.index("if(externalRows.length)")]


def test_record_scope_is_disclosed():
    assert "Last 25 finished calls" in PICKS
    assert "most recent 25 finished calls" in PICKS


def test_record_distinguishes_expired_from_stop_loss():
    assert "var expiredCall = s.outcome === 'EXPIRED';" in PICKS
    assert "Expired." in PICKS
    assert "reached neither target nor get-out price before the watch window ended" in PICKS
    assert "hit the get-out price" in PICKS
