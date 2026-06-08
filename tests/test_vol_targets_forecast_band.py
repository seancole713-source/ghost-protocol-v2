"""Phase 4 — vol-aware forecast band width (scorecard telemetry only)."""
from core.vol_targets import base_vol_pct, forecast_band_vol_pct, median_realized_range_pct
from core.daily_forecast_scorecard import forecast_ohlc_from_prob


def _bars(closes, spread_pct=0.02):
    out = []
    for c in closes:
        c = float(c)
        half = c * spread_pct / 2
        out.append({"open": c, "high": c + half, "low": c - half, "close": c})
    return out


def test_median_realized_range_pct():
    rows = _bars([100] * 10, spread_pct=0.10)
    med = median_realized_range_pct(rows, 10)
    assert med is not None
    assert med == 0.10


def test_forecast_band_stays_at_base_for_quiet_symbol():
    rows = _bars([100] * 12, spread_pct=0.015)
    info = forecast_band_vol_pct("WOLF", "stock", rows)
    assert info["source"] == "base"
    assert info["vol_pct"] == base_vol_pct("WOLF", "stock")


def test_forecast_band_widens_for_high_range_meme():
    rows = _bars([4.0 + i * 0.01 for i in range(15)], spread_pct=0.12)
    info = forecast_band_vol_pct("SPCE", "stock", rows)
    assert info["source"] in ("realized_range", "realized_range_capped")
    assert info["vol_pct"] > base_vol_pct("SPCE", "stock")
    assert info["realized_range_pct"] is not None
    assert info["realized_range_pct"] > 0.10


def test_forecast_ohlc_uses_widened_band_vol():
    rows = _bars([5.0] * 12, spread_pct=0.10)
    band = forecast_band_vol_pct("SPCE", "stock", rows)
    tight = forecast_ohlc_from_prob(5.0, 0.30, "SPCE", "stock")
    wide = forecast_ohlc_from_prob(5.0, 0.30, "SPCE", "stock", band_vol=band)
    assert wide["band_vol_pct"] > tight["band_vol_pct"]
    assert wide["high"] - wide["low"] > tight["high"] - tight["low"]
