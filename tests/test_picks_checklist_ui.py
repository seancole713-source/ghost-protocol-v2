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
    assert "Approved core picks" in today_source
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


def test_model_readiness_is_not_inferred_from_pause_latch():
    assert "function modelReadiness(models)" in PICKS
    assert "models.fleet_summary.fireable_now" in PICKS
    assert "No approved models" in PICKS
    assert "Model readiness unavailable" in PICKS
    assert "paused?'Safety-paused':'Available'" not in PICKS
    assert '<span class="val ok">Available</span>' not in PICKS
    assert '/console#research' in PICKS


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


def _today_source():
    return PICKS[PICKS.index("function scanCoverage") : PICKS.index("async function loadMyStocks")]


def test_fresh_scan_with_degraded_coverage_is_not_a_clean_no():
    """P2 audit: a fresh scan with 6 of 105 usable symbols must not read
    'no alerts across 105 symbols'."""
    src = _today_source()
    assert "squeeze.usable_symbols" in src and "squeeze.fetch_ok" in src
    assert "squeeze.scanned_symbols" in src and "squeeze.symbols" in src
    assert "squeeze.coverage_degraded" in src
    assert "squeezeFresh && coverage.degraded" in src
    assert "Insufficient coverage <span class=\"cov\">('+esc(coverageText(coverage))+')</span>" in src
    assert "' symbol'+(cov.scanned===1?'':'s')+' usable'" in src
    assert "No alert is not evidence that nothing is moving" in src
    # The old copy claimed full coverage from the scanned-symbol count.
    assert "no unusual-volume alerts across '+Number(squeeze.symbols" not in src
    # Missing counts are treated as degraded, never as full coverage.
    assert "if(!known) return {known:false, usable:null, scanned:null, degraded:true};" in src


def test_today_presents_three_separately_labelled_sections():
    src = _today_source()
    approved = src.index("<h2>Approved core picks</h2>")
    experimental = src.index("<h2>Experimental / paper forecasts</h2>")
    radar = src.index("<h2>Unvalidated radar observations</h2>")
    assert approved < experimental < radar
    # Approved requires an explicit live core engine and a non-research row.
    assert (
        "var approved = coreResearch ? [] : enriched.filter(function(e){ "
        "return e.pick.research_pick !== true; });"
    ) in src
    assert "None. The core engine is research-only, so no pick is approved." in src
    # Experimental reads the existing public, read-only research endpoint
    # (already used by /console#research), never an admin route.
    assert "getJson('/api/forecasts/research?limit=5')" in src
    assert "research.trading_eligible === false" in src
    assert "f.trading_eligible === false" in src
    assert "not evidence that there are zero forecasts" in src
    assert "/api/admin" not in src
    # Research calls and radar rows sit under their own sections.
    assert src.index("Core v3 research calls") > experimental
    assert src.index("Fresh unusual activity") > radar
    assert src.index("Externally discovered activity — observed by Ghost") > radar


def test_today_escapes_inserted_text():
    src = _today_source()
    for needle in (
        "esc(f.symbol||'—')", "esc(f.direction||'Unknown')", "esc(f.id)",
        "esc(coverageText(coverage))", "esc(item.symbol||'—')", "esc(p.symbol||'')",
    ):
        assert needle in src, needle
    exp = src[src.index("<h2>Experimental / paper forecasts</h2>") : src.index("<h2>Unvalidated radar observations</h2>")]
    # Research forecast prices go through money() (numeric), never raw text.
    assert "money(f.entry_reference)" in exp
    assert "'+f." not in exp


def test_muted_and_warning_tokens_meet_wcag_aa():
    """P2 audit: muted text and warning colors must be >=4.5:1 on every
    background they are drawn on, in light and dark themes."""
    import re

    def lum(h):
        h = h.lstrip("#")
        rgb = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    def ratio(a, b):
        hi, lo = sorted((lum(a), lum(b)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    css = PICKS[: PICKS.index("</style>")]
    blocks = re.findall(r"--ground:[^}]*", css)
    assert len(blocks) == 3  # light, prefers-dark, forced-dark
    for block in blocks:
        tokens = dict(re.findall(r"--([a-z0-9-]+):(#[0-9A-Fa-f]{6})", block))
        for fg in ("ink-2", "ink-3", "warn"):
            for bg in ("ground", "surface", "surface-2", "warn-soft"):
                assert ratio(tokens[fg], tokens[bg]) >= 4.5, (fg, bg, tokens[fg], tokens[bg])


def test_unusual_volume_is_not_called_a_squeeze():
    """I01: fresh unusual-volume alerts are unvalidated radar. With no
    short-covering evidence in the payload, the verdict must never say "Yes"
    (squeeze found); no-activity, insufficient-coverage and unknown stay
    distinct."""
    src = _today_source()
    verdict = src[src.index("Did Ghost find a squeeze today?") : src.index("if(paused === null){")]
    assert "Yes." not in verdict
    assert 'class="a yes"' not in verdict
    assert "<p class=\"a mid\">Unusual activity detected</p>" in verdict
    assert "Unvalidated radar, not a confirmed squeeze" in verdict
    assert "Insufficient coverage <span class=\"cov\">" in verdict
    assert "<p class=\"a no\">No unusual activity.</p>" in verdict
    assert "<p class=\"a mid\">Unknown.</p>" in verdict
    # A clean "no activity" requires a fresh scan with adequate coverage.
    assert verdict.index("squeezeFresh && coverage.degraded") < verdict.index("No unusual activity.")
    assert "squeeze confirmed" not in verdict.lower()
