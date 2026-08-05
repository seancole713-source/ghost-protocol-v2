"""Serveability guards — DB rows vs load_model/loadable counts."""
import json
import time

import core.signal_engine as _se


def _serveable_meta(**overrides):
    meta = {
        "tier": "proven",
        "direction": "UP",
        "model_sha256": "a" * 64,
        "label_type": _se.LABEL_TYPE,
        "label_schema": _se._v3_label_schema(),
        "feature_schema": _se._v3_feature_schema(),
        "validation_schema": _se._v3_validation_schema(),
        "label_hold_bars": _se.V3_LABEL_HOLD_BARS,
        "trained_at": int(time.time()),
        "accuracy": 0.70,
        "edge": 0.10,
        "wf_acc_mean": 0.68,
        "wf_edge_mean": 0.08,
        "wf_fold_count": 5,
    }
    meta.update(overrides)
    return meta


def test_model_serve_guard_rejects_stale_feature_schema():
    assert _se.model_serve_guard(
        _serveable_meta(feature_schema="macd_pct_v1+sec0")
    ) == "feature_schema_stale"


def test_model_serve_guard_accepts_current_schema():
    assert _se.model_serve_guard(_serveable_meta(), expected_direction="UP") is None


def test_model_serve_guard_accepts_old_proven_artifact_inside_activation_lease(monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(_se.time, "time", lambda: now)
    meta = _serveable_meta(
        trained_at=now - 30 * 86400,
        activation_artifact_sha="b" * 64,
        activated_at=now - 60,
        activation_lease_expires_at=now + 3600,
        activation_proof={
            "registration_id": "registration",
            "wins": 42,
            "n": 50,
            "status": "PROVEN",
            "persisted_status": "PROVEN",
            "closed_at_ts": now - 120,
            "all_secondary_pass": True,
        },
    )

    assert _se.model_serve_guard(meta, expected_direction="UP") is None


def test_model_serve_guard_rejects_expired_activation_lease(monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(_se.time, "time", lambda: now)
    meta = _serveable_meta(
        trained_at=now - 30 * 86400,
        activation_artifact_sha="b" * 64,
        activated_at=now - 3600,
        activation_lease_expires_at=now,
        activation_proof={
            "registration_id": "registration",
            "wins": 42,
            "n": 50,
            "status": "PROVEN",
            "persisted_status": "PROVEN",
            "closed_at_ts": now - 7200,
            "all_secondary_pass": True,
        },
    )

    assert _se.model_serve_guard(meta) == "activation_lease_expired"


def test_model_serve_guard_rejects_legacy_validation_schema():
    meta = _serveable_meta()
    del meta["validation_schema"]
    assert _se.model_serve_guard(meta) == "validation_schema_stale"


def test_model_serve_guard_rejects_future_training_timestamp(monkeypatch):
    now = 1_800_000_000.0
    monkeypatch.setattr(_se.time, "time", lambda: now)
    assert _se.model_serve_guard(
        _serveable_meta(trained_at=now + 301),
    ) == "trained_at_future"


def test_model_serve_guard_rejects_nonfinite_and_bounded_metrics():
    for key, bad in (
        ("accuracy", float("nan")),
        ("edge", float("inf")),
        ("wf_acc_mean", 1.01),
        ("wf_edge_mean", -1.01),
        ("wf_fold_count", 4.5),
        ("wf_fold_count", True),
        ("natural_rate", float("nan")),
    ):
        assert _se.model_serve_guard(
            _serveable_meta(**{key: bad}),
        ) == "model_metrics_invalid"


def test_model_serve_guard_rejects_direction_mismatch():
    assert _se.model_serve_guard(
        _serveable_meta(direction="DOWN"), expected_direction="UP",
    ) == "direction_mismatch"


def test_model_serve_guard_requires_exact_hold_horizon():
    for bad in (True, 3.5, float("nan"), float("inf")):
        assert _se.model_serve_guard(
            _serveable_meta(label_hold_bars=bad),
        ) == "label_hold_bars_stale"


def test_research_guard_is_scoring_only():
    meta = _serveable_meta(tier="research")
    assert _se.model_serve_guard(meta) == "tier_unproven"
    assert _se.model_serve_guard(meta, allow_research_scoring=True) is None

def test_live_trainer_rejects_research_label_modes(monkeypatch):
    seen = []
    monkeypatch.setenv("V3_LABEL_TYPE", "volatility")
    monkeypatch.setattr(_se, "_persist_train_details", lambda details: seen.extend(details))

    model, accuracy, passed = _se.train_and_validate([])

    assert (model, accuracy, passed) == (None, 0.0, False)
    assert seen[0]["stage"] == "contract_guard"
    assert "research-only" in seen[0]["fail_reason"]

def test_live_trainer_rejects_non_point_in_time_features(monkeypatch):
    seen = []
    monkeypatch.setenv("V3_LABEL_TYPE", "tp_sl")
    monkeypatch.setenv("V3_NEWS_FEATURES", "on")
    monkeypatch.setenv("V3_OPTIONS_FEATURES", "off")
    monkeypatch.setenv("V3_INTRADAY_FEATURES", "off")
    monkeypatch.setattr(_se, "_persist_train_details", lambda details: seen.extend(details))

    model, accuracy, passed = _se.train_and_validate([])

    assert (model, accuracy, passed) == (None, 0.0, False)
    assert seen[0]["stage"] == "feature_contract_guard"
    assert "news" in seen[0]["fail_reason"]


def test_max_calibration_brier_phase5_floor(monkeypatch):
    monkeypatch.setenv("V3_MAX_CALIBRATION_BRIER", "0.24")
    assert _se._v3_max_calibration_brier() == 0.31


def test_sma5_from_daily_bars_uses_last_five_closes():
    rows = [{"close": float(i)} for i in (10, 11, 12, 13, 14, 15, 16)]
    assert _se._sma5_from_daily_bars(rows) == (14 + 15 + 16 + 13 + 12) / 5


def test_block_up_below_sma5_blocks_when_price_under_sma(monkeypatch):
    monkeypatch.setenv("V3_BLOCK_UP_BELOW_SMA5", "1")
    monkeypatch.setattr(
        _se,
        "_fetch_ohlcv",
        lambda symbol, asset_type, period=None, interval="1d": [
            {"close": 100.0},
            {"close": 100.0},
            {"close": 100.0},
            {"close": 100.0},
            {"close": 100.0},
        ],
    )
    blocked, sma, cur = _se._block_up_below_sma5("WOLF", "stock", 95.0)
    assert blocked is True
    assert sma == 100.0
    assert cur == 95.0


def test_block_up_below_sma5_can_disable(monkeypatch):
    monkeypatch.setenv("V3_BLOCK_UP_BELOW_SMA5", "0")
    blocked, _, _ = _se._block_up_below_sma5("WOLF", "stock", 1.0)
    assert blocked is False


def test_get_model_status_counts_serveable_only(monkeypatch):
    wolf_meta = _serveable_meta(accuracy=0.6, edge=0.1)
    stale_meta = _serveable_meta(
        feature_schema="macd_pct_v1+sec0", accuracy=0.5,
    )
    rows = {
        "meta_WOLF": json.dumps(wolf_meta),
        "meta_STALE": json.dumps(stale_meta),
        "model_WOLF": "x",
    }

    class _Cur:
        def execute(self, sql, params=None):
            self._sql = sql

        def fetchall(self):
            if "meta_%" in self._sql and "LIKE 'model_%'" not in self._sql:
                return [(k, v) for k, v in rows.items() if k.startswith("meta_")]
            if "LIKE 'model_%'" in self._sql:
                return [(k,) for k in rows if k.startswith("model_")]
            return []

        def fetchone(self):
            return None

    class _Conn:
        def cursor(self):
            return _Cur()

    class _DbCtx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    import core.db as _db

    monkeypatch.setattr(_db, "db_conn", lambda: _DbCtx())
    monkeypatch.setattr(_se, "get_last_train_gate_summary", lambda: {})
    st = _se.get_model_status()
    assert st["models"] == 1
    assert st["models_stored"] == 2
    assert "WOLF" in st["symbols"]
    assert "STALE" in st["stored_symbols"]
    assert st["stored_symbols"]["STALE"]["serve_reject"] == "feature_schema_stale"
