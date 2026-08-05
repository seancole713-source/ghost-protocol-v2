"""Tests for core/research_resolvers.py — task-specific outcome resolvers."""
import pytest
from core.research_resolvers import (
    ResearchResolution,
    resolve_tp_sl_swing,
    resolve_intraday_continuation,
    resolve_volatility_expansion,
    resolve_cross_sectional_ranking,
    resolve_event_reaction,
    resolve_pending_tp_sl_prediction,
    get_resolver,
)


# ── ResearchResolution invariants ───────────────────────────────────────────

def test_resolution_valid():
    r = ResearchResolution(outcome="WIN", observed_value=3.5, resolved_ts=1000, available_ts=1000)
    assert r.outcome == "WIN"


def test_resolution_rejects_invalid_outcome():
    with pytest.raises(ValueError, match="Invalid outcome"):
        ResearchResolution(outcome="MAYBE", observed_value=None, resolved_ts=1000, available_ts=1000)


def test_resolution_rejects_non_positive_ts():
    with pytest.raises(ValueError):
        ResearchResolution(outcome="WIN", observed_value=None, resolved_ts=0, available_ts=1000)


def test_resolution_is_frozen():
    r = ResearchResolution(outcome="WIN", observed_value=None, resolved_ts=1000, available_ts=1000)
    with pytest.raises(Exception):
        r.outcome = "LOSS"  # type: ignore


# ── resolver registry ──────────────────────────────────────────────────────

def test_all_v1_resolvers_registered():
    for rid in ("tp_sl_bar_path/v1", "intraday_continuation/v1",
                "volatility_expansion/v1", "cross_sectional_ranking/v1",
                "event_reaction/v1"):
        assert get_resolver(rid, "1.0.0") is not None


def test_get_resolver_missing():
    assert get_resolver("nonexistent/v1", "1.0.0") is None


# ── TP/SL resolver ─────────────────────────────────────────────────────────

def _daily_bars(prices, base_ts=1_720_000_000):
    """Build daily OHLC bars from a list of (high, low, close) tuples."""
    bars = []
    for i, (high, low, close) in enumerate(prices):
        bars.append({
            "ts": base_ts + i * 86400,
            "open": close,
            "high": high,
            "low": low,
            "close": close,
        })
    return bars


def test_tp_sl_swing_win():
    bars = _daily_bars([(105, 99, 103), (110, 102, 108), (115, 107, 112)])
    result = resolve_tp_sl_swing(
        symbol="WOLF", direction="UP", entry_price=100, target_price=110,
        stop_price=95, predicted_at=1_720_000_000 - 3600, hold_bars=3,
        daily_bars=bars,
    )
    assert result is not None
    assert result.outcome == "WIN"


def test_tp_sl_swing_loss():
    bars = _daily_bars([(102, 94, 98), (103, 90, 95), (100, 88, 92)])
    result = resolve_tp_sl_swing(
        symbol="WOLF", direction="UP", entry_price=100, target_price=110,
        stop_price=95, predicted_at=1_720_000_000 - 3600, hold_bars=3,
        daily_bars=bars,
    )
    assert result is not None
    assert result.outcome == "LOSS"


def test_tp_sl_swing_same_bar_collision_is_loss():
    """Same-bar target+stop collision is LOSS (conservative)."""
    # Use a predicted_at that is clearly on an earlier date than the bar
    bars = _daily_bars([(115, 90, 105)], base_ts=1_720_000_000 + 86400)
    result = resolve_tp_sl_swing(
        symbol="WOLF", direction="UP", entry_price=100, target_price=110,
        stop_price=95, predicted_at=1_720_000_000, hold_bars=1,
        daily_bars=bars,
    )
    assert result is not None
    assert result.outcome == "LOSS"


def test_tp_sl_swing_expired():
    """No touch after complete horizon is EXPIRED."""
    bars = _daily_bars([(104, 98, 102), (104, 99, 103), (105, 100, 104)],
                       base_ts=1_720_000_000 + 86400)
    result = resolve_tp_sl_swing(
        symbol="WOLF", direction="UP", entry_price=100, target_price=120,
        stop_price=90, predicted_at=1_720_000_000, hold_bars=3,
        daily_bars=bars,
    )
    assert result is not None
    assert result.outcome == "EXPIRED"


def test_tp_sl_swing_not_yet_mature():
    """Incomplete horizon returns None."""
    bars = _daily_bars([(104, 98, 102)])  # only 1 bar, need 3
    result = resolve_tp_sl_swing(
        symbol="WOLF", direction="UP", entry_price=100, target_price=120,
        stop_price=90, predicted_at=1_720_000_000 - 3600, hold_bars=3,
        daily_bars=bars,
    )
    assert result is None


def test_tp_sl_swing_down_direction():
    bars = _daily_bars([(98, 90, 95), (95, 85, 90), (92, 82, 88)])
    result = resolve_tp_sl_swing(
        symbol="WOLF", direction="DOWN", entry_price=100, target_price=88,
        stop_price=105, predicted_at=1_720_000_000 - 3600, hold_bars=3,
        daily_bars=bars,
    )
    assert result is not None
    assert result.outcome == "WIN"


def test_pending_tp_sl_prediction_uses_frozen_geometry():
    issued_ts = 1_720_000_000
    bars = _daily_bars([(111, 99, 108)], base_ts=issued_ts + 86400)
    result = resolve_pending_tp_sl_prediction(
        {
            "symbol": "WOLF",
            "direction": "UP",
            "issued_ts": issued_ts,
            "context": {
                "entry_price": 100,
                "target_price": 110,
                "stop_price": 95,
                "hold_bars": 1,
            },
        },
        daily_bars=bars,
        now=issued_ts + 2 * 86400,
    )
    assert result is not None
    assert result.outcome == "WIN"
    assert result.evidence["target_price"] == 110


def test_pending_tp_sl_prediction_marks_missing_geometry_invalid():
    issued_ts = 1_720_000_000
    result = resolve_pending_tp_sl_prediction(
        {"symbol": "WOLF", "direction": "UP", "issued_ts": issued_ts},
        now=issued_ts,
    )
    assert result is not None
    assert result.outcome == "DATA_INVALID"
    assert result.resolved_ts > issued_ts
    assert result.reason == "missing_frozen_tp_sl_context"


# ── intraday continuation resolver ──────────────────────────────────────────

def _hourly_bars(prices, base_ts=1_720_000_000):
    bars = []
    for i, (high, low, close) in enumerate(prices):
        bars.append({
            "ts": base_ts + i * 3600,
            "open": close, "high": high, "low": low, "close": close,
        })
    return bars


def test_intraday_continuation_win_up():
    bars = _hourly_bars([(101, 99, 100.5), (103, 100, 102.5)])
    result = resolve_intraday_continuation(
        symbol="WOLF", direction="UP", entry_price=100,
        predicted_at=1_720_000_000 - 1800, hourly_bars=bars,
    )
    assert result is not None
    assert result.outcome == "WIN"


def test_intraday_continuation_loss_up():
    bars = _hourly_bars([(101, 99, 100.5), (100, 98, 99.5)])
    result = resolve_intraday_continuation(
        symbol="WOLF", direction="UP", entry_price=100,
        predicted_at=1_720_000_000 - 1800, hourly_bars=bars,
    )
    assert result is not None
    assert result.outcome == "LOSS"


def test_intraday_continuation_no_bars():
    result = resolve_intraday_continuation(
        symbol="WOLF", direction="UP", entry_price=100,
        predicted_at=1_720_000_000,
    )
    assert result is not None
    assert result.outcome == "DATA_INVALID"


# ── volatility expansion resolver ──────────────────────────────────────────

def test_volatility_expansion_win():
    """Forward vol > trailing vol should be WIN."""
    base = 1_720_000_000
    bars = []
    # 10 trailing bars: slight noise so trailing vol > 0
    for i, c in enumerate([100.0, 100.1, 99.9, 100.2, 99.8, 100.1, 99.9, 100.0, 100.2, 99.8, 100.0]):
        bars.append({"ts": base + i * 86400, "open": c, "high": c + 0.3, "low": c - 0.3, "close": c})
    # 5 forward bars: volatile
    for i, c in enumerate([102, 98, 103, 97, 101]):
        bars.append({"ts": base + (11 + i) * 86400, "open": float(c), "high": float(c) + 1, "low": float(c) - 1, "close": float(c)})

    result = resolve_volatility_expansion(
        symbol="WOLF", predicted_at=base + 10 * 86400 + 3600,
        hold_bars=5, lookback_bars=10, daily_bars=bars,
    )
    assert result is not None
    assert result.outcome == "WIN"


def test_volatility_expansion_no_bars():
    result = resolve_volatility_expansion(
        symbol="WOLF", predicted_at=1_720_000_000,
    )
    assert result is not None
    assert result.outcome == "DATA_INVALID"


# ── cross-sectional resolver ───────────────────────────────────────────────

def test_cross_sectional_top_quartile_win():
    returns = {"WOLF": 0.15, "A": 0.05, "B": 0.02, "C": 0.01, "D": -0.01,
               "E": -0.02, "F": -0.05, "G": -0.10}
    result = resolve_cross_sectional_ranking(
        symbol="WOLF", output="TOP_QUARTILE", predicted_at=1_720_000_000,
        symbol_returns=returns,
    )
    assert result is not None
    assert result.outcome == "WIN"


def test_cross_sectional_top_quartile_loss():
    returns = {"WOLF": 0.01, "A": 0.15, "B": 0.12, "C": 0.10, "D": 0.08,
               "E": 0.05, "F": 0.03, "G": 0.02}
    result = resolve_cross_sectional_ranking(
        symbol="WOLF", output="TOP_QUARTILE", predicted_at=1_720_000_000,
        symbol_returns=returns,
    )
    assert result is not None
    assert result.outcome == "LOSS"


def test_cross_sectional_insufficient_cohort():
    result = resolve_cross_sectional_ranking(
        symbol="WOLF", output="TOP_QUARTILE", predicted_at=1_720_000_000,
        symbol_returns={"WOLF": 0.05, "A": 0.03},
    )
    assert result is not None
    assert result.outcome == "DATA_INVALID"


# ── event reaction resolver ────────────────────────────────────────────────

def test_event_reaction_positive_win():
    sym_bars = _daily_bars([(101, 99, 100.5), (104, 100, 103), (107, 102, 106)],
                           base_ts=1_720_000_000 + 86400)
    spy_bars = _daily_bars([(401, 399, 400.5), (402, 400, 401), (403, 401, 402)],
                           base_ts=1_720_000_000 + 86400)
    result = resolve_event_reaction(
        symbol="WOLF", output="POSITIVE", event_ts=1_720_000_000,
        hold_bars=3, symbol_bars=sym_bars, spy_bars=spy_bars,
    )
    assert result is not None
    assert result.outcome == "WIN"


def test_event_reaction_no_bars():
    result = resolve_event_reaction(
        symbol="WOLF", output="POSITIVE", event_ts=1_720_000_000,
    )
    assert result is not None
    assert result.outcome == "DATA_INVALID"
