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
    today_source = PICKS[PICKS.index("async function loadToday") : PICKS.index("async function loadMyStocks")]
    assert "/api/ghost/checklist/prediction/" in today_source
    assert "getJson('/api/ghost/checklist/'+encodeURIComponent(p.symbol)" not in today_source


def test_record_distinguishes_expired_from_stop_loss():
    assert "var expiredCall = s.outcome === 'EXPIRED';" in PICKS
    assert "Expired." in PICKS
    assert "reached neither target nor get-out price before the watch window ended" in PICKS
    assert "hit the get-out price" in PICKS
