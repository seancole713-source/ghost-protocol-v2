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

    # Setup: fuel present, no move.
    s = sh.classify_stage(
        fuel=60, trigger=0, confirmation=0,
        move_pct=0, rvol=1.0, breakout_pct=0,
    )
    assert s["stage"] == "setup"

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
