"""Tests for shared TP/SL resolution (Phase 2 label alignment)."""
import time
from datetime import datetime, timezone


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


def test_forward_bars_accept_unix_second_timestamps():
    entry_ts = int(datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc).timestamp())
    rows = [
        {"ts": int(datetime(2026, 6, day, tzinfo=timezone.utc).timestamp()),
         "high": 11, "low": 9}
        for day in (2, 3, 4)
    ]
    fwd = tps.forward_bars_after_entry(rows, entry_ts, hold_bars=2)
    assert [tps._date_key(row["ts"]) for row in fwd] == ["2026-06-03", "2026-06-04"]


def test_forward_bars_dedup_same_calendar_date():
    """Several intraday timestamps on one date count as ONE daily bar."""
    ts = int(datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc).timestamp())
    rows = [
        {"ts": "2026-06-03T09:30:00Z", "high": 11, "low": 9},
        {"ts": "2026-06-03T10:00:00Z", "high": 11, "low": 9},
        {"ts": "2026-06-03T16:00:00Z", "high": 11, "low": 9},
        {"ts": "2026-06-04T00:00:00Z", "high": 11, "low": 9},
    ]
    fwd = tps.forward_bars_after_entry(rows, ts, hold_bars=3)
    assert len(fwd) == 2
    assert tps._date_key(fwd[0]["ts"]) == "2026-06-03"
    assert tps._date_key(fwd[1]["ts"]) == "2026-06-04"

def test_direction_label_respects_down_lane():
    from core.tp_sl_resolve import simulate_direction_label

    rows = [
        {"ts": "2026-06-01T00:00:00Z", "close": 10.0},
        {"ts": "2026-06-02T00:00:00Z", "close": 9.0},
    ]

    assert simulate_direction_label(rows, 0, 1, "UP")[0] == "LOSS"
    assert simulate_direction_label(rows, 0, 1, "DOWN")[0] == "WIN"


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


def test_resolve_open_prediction_ignores_partial_current_daily_bar():
    ts = int(datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc).timestamp())
    before_close = int(datetime(2026, 6, 5, 18, 0, tzinfo=timezone.utc).timestamp())
    rows = [
        {"ts": f"2026-06-0{day}T00:00:00Z", "high": 10.2, "low": 10.0}
        for day in (3, 4, 5)
    ]
    assert tps.resolve_open_prediction(
        direction="UP", target=11.0, stop=9.0, predicted_at=ts,
        hold_bars=3, daily_bars=rows, now=before_close,
    ) is None
    after_close = int(datetime(2026, 6, 5, 22, 0, tzinfo=timezone.utc).timestamp())
    assert tps.resolve_open_prediction(
        direction="UP", target=11.0, stop=9.0, predicted_at=ts,
        hold_bars=3, daily_bars=rows, now=after_close,
    ) == "EXPIRED"


def test_resolve_open_prediction_does_not_expire_incomplete_bar_horizon():
    ts = int(datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc).timestamp())
    rows = [
        {"ts": "2026-06-03T00:00:00Z", "high": 10.2, "low": 10.0},
        {"ts": "2026-06-04T00:00:00Z", "high": 10.2, "low": 10.0},
    ]
    out = tps.resolve_open_prediction(
        direction="UP",
        target=11.0,
        stop=9.0,
        predicted_at=ts,
        hold_bars=3,
        daily_bars=rows,
        now=ts + 86400 * 10,
        expires_at=ts + 86400 * 5,
    )
    assert out is None


def test_snapshot_resolves_while_daily_horizon_is_incomplete():
    ts = int(datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc).timestamp())
    now = int(datetime(2026, 6, 4, 18, 0, tzinfo=timezone.utc).timestamp())
    rows = [
        {"ts": "2026-06-03T00:00:00Z", "high": 10.2, "low": 10.0, "close": 10.1},
        {"ts": "2026-06-04T00:00:00Z", "high": 11.5, "low": 10.0, "close": 11.2},
    ]
    outcome, resolved_at, exit_price = tps.resolve_open_prediction_detail(
        direction="UP", target=11.0, stop=9.0, predicted_at=ts,
        hold_bars=3, daily_bars=rows, snapshot_price=11.2, now=now,
    )
    assert (outcome, resolved_at, exit_price) == ("WIN", now, 11.0)


def test_delayed_expiry_uses_horizon_close_and_timestamp():
    ts = int(datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc).timestamp())
    rows = [
        {"ts": f"2026-06-0{day}T00:00:00Z", "high": 10.2,
         "low": 10.0, "close": close}
        for day, close in ((3, 10.1), (4, 10.15), (5, 10.05))
    ]
    outcome, resolved_at, exit_price = tps.resolve_open_prediction_detail(
        direction="UP", target=11.0, stop=9.0, predicted_at=ts,
        hold_bars=3, daily_bars=rows,
        snapshot_price=12.0,
        now=int(datetime(2026, 6, 8, 21, 0, tzinfo=timezone.utc).timestamp()),
    )
    assert outcome == "EXPIRED"
    assert resolved_at == int(
        datetime(2026, 6, 5, 21, 0, tzinfo=timezone.utc).timestamp()
    )
    assert exit_price == 10.05


def test_simulated_label_resolution_timestamp_is_exact():
    rows = [
        {"ts": "2026-06-02T00:00:00Z", "close": 100.0, "high": 100.0, "low": 100.0},
        {"ts": "2026-06-03T00:00:00Z", "close": 100.0, "high": 100.5, "low": 99.5},
        {"ts": "2026-06-04T00:00:00Z", "close": 100.0, "high": 103.0, "low": 99.5},
        {"ts": "2026-06-05T00:00:00Z", "close": 100.0, "high": 100.5, "low": 99.5},
    ]
    outcome, resolved_ts = tps.simulate_tp_sl_label_detail(
        rows, 0, hold_bars=3, vol_pct=0.02,
    )
    assert outcome == "WIN"
    assert resolved_ts == int(
        datetime(2026, 6, 4, 21, 0, tzinfo=timezone.utc).timestamp()
    )


def test_expiry_resolution_timestamp_is_daily_close_not_midnight():
    rows = [
        {"ts": f"2026-06-0{day}T00:00:00Z", "close": 100.0,
         "high": 100.5, "low": 99.5}
        for day in (2, 3, 4, 5)
    ]
    outcome, resolved_ts = tps.simulate_tp_sl_label_detail(
        rows, 0, hold_bars=3, vol_pct=0.02,
    )
    assert outcome == "EXPIRED"
    assert resolved_ts == int(
        datetime(2026, 6, 5, 21, 0, tzinfo=timezone.utc).timestamp()
    )


def test_holdout_slices_do_not_overlap():
    import core.signal_engine as _se
    train_end, calib_end = _se._v3_holdout_slices(127)
    assert 0 < train_end < calib_end < 127
    assert train_end == int(127 * 0.70)
    assert calib_end == int(127 * 0.85)


def test_holdout_acc_override_env(monkeypatch):
    import core.signal_engine as _se
    monkeypatch.setenv("V3_HOLDOUT_ACC_OVERRIDES", "TLRY=0.47")
    assert _se._v3_holdout_acc_overrides() == {"TLRY": 0.47}


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
            "wf_fold_count": 3, "trained_at": time.time(),
            "model_sha256": "a" * 64, "label_schema": _se._v3_label_schema(),
            "validation_schema": _se._v3_validation_schema(),
            "label_hold_bars": _se.V3_LABEL_HOLD_BARS,
            "precision_gate": {"ok": True, "threshold": 0.55, "target": 0.70,
                               "calib": {"support": 20, "wins": 20},
                               "gate": {"support": 20, "wins": 20}}}
    monkeypatch.setattr(_se, "load_model", lambda s, direction="UP": (_M(), _se.FEATURE_COLS, meta))
    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "legacy")
    # Hermetic: kill the live premarket overlay (time-of-day flake — real
    # symbol price stomps the synthetic fixture during 4:00-9:30 AM CT).
    monkeypatch.setenv("GHOST_PREMARKET_SCAN", "0")
    monkeypatch.setenv("V3_PROVEN_SKILL_GATE", "0")
    monkeypatch.setenv("V3_OVERCONFIDENCE_GATE", "0")
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
            "wf_edge_mean": 0.2, "wf_fold_count": 4, "trained_at": time.time(),
            "model_sha256": "a" * 64, "label_schema": _se._v3_label_schema(),
            "validation_schema": _se._v3_validation_schema(),
            "label_hold_bars": _se.V3_LABEL_HOLD_BARS,
            "precision_gate": {"ok": True, "threshold": 0.55, "target": 0.70,
                               "calib": {"support": 20, "wins": 20},
                               "gate": {"support": 20, "wins": 20}}}
    monkeypatch.setattr(_se, "load_model", lambda s, direction="UP": (_M(), _se.FEATURE_COLS, meta))
    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "legacy")
    # Hermetic: kill the live premarket overlay (time-of-day flake — real
    # symbol price stomps the synthetic fixture during 4:00-9:30 AM CT).
    monkeypatch.setenv("GHOST_PREMARKET_SCAN", "0")
    monkeypatch.setenv("V3_PROVEN_SKILL_GATE", "0")
    monkeypatch.setenv("V3_OVERCONFIDENCE_GATE", "0")
    for k, v in {"V3_MIN_WIN_PROBA": "0.55", "V3_MIN_EDGE": "0.0",
                 "V3_MIN_HOLDOUT_ACC": "0.0", "V3_MIN_WF_ACC_MEAN": "0.0"}.items():
        monkeypatch.setenv(k, v)

    sig, reason = _se.predict_live_ex("WOLF", "stock")
    assert sig is not None
    direction, conf = sig
    assert direction == "UP"
    assert conf == 0.623
    assert conf != round(max(0.75, 0.66 + (0.6234 - 0.55) * 4.0), 3)
