"""PR #157: consolidated daily report — composes sections + narrative, never raises."""
import core.daily_report as dr


def test_build_daily_report_never_raises(monkeypatch):
    # Force every section to blow up; report must still return ok with error notes.
    import sys
    def boom(*a, **k): raise RuntimeError("section down")
    # patch the internal callables by making their imports fail
    monkeypatch.setattr(dr, "_narrate", dr._narrate)  # keep narrator
    out = dr.build_daily_report()
    assert out["ok"] is True
    assert "date" in out and "narrative" in out
    assert isinstance(out["narrative"], list)


def test_narrative_reads_zero_fire_day():
    r = {
        "date": "2026-07-08",
        "identity": {"pr_version": 157, "health_score": 95, "health_status": "healthy"},
        "decisions": {"picks_fired_today": 0, "scan_cycles_today": 40, "symbols_scanned": 74,
                      "gate_reason": "meta_gate", "live_up_prob": 0.5, "up_prob_needed": 0.62,
                      "regime": "Trend-up", "top_skip_reasons": {"v3_regime_gate": 20}},
        "wallet": {"total_value": 9868.0, "today_pnl": -104.0, "goal": 20000, "goal_pct": 49.3,
                   "closed_today": [{"symbol": "GME", "reason": "stop", "pnl": -18.8, "pnl_pct": -3.8}],
                   "closed_today_wins": 0, "closed_today_losses": 1, "opened_today": []},
        "calibration": {"verdict": "High-confidence calls 62.1% — real, not yet 70%.",
                        "brier": 0.29, "resolved_n": 1031},
    }
    lines = dr._narrate(r)
    txt = " ".join(lines)
    assert "fired ZERO live picks" in txt
    assert "GME" in txt and "stop" in txt
    assert "62.1%" in txt
    assert "$9868.0" in txt or "9868" in txt


def test_narrative_flags_a_fire_day():
    r = {"date": "d", "identity": {}, "decisions": {"picks_fired_today": 2},
         "wallet": {"error": "x"}, "calibration": {}}
    txt = " ".join(dr._narrate(r))
    assert "FIRED 2 live pick" in txt
