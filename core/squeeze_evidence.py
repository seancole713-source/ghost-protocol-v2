"""Squeeze market-data provenance; these are validity checks, not signal gates."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
import time
from zoneinfo import ZoneInfo
from typing import Any

# A 5-minute aggregate is timestamped at its beginning, not at the last trade.
# Allow one additional bar interval for publication; never relabel it a live tick.
BAR_MAX_AGE_S = 600
CONTRACT = "squeeze_bar_evidence_v1"
EVIDENCE_FIELDS = (
    "market_data_contract", "price_as_of_ts", "price_source", "price_feed",
    "price_timestamp_basis", "daily_feed", "intraday_feed", "reference_session_date",
    "bars_complete", "quote_status", "price_age_s", "volume_basis",
)


@dataclass
class BarFetch:
    bars: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    complete: bool = False
    feed: str | None = None
    reason: str | None = None
    pages: int = 0

    def summary(self) -> dict[str, Any]:
        return {"complete": self.complete, "feed": self.feed, "reason": self.reason, "pages": self.pages}


@dataclass
class MarketSnapshot:
    metrics: dict[str, dict[str, Any] | None] = field(default_factory=dict)
    statuses: dict[str, dict[str, Any]] = field(default_factory=dict)


def observation_ts(value: Any) -> float | None:
    try:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            result = float(value)
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return None
            result = dt.timestamp()
        return result if math.isfinite(result) and result > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def evidence_status(metrics: dict[str, Any], *, now: float | None = None) -> str:
    """Recheck clocks at consumption, not just at the beginning of a scan."""
    from core.daily_bar_contract import previous_session

    now = time.time() if now is None else now
    ts = observation_ts(metrics.get("price_as_of_ts"))
    if ts is None or ts > now or metrics.get("data_stale") is True:
        return "invalid_quote"
    # Exchange session dates are Eastern, the same basis as daily_bar_contract.bar_session_date
    # (which picks the baseline). Mixing in the Central wall clock here disagreed with it for
    # the hour between midnight ET and midnight CT and rejected a valid baseline.
    et = ZoneInfo("America/New_York")
    today_et = datetime.fromtimestamp(now, et).date()
    if now - ts > BAR_MAX_AGE_S or datetime.fromtimestamp(ts, et).date() != today_et:
        return "stale_quote"
    try:
        for name in ("price", "prior_close", "session_high", "avg_daily_volume"):
            value = float(metrics[name])
            if not math.isfinite(value) or value <= 0:
                return "invalid_quote"
        for name in ("session_volume", "current_move_pct", "peak_move_pct"):
            if not math.isfinite(float(metrics[name])):
                return "invalid_quote"
        if float(metrics["session_volume"]) < 0:
            return "invalid_quote"
    except (ValueError, TypeError, KeyError, OverflowError):
        return "invalid_quote"
    expected = previous_session(today_et).isoformat()
    if (
        metrics.get("reference_session_date") != expected
        or metrics.get("bars_complete") is not True
        or not metrics.get("daily_feed")
        or metrics.get("daily_feed") != metrics.get("intraday_feed")
    ):
        return "invalid_baseline"
    return "ready"
