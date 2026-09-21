"""Two-sided TP/SL bands: the half of the vol problem PR #163 left unsolved.

PR #163 found that "base_vol_pct is a flat 2% for every stock -- a $1.40
biotech got the same bracket as MSFT" and fixed it for the WALLET only, via
forecast_band_vol_pct. But that helper is widen-only:

    widened = max(base, realized * scale)

so a QUIET symbol keeps the flat 2%. That is the expensive half. A 2% target
inside five bars is unreachable for a mega-cap or an event-pinned name, the
trade EXPIRES, and expiry counts as a loss (SE-4) -- so Ghost is scored wrong
on a trade that never ran.

Measured over 4,424 resolved shadow outcomes on 2026-09-04: 6.6% expired,
concentrated in APGE (49/49 = 100%), JPM 74.4%, GOOG 58.3%, COST 37.8%,
AMZN 34.1%, V 31.8%. Pooled Wilson LB was 53.89% with those symbols included
and 56.29% without -- the difference between failing and clearing a 55% target,
caused by instrument sizing rather than by prediction quality.

Default OFF: enabling changes the geometry contract, hence every label_schema,
hence requires a full retrain before anything can serve.
"""
from __future__ import annotations

import importlib

import pytest

import core.vol_targets as vt


def _bars(n, range_pct, close=100.0):
    """Synthetic daily bars with a fixed (high-low)/close."""
    return [{
        "open": close, "close": close, "volume": 1_000_000,
        "high": close * (1 + range_pct / 2.0),
        "low": close * (1 - range_pct / 2.0),
    } for _ in range(n)]


@pytest.fixture
def adaptive_on(monkeypatch):
    monkeypatch.setenv("V3_ADAPTIVE_VOL_BANDS", "1")
    importlib.reload(vt)
    yield vt
    monkeypatch.delenv("V3_ADAPTIVE_VOL_BANDS", raising=False)
    importlib.reload(vt)


# ------------------------------------------------------------ the core gap --

def test_quiet_symbols_get_a_narrower_band(adaptive_on):
    """The whole point. A 0.1%-range symbol cannot reach a 2% target in five
    bars, so it expires forever -- APGE went 49/49 expired."""
    base = adaptive_on.base_vol_pct("APGE", "stock")
    got = adaptive_on.adaptive_vol_pct("APGE", "stock", _bars(20, 0.001))

    assert got["vol_pct"] < base
    assert got["source"] == "realized_range_floored"


def test_forecast_band_cannot_do_this(adaptive_on):
    """Contrast that pins WHY a new function was needed rather than reusing the
    existing one: forecast_band_vol_pct is max(base, ...), so it never narrows."""
    quiet = _bars(20, 0.001)

    widen_only = adaptive_on.forecast_band_vol_pct("APGE", "stock", quiet)
    two_sided = adaptive_on.adaptive_vol_pct("APGE", "stock", quiet)

    assert widen_only["vol_pct"] == adaptive_on.base_vol_pct("APGE", "stock")
    assert two_sided["vol_pct"] < widen_only["vol_pct"]


def test_volatile_symbols_still_get_a_wider_band(adaptive_on):
    base = adaptive_on.base_vol_pct("X", "stock")
    got = adaptive_on.adaptive_vol_pct("X", "stock", _bars(20, 0.09))

    assert got["vol_pct"] > base


def test_band_is_floored_so_a_pinned_symbol_still_needs_a_real_move(adaptive_on):
    """Without a floor, a pinned name would resolve on noise -- which would
    manufacture wins rather than fix the measurement."""
    floor = adaptive_on._adaptive_vol_floor("stock")
    got = adaptive_on.adaptive_vol_pct("APGE", "stock", _bars(20, 0.00001))

    assert got["vol_pct"] >= floor


def test_band_is_capped(adaptive_on):
    cap = adaptive_on._forecast_band_vol_cap("stock")
    got = adaptive_on.adaptive_vol_pct("X", "stock", _bars(20, 0.95))

    assert got["vol_pct"] <= cap


# --------------------------------------------------------- no lookahead --

def test_end_idx_excludes_bars_after_the_entry(adaptive_on):
    """A label must never see range from after its own entry. Quiet history
    followed by a volatile blow-up must still price off the quiet part."""
    rows = _bars(12, 0.002) + _bars(12, 0.20)

    early = adaptive_on.adaptive_vol_pct("X", "stock", rows, end_idx=11)
    full = adaptive_on.adaptive_vol_pct("X", "stock", rows)

    assert early["vol_pct"] < full["vol_pct"]


def test_insufficient_history_falls_back_to_base(adaptive_on):
    got = adaptive_on.adaptive_vol_pct("X", "stock", _bars(2, 0.02))

    assert got["vol_pct"] == adaptive_on.base_vol_pct("X", "stock")
    assert got["source"] == "base_insufficient_history"


# ------------------------------------------------------- model identity --

def test_disabled_by_default_and_returns_the_flat_band(monkeypatch):
    monkeypatch.delenv("V3_ADAPTIVE_VOL_BANDS", raising=False)
    importlib.reload(vt)

    assert vt.adaptive_vol_enabled() is False
    got = vt.adaptive_vol_pct("APGE", "stock", _bars(20, 0.001))
    assert got["vol_pct"] == vt.base_vol_pct("APGE", "stock")
    assert got["source"] == "base_adaptive_disabled"


def test_enabling_changes_the_geometry_schema(monkeypatch):
    """Labels mean something different under adaptive bands, so models trained
    under the flat band must be REJECTED, not served against a contract they
    were never fit for. The schema hash is what enforces that."""
    monkeypatch.delenv("V3_ADAPTIVE_VOL_BANDS", raising=False)
    importlib.reload(vt)
    off = vt.tp_sl_geometry_schema()

    monkeypatch.setenv("V3_ADAPTIVE_VOL_BANDS", "1")
    importlib.reload(vt)
    on = vt.tp_sl_geometry_schema()

    assert off != on, "flipping adaptive bands would silently rewrite labels"
    assert vt.tp_sl_geometry_contract()["adaptive_vol"]["enabled"] is True

    monkeypatch.delenv("V3_ADAPTIVE_VOL_BANDS", raising=False)
    importlib.reload(vt)
    assert vt.tp_sl_geometry_contract()["adaptive_vol"] == {"enabled": False}
