"""Tests for core/research_point_in_time.py — point-in-time dataset validation."""
import pytest
from core.research_point_in_time import (
    SourceObservation,
    DatasetSample,
    DatasetManifest,
    validate_source_timestamp,
    daily_ohlcv_observation,
    news_event_observation,
    build_dataset_manifest,
)


# ── SourceObservation ───────────────────────────────────────────────────────

def test_source_observation_valid():
    obs = SourceObservation(
        source_id="daily_ohlcv",
        event_ts=1000,
        available_ts=2000,
        values={"close": 50.0},
    )
    assert obs.source_id == "daily_ohlcv"
    assert obs.event_ts == 1000
    assert obs.available_ts == 2000


def test_source_observation_rejects_empty_source_id():
    with pytest.raises(ValueError, match="source_id"):
        SourceObservation(source_id="", event_ts=1000, available_ts=2000, values={})


def test_source_observation_rejects_non_positive_event_ts():
    with pytest.raises(ValueError, match="event_ts"):
        SourceObservation(source_id="test", event_ts=0, available_ts=2000, values={})


def test_source_observation_rejects_available_before_event():
    with pytest.raises(ValueError, match="available_ts"):
        SourceObservation(source_id="test", event_ts=2000, available_ts=1000, values={})


def test_source_observation_rejects_non_finite_values():
    with pytest.raises(ValueError, match="non-finite"):
        SourceObservation(source_id="test", event_ts=1000, available_ts=2000,
                         values={"bad": float("nan")})


def test_source_observation_is_frozen():
    obs = SourceObservation(source_id="test", event_ts=1000, available_ts=2000, values={})
    with pytest.raises(Exception):
        obs.source_id = "changed"  # type: ignore


# ── DatasetSample ──────────────────────────────────────────────────────────

def _make_sample(**overrides):
    kwargs = {
        "symbol": "WOLF",
        "prediction_ts": 1_720_000_000,
        "features": {"rsi": 50.0},
        "feature_asof_ts": 1_720_000_000,
        "label": 1,
        "outcome": "WIN",
        "label_ts": 1_720_300_000,
        "contract_id": "abc123",
        "sources": (),
    }
    kwargs.update(overrides)
    return DatasetSample(**kwargs)


def test_dataset_sample_valid():
    s = _make_sample()
    assert s.symbol == "WOLF"
    assert s.label == 1


def test_dataset_sample_rejects_feature_asof_after_prediction():
    with pytest.raises(ValueError, match="feature_asof_ts"):
        _make_sample(feature_asof_ts=1_730_000_000, prediction_ts=1_720_000_000)


def test_dataset_sample_rejects_label_before_prediction():
    with pytest.raises(ValueError, match="label_ts"):
        _make_sample(label_ts=1_710_000_000, prediction_ts=1_720_000_000)


def test_dataset_sample_rejects_invalid_outcome():
    with pytest.raises(ValueError, match="Invalid outcome"):
        _make_sample(outcome="MAYBE")


def test_dataset_sample_rejects_invalid_label():
    with pytest.raises(ValueError, match="label must be"):
        _make_sample(label=2)


def test_dataset_sample_rejects_empty_contract_id():
    with pytest.raises(ValueError, match="contract_id"):
        _make_sample(contract_id="")


def test_dataset_sample_rejects_empty_symbol():
    with pytest.raises(ValueError, match="symbol"):
        _make_sample(symbol="")


def test_dataset_sample_id_deterministic():
    s1 = _make_sample()
    s2 = _make_sample()
    assert s1.compute_sample_id() == s2.compute_sample_id()
    assert len(s1.compute_sample_id()) == 64


def test_dataset_sample_id_changes_on_different_label():
    s1 = _make_sample(label=1, outcome="WIN")
    s2 = _make_sample(label=0, outcome="LOSS")
    assert s1.compute_sample_id() != s2.compute_sample_id()


# ── timestamp validation ───────────────────────────────────────────────────

# Use realistic post-2020 Unix timestamps (feature_asof_unix rejects <= 1e9)
_TS = 1_720_000_000  # ~July 2024

def test_validate_source_timestamp_valid():
    event, avail, err = validate_source_timestamp(_TS, _TS + 3600, source_id="test")
    assert event == _TS
    assert avail == _TS + 3600
    assert err is None


def test_validate_source_timestamp_rejects_event_after_available():
    event, avail, err = validate_source_timestamp(_TS + 3600, _TS, source_id="test")
    assert event is None
    assert avail is None
    assert "event_ts" in (err or "")


def test_validate_source_timestamp_rejects_available_after_prediction():
    event, avail, err = validate_source_timestamp(
        _TS, _TS + 3600, source_id="test", prediction_ts=_TS + 1800,
    )
    assert event is None
    assert avail is None
    assert "prediction_ts" in (err or "")


def test_validate_source_timestamp_rejects_stale_data():
    # available_ts is 3600s old, but max_staleness_s=1800
    event, avail, err = validate_source_timestamp(
        _TS, _TS + 3600, source_id="test",
        prediction_ts=_TS + 7200, max_staleness_s=1800,
    )
    assert event is None
    assert avail is None
    assert "staleness" in (err or "")


def test_validate_source_timestamp_rejects_millisecond_scale():
    event, avail, err = validate_source_timestamp(1.8e12, 1.8e12, source_id="test")
    assert event is None
    assert avail is None


def test_validate_source_timestamp_rejects_boolean():
    event, avail, err = validate_source_timestamp(True, _TS + 3600, source_id="test")
    assert event is None


def test_validate_source_timestamp_rejects_nan():
    event, avail, err = validate_source_timestamp(float("nan"), _TS + 3600, source_id="test")
    assert event is None


# ── daily OHLCV adapter ────────────────────────────────────────────────────

def test_daily_ohlcv_observation_valid():
    bar = {"ts": 1_720_000_000, "open": 100.0, "high": 105.0, "low": 99.0, "close": 103.0, "volume": 1_000_000}
    obs = daily_ohlcv_observation(bar, prediction_ts=1_730_000_000)
    assert obs is not None
    assert obs.source_id == "daily_ohlcv"
    assert obs.values["close"] == 103.0


def test_daily_ohlcv_observation_rejects_future_bar():
    bar = {"ts": 1_730_000_000, "close": 100.0}
    obs = daily_ohlcv_observation(bar, prediction_ts=1_720_000_000)
    assert obs is None


def test_daily_ohlcv_observation_rejects_missing_ts():
    bar = {"close": 100.0}
    obs = daily_ohlcv_observation(bar)
    assert obs is None


# ── news event adapter ─────────────────────────────────────────────────────

def test_news_event_observation_valid():
    # Use a prediction_ts close enough to avoid staleness rejection
    event = {"asof_ts": _TS, "ingested_at": _TS + 100,
             "materiality": 0.8, "confidence": 0.7,
             "event_type": "guidance_cut", "direction_hint": "bearish"}
    obs = news_event_observation(event, prediction_ts=_TS + 3600)
    assert obs is not None
    assert obs.source_id == "news_events"
    assert obs.values["materiality"] == 0.8


def test_news_event_observation_rejects_future_event():
    event = {"asof_ts": _TS + 7200, "ingested_at": _TS + 7300}
    obs = news_event_observation(event, prediction_ts=_TS)
    assert obs is None


def test_news_event_observation_rejects_peer_derived_event():
    event = {
        "asof_ts": _TS, "ingested_at": _TS + 100,
        "materiality": 1.0, "confidence": 1.0,
        "event_type": "fda_approval", "direction_hint": "bullish",
        "derived": True, "origin_symbol": "MRNA", "decision_eligible": False,
    }
    assert news_event_observation(event, prediction_ts=_TS + 3600) is None


# ── DatasetManifest ────────────────────────────────────────────────────────

def test_build_dataset_manifest():
    samples = [
        _make_sample(symbol="WOLF", outcome="WIN"),
        _make_sample(symbol="WOLF", outcome="LOSS", label=0,
                     prediction_ts=1_720_000_001, label_ts=1_720_300_001),
        _make_sample(symbol="WOLF", outcome="DATA_INVALID", label=0,
                     prediction_ts=1_720_000_002, label_ts=1_720_300_002),
    ]
    manifest = build_dataset_manifest(
        contract_id="abc123",
        symbols=["WOLF"],
        date_range_start="2024-01-01",
        date_range_end="2024-06-30",
        feature_schema="test_v1",
        feature_order=["rsi", "macd"],
        samples=samples,
        rejected={"stale_source": 1},
    )
    assert manifest.sample_count == 3
    assert manifest.accepted_count == 2
    assert manifest.rejected_count == 1
    assert len(manifest.manifest_sha) == 64
    assert manifest.symbols == ("WOLF",)


def test_build_manifest_rejects_count_mismatch():
    # Directly construct a DatasetManifest with mismatched counts
    with pytest.raises(ValueError, match="accepted"):
        DatasetManifest(
            manifest_sha="a" * 64,
            contract_id="abc",
            symbols=("WOLF",),
            date_range_start="2024-01-01",
            date_range_end="2024-01-02",
            source_watermark_ts=_TS,
            feature_schema="v1",
            feature_order=("rsi",),
            sample_count=10,
            accepted_count=5,
            rejected_count=3,  # 5 + 3 != 10
            rejection_reasons={},
        )


def test_manifest_is_frozen():
    samples = [_make_sample()]
    m = build_dataset_manifest(
        contract_id="abc", symbols=["WOLF"],
        date_range_start="2024-01-01", date_range_end="2024-01-02",
        feature_schema="v1", feature_order=["rsi"],
        samples=samples, rejected={},
    )
    with pytest.raises(Exception):
        m.sample_count = 99  # type: ignore
