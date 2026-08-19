"""Tests for core/squeeze_hunter.py — pure scoring, no network I/O."""
from core import squeeze_hunter as sh


def test_high_short_interest_alone_cannot_dominate():
    """High SI alone must never produce a high pressure score (spec §2)."""
    fuel = sh.score_fuel({"short_float_pct": 60.0, "days_to_cover": 0.0})
    # SI capped at 40 pts; no other fuel → fuel stays modest.
    assert fuel <= 40.0
    # With weak trigger + confirmation, composite is capped at 50.
    pressure = sh.squeeze_pressure_score(fuel, 0.0, 0.0)
    assert pressure <= 50.0


def test_pressure_requires_multiple_conditions():
    """Fuel + trigger + confirmation all needed for a high score."""
    fuel = sh.score_fuel({
        "short_float_pct": 40.0,
        "days_to_cover": 6.0,
        "float_shares": 5_000_000,
        "institutional_ownership_pct": 15.0,
        "short_interest_change_pct": 20.0,
    })
    trigger = sh.score_trigger({
        "catalyst_score": 90.0,
        "earnings_surprise": 85.0,
        "premarket_gap_pct": 8.0,
        "rvol": 4.5,
        "call_volume_score": 80.0,
        "breakout_pct": 3.0,
    })
    confirmation = sh.score_confirmation({"breakout_pct": 5.0, "rvol": 4.0, "above_vwap": True})
    pressure = sh.squeeze_pressure_score(fuel, trigger, confirmation)
    assert pressure > 70.0


def test_pressure_bands():
    assert sh.pressure_band(10)["band"] == "low"
    assert sh.pressure_band(40)["band"] == "watch"
    assert sh.pressure_band(60)["band"] == "elevated"
    assert sh.pressure_band(80)["band"] == "high"
    assert sh.pressure_band(95)["band"] == "extreme"


def test_stage_classification_order():
    """Later stages win over earlier ones (spec §8)."""
    # Exhaustion: parabolic + declining momentum + huge volume.
    s = sh.classify_stage(
        fuel=80, trigger=80, confirmation=80,
        move_pct=30, rvol=5.0, breakout_pct=20,
        momentum_declining=True, price_parabolic=True, huge_volume=True,
    )
    assert s["stage"] == "exhaustion"

    # Reversal: declining momentum + negative move.
    s = sh.classify_stage(
        fuel=80, trigger=80, confirmation=80,
        move_pct=-5, rvol=2.0, breakout_pct=0,
        momentum_declining=True,
    )
    assert s["stage"] == "reversal"

    # Fuel alone is not a setup; an independent trigger is required.
    s = sh.classify_stage(
        fuel=60, trigger=20, confirmation=0,
        move_pct=0, rvol=1.0, breakout_pct=0,
    )
    assert s["stage"] == "setup"

    s = sh.classify_stage(
        fuel=60, trigger=0, confirmation=0,
        move_pct=0, rvol=1.0, breakout_pct=0,
    )
    assert s["stage"] == "none"

    # None: no fuel, no move.
    s = sh.classify_stage(
        fuel=10, trigger=0, confirmation=0,
        move_pct=0, rvol=1.0, breakout_pct=0,
    )
    assert s["stage"] == "none"


def test_explosion_projection_is_honest():
    proj = sh.explosion_projection(94.0)
    assert "disclaimer" in proj
    assert "not guaranteed" in proj["disclaimer"].lower() or "NOT guaranteed" in proj["disclaimer"]
    # Higher score → higher upside, lower downside.
    high = sh.explosion_projection(94.0)
    low = sh.explosion_projection(40.0)
    assert high["p_plus_20_pct"] > low["p_plus_20_pct"]
    assert high["p_minus_20_pct"] < low["p_minus_20_pct"]


def test_build_explosion_report_shape():
    rep = sh.build_explosion_report(
        symbol="HTZ",
        short_ctx={"short_float_pct": 34.0, "days_to_cover": 5.8},
        trigger_ctx={"catalyst_score": 90.0, "earnings_surprise": 85.0, "premarket_gap_pct": 8.0, "rvol": 4.5, "call_volume_score": 80.0},
        confirm_ctx={"breakout_pct": 5.0, "rvol": 4.0, "above_vwap": True},
        factors={"short_squeeze_potential": 95, "catalyst": 93, "earnings_surprise": 91, "relative_volume": 97, "technical_breakout": 88, "options_activity": 89, "float_structure": 82, "market_environment": 71},
        move_pct=10.0, rvol=4.5, breakout_pct=5.0,
    )
    assert rep["symbol"] == "HTZ"
    for k in ("fuel_score", "trigger_score", "confirmation_score", "squeeze_pressure_score", "stage", "explosion_score", "projection"):
        assert k in rep
    assert rep["explosion_score"] > 80.0


def test_fuel_reads_free_fields():
    fuel = sh.score_fuel({
        "short_float_pct": 30.0,
        "days_to_cover": 4.0,
        "float_shares": 8_000_000,
        "institutional_ownership_pct": 25.0,
        "short_interest_change_pct": 15.0,
    })
    # SI 30 + dtc 16 + float 15 + inst 7 + change 3 = 71
    assert fuel == 71.0


# ── Review-fix regressions ─────────────────────────────────────────────────

def test_catalyst_unavailable_maps_to_zero_not_fifty():
    """P1: missing catalyst data must NOT fabricate a neutral 50/100 signal."""
    out = sh._catalyst_to_trigger({"available": False})
    assert out["catalyst_score"] == 0.0
    assert out["guidance_score"] == 0.0
    assert out["catalyst_available"] is False


def test_guidance_not_mislabeled_as_earnings_surprise():
    """P2: guidance must be reported under guidance_score, not earnings_surprise."""
    out = sh._catalyst_to_trigger({
        "available": True,
        "catalyst_score": 0.5,
        "guidance_momentum_score": 0.2,
    })
    assert "guidance_score" in out
    assert "earnings_surprise" not in out
    assert out["guidance_score"] == 60.0  # 50 + 0.2*50


def test_momentum_without_fuel_is_not_squeeze():
    """P2: high price/RVOL with zero fuel must NOT be labeled Stage 4 Squeeze."""
    s = sh.classify_stage(
        fuel=0, trigger=0, confirmation=0,
        move_pct=10, rvol=4.0, breakout_pct=6,
    )
    assert s["stage"] != "squeeze"


def test_squeeze_requires_fuel():
    """Squeeze stage requires short-interest fuel (spec §5)."""
    s = sh.classify_stage(
        fuel=60, trigger=60, confirmation=60,
        move_pct=10, rvol=4.0, breakout_pct=6,
    )
    assert s["stage"] == "squeeze"


def test_projection_is_not_calibrated():
    """P2: projection must be flagged as NOT statistically calibrated."""
    proj = sh.explosion_projection(80.0)
    assert proj["calibrated"] is False
    assert "NOT statistically calibrated" in proj["disclaimer"]


def test_public_report_withholds_uncalibrated_percentages():
    public = sh.public_hunter_report({"projection": sh.explosion_projection(80.0)})

    assert public["projection"]["calibrated"] is False
    assert public["projection"]["publication_status"] == "withheld_pending_calibration"
    assert not any(key.endswith("_pct") for key in public["projection"])


def test_reference_quote_requires_true_observation_timestamp():
    out = sh.validate_reference_quote(
        {"price": 12.5, "session": "afterhours", "market_date": "2026-08-03"},
        issued_ts=1_000,
    )
    assert out["valid"] is False
    assert "missing_price_timestamp" in out["reasons"]


def test_reference_quote_accepts_fresh_post_close_observation():
    out = sh.validate_reference_quote(
        {
            "price": 12.5,
            "price_as_of_ts": 990,
            "session": "afterhours",
            "market_date": "2026-08-03",
            "data_stale": False,
            "cache_age_s": 10,
        },
        issued_ts=1_000,
    )
    assert out["valid"] is True
    assert out["price"] == 12.5
    assert out["price_as_of_ts"] == 990


def test_reference_quote_rejects_stale_or_incompatible_session():
    out = sh.validate_reference_quote(
        {
            "price": 12.5,
            "price_as_of_ts": 1_000,
            "session": "premarket",
            "market_date": "2026-08-03",
            "data_stale": True,
        },
        issued_ts=3_000,
    )
    assert out["valid"] is False
    assert "stale_price" in out["reasons"]
    assert "provider_marked_stale" in out["reasons"]
    assert "incompatible_session" in out["reasons"]


def test_hunter_planning_levels_require_confirmed_quote():
    validation = sh.validate_reference_quote(
        {
            "price": 100.0,
            "price_as_of_ts": 990,
            "session": "rth",
            "market_date": "2026-08-03",
            "data_stale": False,
        },
        issued_ts=1_000,
    )
    plan = sh.build_hunter_planning_levels("HTZ", validation)
    assert plan["status"] == "available"
    assert plan["evidence_status"] == "CONFIRMED"
    assert plan["stop_price"] < plan["entry_price"] < plan["target_price"]
    assert plan["entry_price"] == 100.0
    assert plan["as_of_ts"] == 990
    assert plan["purpose"] == "planning_only"
    assert "not a fired Ghost pick" in plan["disclaimer"]


def test_hunter_planning_levels_fail_closed_without_quote():
    validation = sh.validate_reference_quote(
        {
            "price": 100.0,
            "price_as_of_ts": 1_000,
            "session": "premarket",
            "market_date": "2026-08-03",
        },
        issued_ts=1_000,
    )
    plan = sh.build_hunter_planning_levels("HTZ", validation)
    assert plan["status"] == "unavailable"
    assert plan["evidence_status"] == "UNVERIFIED"
    assert plan["entry_price"] is None
    assert plan["target_price"] is None
    assert plan["stop_price"] is None
    assert "incompatible_session" in plan["reasons"]


def test_market_environment_uses_vix():
    """P2: market environment must reflect real VIX, not a constant 50."""
    assert sh.market_environment_score({"label": "risk_off_high_volatility"}) == 20.0
    assert sh.market_environment_score({"label": "calm_risk_on"}) == 80.0
    assert sh.market_environment_score({"label": "risk_on"}) == 80.0
    assert sh.market_environment_score({"label": "mixed"}) == 55.0
    assert sh.market_environment_score({"label": "risk_off"}) == 35.0
    assert sh.market_environment_score(None) is None
    assert sh.market_environment_score({"label": "unknown"}) is None


def test_unknown_market_environment_adds_no_score_credit():
    factors = {name: 0.0 for name in sh.EXPLOSION_FACTORS}
    factors["market_environment"] = sh.market_environment_score(None)

    assert sh.explosion_score(factors) == 0.0


def test_hunter_qualification_requires_squeeze_fuel_and_independent_evidence():
    momentum_only = {
        "stage": "confirmation",
        "fuel_score": 0,
        "trigger_score": 20,
        "confirmation_score": 70,
    }
    fuel_only = {
        "stage": "setup",
        "fuel_score": 55,
        "trigger_score": 0,
        "confirmation_score": 0,
    }
    complete_setup = {
        "stage": "setup",
        "fuel_score": 55,
        "trigger_score": 20,
        "confirmation_score": 0,
    }

    assert sh.qualifies_hunter_setup(momentum_only) is False
    assert sh.qualifies_hunter_setup(fuel_only) is False
    assert sh.qualifies_hunter_setup(complete_setup) is True


def test_scheduled_scan_uses_cached_batched_inputs(monkeypatch):
    calls = []

    monkeypatch.setattr(
        sh,
        "_batched_market_context",
        lambda symbols: {"HTZ": {"price": 10, "rvol": 3, "current_move_pct": 5}},
    )
    monkeypatch.setattr(sh, "_fetch_market_regime", lambda: None)

    def _report(symbol, **kwargs):
        calls.append((symbol, kwargs))
        return {
            "ok": True,
            "symbol": symbol,
            "qualified": False,
            "stage": "none",
            "explosion_score": 12.0,
            "planning_levels": {"evidence_status": "UNVERIFIED"},
            "evidence_coverage": {
                "sources": {"short_interest": False},
            },
        }

    monkeypatch.setattr(sh, "fetch_explosion_report", _report)
    result = sh.scan_watchlist(symbols=["HTZ"], limit=5)

    assert result["ok"] is True
    assert result["candidates"] == []
    assert result["watchlist"][0]["symbol"] == "HTZ"
    assert calls[0][1]["cached_only"] is True
    assert calls[0][1]["market_metrics"]["rvol"] == 3


def test_snapshot_reader_never_rebuilds(monkeypatch):
    monkeypatch.setattr(
        sh,
        "_hunter_snapshot",
        {
            "ok": True,
            "generated_at_ts": int(sh.time.time()),
            "candidates": [{"symbol": "A"}, {"symbol": "B"}],
            "watchlist": [{"symbol": "C"}],
        },
    )
    monkeypatch.setattr(
        sh,
        "scan_watchlist",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not scan")),
    )

    result = sh.get_hunter_snapshot(limit=1)

    assert result["candidates"] == [{"symbol": "A"}]
    assert result["watchlist"] == [{"symbol": "C"}]
    assert result["snapshot_stale"] is False


def test_symbol_snapshot_reads_private_board_rows(monkeypatch):
    monkeypatch.setattr(
        sh,
        "_hunter_snapshot",
        {
            "ok": True,
            "generated_at_ts": int(sh.time.time()),
            "_rows": [{"ok": True, "symbol": "HTZ", "explosion_score": 55}],
            "candidates": [],
            "watchlist": [],
        },
    )

    result = sh.get_hunter_symbol_snapshot("htz")

    assert result["ok"] is True
    assert result["symbol"] == "HTZ"
    assert result["snapshot_age_s"] >= 0
