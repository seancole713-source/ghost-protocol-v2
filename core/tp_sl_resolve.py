"""Shared TP/SL resolution — training labels and live reconcile use the same rules."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import pytz
except ImportError:
    pytz = None  # type: ignore


def _date_key(ts: Any) -> str:
    return str(ts or "")[:10]


def resolve_tp_sl_bar_path(
    bars: Sequence[Dict[str, Any]],
    target: float,
    stop: float,
    direction: str = "UP",
    max_bars: Optional[int] = None,
) -> Optional[str]:
    """Path simulation on daily OHLC. Conservative same-bar rule: both touched -> LOSS.

    Returns WIN, LOSS, or None if the path is still open within ``max_bars``.
    """
    if target <= 0 or stop <= 0:
        return None
    direction = (direction or "UP").upper()
    n = len(bars) if max_bars is None else min(len(bars), max_bars)
    for j in range(n):
        lo = float(bars[j]["low"])
        hi = float(bars[j]["high"])
        if direction == "UP":
            hit_stop = lo <= stop
            hit_tgt = hi >= target
        else:
            hit_stop = hi >= stop
            hit_tgt = lo <= target
        if hit_stop and hit_tgt:
            return "LOSS"
        if hit_stop:
            return "LOSS"
        if hit_tgt:
            return "WIN"
    return None


def resolve_tp_sl_snapshot(
    price: float,
    target: float,
    stop: float,
    direction: str = "UP",
) -> Optional[str]:
    """Single-price check when daily bars are unavailable (legacy fallback)."""
    if not price or price <= 0:
        return None
    direction = (direction or "UP").upper()
    if direction == "UP":
        if price >= target:
            return "WIN"
        if price <= stop:
            return "LOSS"
    else:
        if price <= target:
            return "WIN"
        if price >= stop:
            return "LOSS"
    return None


def forward_bars_after_entry(
    rows: Sequence[Dict[str, Any]],
    predicted_at: int,
    hold_bars: int,
) -> List[Dict[str, Any]]:
    """Daily bars strictly after the entry calendar day (matches training entry at bar close)."""
    entry_date = datetime.fromtimestamp(predicted_at, tz=timezone.utc).date()
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        try:
            bar_date = datetime.strptime(_date_key(row.get("ts")), "%Y-%m-%d").date()
        except Exception:
            continue
        if bar_date > entry_date:
            out.append(row)
        if len(out) >= hold_bars:
            break
    return out


def resolve_open_prediction(
    *,
    direction: str,
    target: float,
    stop: float,
    predicted_at: int,
    hold_bars: int,
    daily_bars: Optional[Sequence[Dict[str, Any]]] = None,
    snapshot_price: Optional[float] = None,
    now: Optional[int] = None,
    expires_at: Optional[int] = None,
) -> Optional[str]:
    """Resolve an open pick: daily bar-path when OHLC is available, snapshot fallback otherwise."""
    ts = now if now is not None else int(datetime.now(tz=timezone.utc).timestamp())
    bars = daily_bars or []
    if bars:
        fwd = forward_bars_after_entry(bars, predicted_at, hold_bars)
        outcome = resolve_tp_sl_bar_path(fwd, target, stop, direction, max_bars=hold_bars)
        if outcome:
            return outcome
        if expires_at and ts > expires_at:
            return "EXPIRED"
        return None
    if snapshot_price is not None:
        snap = resolve_tp_sl_snapshot(snapshot_price, target, stop, direction)
        if snap:
            return snap
    if expires_at and ts > expires_at:
        return "EXPIRED"
    return None


def expires_at_nth_trading_close(from_ts: int, hold_bars: int) -> int:
    """Close of the Nth trading day after ``from_ts`` (America/Chicago), matching label horizon."""
    hold_bars = max(1, int(hold_bars))
    if pytz is None:
        return from_ts + hold_bars * 86400
    tz = pytz.timezone("America/Chicago")
    cur = datetime.fromtimestamp(from_ts, tz=tz)
    counted = 0
    while counted < hold_bars:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            counted += 1
    close = cur.replace(hour=16, minute=0, second=0, microsecond=0)
    return int(close.timestamp())


def label_hold_bars() -> int:
    """Same default as core.signal_engine.V3_LABEL_HOLD_BARS (avoid import cycle at module load)."""
    return max(1, int(os.getenv("V3_LABEL_HOLD_BARS", "3")))
