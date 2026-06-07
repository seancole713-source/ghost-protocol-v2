"""Tests for shared TP/SL resolution (Phase 2 label alignment)."""
import time
from datetime import datetime, timezone

import pytest

from core import tp_sl_resolve as tps


def test_bar_path_same_bar_both_hit_is_loss():
    bars = [{"high": 12.0, "low": 9.0}]
    assert tps.resolve_tp_sl_bar_path(bars, target=11.0, stop=9.5) == "LOSS"


def test_bar_path_target_wins():
    bars = [{"high": 11.0, "low": 10.0}]
    assert tps.resolve_tp_sl_bar_path(bars, target=10.5, stop=9.0) == "WIN"


def test_bar_path_still_open():
    bars = [{"high": 10.2, "low": 10.0}]
    assert tps.resolve_tp_sl_bar_path(bars, target=11.0, stop=9.0) is None


def test_forward_bars_skip_entry_day():
    ts = int(datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc).timestamp())
    rows = [
        {"ts": "2026-06-02T00:00:00Z", "high": 11, "low": 9},
        {"ts": "2026-06-03T00:00:00Z", "high": 11, "low": 9},
        {"ts": "2026-06-04T00:00:00Z", "high": 11, "low": 9},
    ]
    fwd = tps.forward_bars_after_entry(rows, ts, hold_bars=2)
    assert len(fwd) == 2
    assert tps._date_key(fwd[0]["ts"]) == "2026-06-03"


def test_resolve_open_prediction_expired_after_hold_window():
    ts = int(datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc).timestamp())
    rows = [
        {"ts": "2026-06-03T00:00:00Z", "high": 10.2, "low": 10.0},
        {"ts": "2026-06-04T00:00:00Z", "high": 10.2, "low": 10.0},
        {"ts": "2026-06-05T00:00:00Z", "high": 10.2, "low": 10.0},
    ]
    out = tps.resolve_open_prediction(
        direction="UP",
        target=11.0,
        stop=9.0,
        predicted_at=ts,
        hold_bars=3,
        daily_bars=rows,
        snapshot_price=10.1,
        now=ts + 86400 * 10,
        expires_at=ts + 86400 * 5,
    )
    assert out == "EXPIRED"


def test_holdout_slices_do_not_overlap():
    import core.signal_engine as _se
    train_end, calib_end = _se._v3_holdout_slices(127)
    assert 0 < train_end < calib_end < 127
    assert train_end == int(127 * 0.70)
    assert calib_end == int(127 * 0.85)


def test_feature_asof_on_live_features(monkeypatch):
    import core.signal_engine as _se
    import numpy as _np

    ts = "2026-06-05T20:00:00Z"
    rows = []
    for i in range(220):
        px = 100.0 + i * 0.4
        rows.append({"ts": ts if i == 219 else "2026-05-20T%02d:00:00Z" % (i % 24),
                     "open": px - 0.2, "high": px + 0.5, "low": px - 0.5,
                     "close": px, "volume": 1000 + i * 5})
    monkeypatch.setattr(_se, "_fetch_ohlcv", lambda *a, **k: rows)

    class _M:
        def predict_proba(self, X):
            return _np.array([[0.2, 0.61]])

    meta = {"edge": 0.2, "accuracy": 0.6, "wf_acc_mean": 0.6, "wf_edge_mean": 0.1,
            "wf_fold_count": 3, "trained_at": time.time()}
    monkeypatch.setattr(_se, "load_model", lambda s: (_M(), _se.FEATURE_COLS, meta))
    for k, v in {"V3_MIN_WIN_PROBA": "0.55", "V3_MIN_EDGE": "0.0",
                 "V3_MIN_HOLDOUT_ACC": "0.0", "V3_MIN_WF_ACC_MEAN": "0.0"}.items():
        monkeypatch.setenv(k, v)

    scores = {}
    sig, _ = _se.predict_live_ex("WOLF", "stock", scores=scores)
    assert sig is not None
    from core.feature_schema import FEATURE_ASOF_KEY, feature_asof_unix
    assert scores["features"][FEATURE_ASOF_KEY] == feature_asof_unix(ts)


def test_confidence_equals_up_prob(monkeypatch):
    """Phase 2: fired confidence must equal calibrated up_prob, not holdout accuracy blend."""
    import core.signal_engine as _se
    import numpy as _np

    rows = []
    for i in range(220):
        px = 100.0 + i * 0.4
        rows.append({"ts": "2026-05-20T%02d:00:00Z" % (i % 24),
                     "open": px - 0.2, "high": px + 0.5, "low": px - 0.5,
                     "close": px, "volume": 1000 + i * 5})
    monkeypatch.setattr(_se, "_fetch_ohlcv", lambda s, a, period="5d", interval="1h": rows)

    class _M:
        def predict_proba(self, X):
            return _np.array([[0.18, 0.6234]])

    meta = {"edge": 0.3, "accuracy": 0.66, "wf_acc_mean": 0.64,
            "wf_edge_mean": 0.2, "wf_fold_count": 4, "trained_at": time.time()}
    monkeypatch.setattr(_se, "load_model", lambda s: (_M(), _se.FEATURE_COLS, meta))
    for k, v in {"V3_MIN_WIN_PROBA": "0.55", "V3_MIN_EDGE": "0.0",
                 "V3_MIN_HOLDOUT_ACC": "0.0", "V3_MIN_WF_ACC_MEAN": "0.0"}.items():
        monkeypatch.setenv(k, v)

    sig, reason = _se.predict_live_ex("WOLF", "stock")
    assert sig is not None
    direction, conf = sig
    assert direction == "UP"
    assert conf == 0.623
    assert conf != round(max(0.75, 0.66 + (0.6234 - 0.55) * 4.0), 3)
