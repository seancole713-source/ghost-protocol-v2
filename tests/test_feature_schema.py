"""Point-in-time feature timestamp integrity tests."""
import math
import os
import time
from datetime import datetime, timezone

import pytest

from core.feature_schema import FEATURE_ASOF_KEY, attach_feature_asof, feature_asof_unix


@pytest.mark.parametrize(
    "bad",
    [None, "", "not-a-timestamp", False, True, 0, -1, 1_700_000_000.5,
     math.nan, math.inf, -math.inf],
)
def test_feature_asof_invalid_historical_evidence_fails_closed(bad):
    assert feature_asof_unix(bad) == 0


def test_feature_asof_live_fallback_must_be_explicit(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1_800_000_000.9)
    assert feature_asof_unix(None, default_now=True) == 1_800_000_000
    features = {}
    assert attach_feature_asof(features, None)[FEATURE_ASOF_KEY] == 0
    assert attach_feature_asof(features, None, default_now=True)[FEATURE_ASOF_KEY] == 1_800_000_000


def test_feature_asof_rejects_millisecond_scale_timestamps():
    """1.8e12 is epoch-millis, not seconds — must fail closed."""
    assert feature_asof_unix(1_800_000_000_000) == 0


def test_feature_asof_rejects_far_future_timestamps(monkeypatch):
    """A timestamp 200 days in the future must fail closed."""
    monkeypatch.setattr(time, "time", lambda: 1_800_000_000.0)
    assert feature_asof_unix(1_800_000_000 + 200 * 86400) == 0


def test_feature_asof_accepts_recent_timestamp(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1_800_000_000.0)
    assert feature_asof_unix(1_800_000_000 - 3600) == 1_800_000_000 - 3600


def test_feature_asof_naive_iso_is_utc_across_host_timezones():
    expected = int(datetime(2026, 1, 15, 12, 30, tzinfo=timezone.utc).timestamp())
    old_tz = os.environ.get("TZ")
    try:
        for host_tz in ("UTC", "America/Los_Angeles"):
            os.environ["TZ"] = host_tz
            if hasattr(time, "tzset"):
                time.tzset()
            assert feature_asof_unix("2026-01-15T12:30:00") == expected
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        if hasattr(time, "tzset"):
            time.tzset()
