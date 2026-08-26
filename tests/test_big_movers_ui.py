"""Big Movers consumer-page semantics and safety disclosures."""
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "picks.html"


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_big_movers_tab_and_live_loader_exist():
    page = _page()
    assert 'id="tab-bigmovers"' in page
    assert 'id="view-bigmovers"' in page
    assert "load:loadBigMovers" in page
    assert "'/api/big-movers?min_gain_pct=5'" in page
    assert "setInterval(refreshVisibleTab, 60000)" in page


def test_big_movers_uses_deadline_not_exact_hit_claim():
    page = _page()
    assert "Target window ends" in page
    assert "not an exact promised hit date" in page
    lowered = page.lower()
    assert "expected hit date" not in lowered
    assert "guaranteed gain" not in lowered


def test_big_movers_discloses_scope_and_immutable_gain_basis():
    page = _page()
    assert "original issued entry to its original target" in page
    assert "not the entire stock market" in page
    assert "Squeeze and external observations are excluded" in page
    assert "official_live_prediction === true" in page
    assert "item.research_pick === false" in page


def test_big_movers_escapes_provider_text_and_symbol():
    page = _page()
    assert "esc(item.symbol||'—')" in page
    assert "esc(state)" in page
    assert "esc(resp.empty_reason" in page
