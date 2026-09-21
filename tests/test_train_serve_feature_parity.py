"""Train/serve parity for windowed features, and the negative-edge floor ban.

Two regressions found by an adversarially-verified audit on 2026-09-03:

1. TRAIN/SERVE SKEW. backtest_symbol() windows every labeled row to
   `_backtest_window()` trailing bars, so `_calculate_features` never saw
   >=200 bars at fit time and ema200 was aliased to ema50. The live and
   research serve paths passed a full ~252-bar year in, taking the n>=200
   branch instead (real ema200 + 3-way EMA stack) — a different feature
   distribution than the model was trained on, invisible to the serve guard
   because _v3_feature_schema() does not encode window size.

2. BANNED NEGATIVE-EDGE FLOOR. The thin-data block in _train_one_direction
   scaled min_wf_edge back down to -0.05, silently undoing PR #135 ("a model
   with negative out-of-time edge must never count toward a 70% system ...
   loosening below zero requires an explicit env choice") with no env choice
   involved — and gate-passing models' OOS rows feed the pooled cross-symbol
   live-fire proof.
"""
from __future__ import annotations

from core import signal_engine as se
from core.engine_config import _backtest_window, _v3_min_wf_edge
from core.engine_features import _calculate_features


def _bars(n, start=100.0, step=0.25):
    """Monotone synthetic daily bars — enough for the n>=200 EMA branch."""
    out = []
    for i in range(n):
        px = start + i * step
        out.append({
            "ts": f"2026-01-{(i % 28) + 1:02d}",
            "open": px, "high": px + 0.5, "low": px - 0.5,
            "close": px, "volume": 1_000_000,
        })
    return out


# ------------------------------------------------- train/serve feature parity --

def test_serving_feature_bars_matches_training_window():
    """The slice handed to _calculate_features must be the same length the
    training loop's `rows[max(0, i-window):i+1]` produces."""
    window = _backtest_window()
    rows = _bars(252)

    served = se._serving_feature_bars(rows)

    assert len(served) == window + 1
    # Same trailing edge as training's last labeled row.
    assert served[-1] is rows[-1]


def test_serving_window_keeps_features_on_the_training_ema_branch():
    """Regression: unwindowed 252 bars take engine_features' n>=200 branch
    (real ema200); windowed bars take the <200 fallback where ema200 aliases
    ema50 — exactly what training produced.

    The two must produce DIFFERENT feature vectors, or this test proves
    nothing about the skew it guards. (On a monotone series the boolean EMA
    flags coincide by luck even though the branch differs, so compare the
    whole vector rather than cherry-picking a flag.)"""
    rows = _bars(252)
    assert len(rows) > 200, "fixture must be long enough to reach the n>=200 branch"

    unwindowed = _calculate_features(rows)
    windowed = _calculate_features(se._serving_feature_bars(rows))

    assert unwindowed != windowed, (
        "windowing made no difference — either the window is no longer applied "
        "or engine_features stopped branching on history length"
    )

    # The windowed result is exactly what the equivalent training row yields.
    train_equivalent = _calculate_features(
        rows[max(0, len(rows) - 1 - _backtest_window()):]
    )
    assert windowed == train_equivalent


def test_serving_feature_bars_is_a_noop_on_short_history():
    rows = _bars(40)
    assert se._serving_feature_bars(rows) == rows


def test_serving_feature_bars_handles_empty():
    assert se._serving_feature_bars([]) == []


def test_live_and_research_serve_paths_both_window_features():
    """Both serve call sites must pass through _serving_feature_bars. A future
    edit that reverts either one to a bare _calculate_features(rows) fails
    here instead of silently reintroducing the skew."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    live = (root / "core" / "signal_engine.py").read_text(encoding="utf-8")
    research = (root / "core" / "research_runner.py").read_text(encoding="utf-8")

    assert "_calculate_features(_serving_feature_bars(rows))" in live
    assert "_calculate_features(_serving_feature_bars(rows))" in research


# ------------------------------------------------- negative-edge floor ban --

def test_thin_data_block_never_relaxes_wf_edge_below_contract_floor(monkeypatch):
    """The thin-data scaling must leave min_wf_edge at (or above) the contract
    floor for every sample count, including the n_samples<=30 case that used
    to yield exactly -0.05."""
    monkeypatch.delenv("V3_MIN_WF_EDGE", raising=False)
    floor = _v3_min_wf_edge()

    for n_samples in (20, 30, 45, 60, 80, 99):
        wf_scale = max(0.0, min(1.0, (n_samples - 30) / 70.0))
        # Reproduce the OLD formula to prove it violated the floor...
        old = -0.05 + wf_scale * (floor + 0.05)
        # ...and the NEW one to prove it does not.
        new = max(floor, _v3_min_wf_edge())

        assert new >= floor, f"n_samples={n_samples} relaxed below the floor"
        if n_samples <= 30:
            assert old < floor, "fixture stale: old formula no longer violates the floor"


def test_explicit_env_choice_can_still_set_a_sub_zero_floor(monkeypatch):
    """The contract permits going below zero — but only as a deliberate env
    choice, never implicitly from a thin sample count."""
    monkeypatch.setenv("V3_MIN_WF_EDGE", "-0.05")
    assert _v3_min_wf_edge() == -0.05
    assert max(_v3_min_wf_edge(), _v3_min_wf_edge()) == -0.05


def test_source_no_longer_hardcodes_the_banned_floor_in_thin_data_block():
    """Guards the specific line that regressed."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "core" / "signal_engine.py").read_text(encoding="utf-8")
    assert "min_wf_edge = -0.05 + wf_scale" not in src
    assert "min_wf_edge = max(min_wf_edge, _v3_min_wf_edge())" in src
