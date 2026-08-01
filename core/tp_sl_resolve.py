"""Shared TP/SL resolution — training labels and live reconcile use the same rules."""
from __future__ import annotations

import os
import time
import numpy as np
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import pytz
except ImportError:
    pytz = None  # type: ignore


def _date_key(ts: Any) -> str:
    """Canonical UTC date for ISO or Unix-second provider timestamps."""
    if isinstance(ts, bool) or ts is None:
        return ""
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc).date().isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            return ""
    value = str(ts).strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return ""


def _daily_bar_available_ts(ts: Any) -> int:
    """Timestamp when a daily OHLC bar can safely be treated as known.

    Provider timestamps often identify a daily bar at midnight. Its high/low
    is not observable then, so evidence is anchored no earlier than 21:00 UTC
    (16:00 America/New_York in standard time; conservative during DST).
    """
    from core.feature_schema import feature_asof_unix

    parsed_ts = feature_asof_unix(ts)
    date_key = _date_key(ts)
    if not parsed_ts or not date_key:
        return 0
    close_floor = int(datetime.strptime(date_key, "%Y-%m-%d").replace(
        hour=21, tzinfo=timezone.utc,
    ).timestamp())
    return max(parsed_ts, close_floor)


def resolve_tp_sl_bar_path_detail(
    bars: Sequence[Dict[str, Any]],
    target: float,
    stop: float,
    direction: str = "UP",
    max_bars: Optional[int] = None,
) -> Tuple[Optional[str], Optional[int], Optional[int]]:
    """Return outcome, information-available timestamp, and resolution bar."""
    if target <= 0 or stop <= 0:
        return None, None, None

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
        outcome = "LOSS" if hit_stop else ("WIN" if hit_tgt else None)
        if outcome:
            resolution_ts = _daily_bar_available_ts(bars[j].get("ts"))
            return outcome, (resolution_ts or None), j
    return None, None, None


def resolve_tp_sl_bar_path(
    bars: Sequence[Dict[str, Any]],
    target: float,
    stop: float,
    direction: str = "UP",
    max_bars: Optional[int] = None,
) -> Optional[str]:
    """Path simulation on daily OHLC. Conservative same-bar rule: both touched -> LOSS."""
    return resolve_tp_sl_bar_path_detail(
        bars, target, stop, direction, max_bars,
    )[0]


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
    *,
    available_asof: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Distinct forward daily bars, optionally limited to completed evidence."""
    entry_date = datetime.fromtimestamp(predicted_at, tz=timezone.utc).date()
    out: List[Dict[str, Any]] = []
    seen_dates = set()
    for row in rows or []:
        try:
            bar_date = datetime.strptime(_date_key(row.get("ts")), "%Y-%m-%d").date()
        except Exception:
            continue
        if bar_date <= entry_date:
            continue
        if available_asof is not None:
            available_ts = _daily_bar_available_ts(row.get("ts"))
            if not available_ts or available_ts > int(available_asof):
                continue
        # The contract is daily bars. Distinct intraday timestamps from one
        # calendar date cannot manufacture several days of horizon maturity.
        if bar_date in seen_dates:
            continue
        seen_dates.add(bar_date)
        out.append(row)
        if len(out) >= hold_bars:
            break
    return out


def resolve_open_prediction_detail(
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
) -> Tuple[Optional[str], Optional[int], Optional[float]]:
    """Return outcome, evidence timestamp, and evidence exit price.

    Completed daily bars have priority because they preserve the conservative
    same-bar collision rule. A current snapshot can still prove TP/SL while the
    daily horizon is incomplete. EXPIRED requires every promised daily bar and
    uses the final horizon close, never a later reconciliation quote.
    """
    required_bars = max(1, int(hold_bars))
    evidence_now = int(time.time()) if now is None else int(now)
    bars = daily_bars or []
    if bars:
        fwd = forward_bars_after_entry(
            bars, predicted_at, required_bars, available_asof=evidence_now,
        )
        outcome, resolved_at, _bar_idx = resolve_tp_sl_bar_path_detail(
            fwd, target, stop, direction, max_bars=required_bars,
        )
        if outcome:
            exit_price = target if outcome == "WIN" else stop
            return outcome, resolved_at, float(exit_price)
        if len(fwd) >= required_bars:
            try:
                horizon_close = float(fwd[required_bars - 1].get("close") or 0)
            except (TypeError, ValueError, OverflowError):
                horizon_close = 0.0
            resolved_at = _daily_bar_available_ts(fwd[required_bars - 1].get("ts"))
            if resolved_at:
                return "EXPIRED", resolved_at, (horizon_close or None)
            return None, None, None

    if snapshot_price is not None:
        snap = resolve_tp_sl_snapshot(snapshot_price, target, stop, direction)
        if snap:
            exit_price = target if snap == "WIN" else stop
            return snap, evidence_now, float(exit_price)
    return None, None, None


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
    """Compatibility outcome API over evidence-detailed resolution."""
    return resolve_open_prediction_detail(
        direction=direction,
        target=target,
        stop=stop,
        predicted_at=predicted_at,
        hold_bars=hold_bars,
        daily_bars=daily_bars,
        snapshot_price=snapshot_price,
        now=now,
        expires_at=expires_at,
    )[0]


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


# Phase 5: calendar forward-bar selection + shared resolve path (forces retrain on bump).
LABEL_SCHEMA = "tp_sl_fwd_v1"


def tp_sl_prices_from_vol(
    entry: float,
    vol_pct: float,
    direction: str = "UP",
) -> tuple[float, float]:
    """Target/stop from entry and vol fraction — same math as prediction._predict_symbol_ex."""
    from core.vol_targets import stop_pct_from_vol

    if entry <= 0:
        return 0.0, 0.0
    direction = (direction or "UP").upper()
    stop_pct = stop_pct_from_vol(vol_pct)
    if direction == "UP":
        return entry * (1 + vol_pct), entry * (1 - stop_pct)
    return entry * (1 - vol_pct), entry * (1 + stop_pct)


def entry_predicted_at(rows: Sequence[Dict[str, Any]], entry_idx: int) -> int:
    """Unix anchor for forward-bar selection — entry bar close (matches feature_asof_ts)."""
    from core.feature_schema import feature_asof_unix

    if entry_idx < 0 or entry_idx >= len(rows):
        return int(datetime.now(tz=timezone.utc).timestamp())
    return feature_asof_unix(rows[entry_idx].get("ts"))


def simulate_direction_label(
    rows: Sequence[Dict[str, Any]],
    entry_idx: int,
    hold_bars: int,
    direction: str = "UP",
) -> Tuple[Optional[str], Optional[int]]:
    """Simple direction label for an explicit UP or DOWN research lane.

    No path dependency, no TP/SL geometry. The model predicts whether the
    price will be higher after hold_bars days. Natural rate is ~50% for a
    random walk — any edge above 50% is real predictive power.

    Returns (outcome, resolution_ts) or (None, None) for incomplete horizons.
    """
    if entry_idx < 0 or entry_idx >= len(rows):
        return None, None
    entry = float(rows[entry_idx].get("close") or 0)
    if entry <= 0:
        return None, None
    predicted_at = entry_predicted_at(rows, entry_idx)
    fwd = forward_bars_after_entry(rows, predicted_at, hold_bars)
    if len(fwd) < max(1, int(hold_bars)):
        return None, None
    final_close = float(fwd[hold_bars - 1].get("close") or 0)
    if final_close <= 0:
        return None, None
    lane = str(direction or "").upper()
    if lane not in ("UP", "DOWN"):
        return None, None
    won = final_close > entry if lane == "UP" else final_close < entry
    outcome = "WIN" if won else "LOSS"
    resolution_ts = _daily_bar_available_ts(fwd[hold_bars - 1].get("ts"))
    return outcome, (resolution_ts or None)


def simulate_cross_sectional_label(
    symbol_forward_returns: Dict[str, float],
    symbol: str,
) -> Tuple[Optional[str], Optional[int]]:
    """Cross-sectional label: WIN if this symbol's forward return ranks above
    the cross-sectional median, LOSS otherwise.

    This is a RELATIVE prediction — the model learns which stocks will
    outperform their peers, not whether any individual stock will go up.
    Natural win rate is exactly 50% by construction (median split).
    Any edge above 50% is real predictive power about relative strength.

    Returns (outcome, None) — resolution timestamp is not applicable since
    the label is derived from the cross-section, not a single bar path.
    """
    sym = (symbol or "").upper()
    ret = symbol_forward_returns.get(sym)
    if ret is None:
        return None, None
    all_rets = [v for v in symbol_forward_returns.values() if v is not None]
    if len(all_rets) < 3:
        return None, None
    median = sorted(all_rets)[len(all_rets) // 2]
    return ("WIN" if ret > median else "LOSS"), None


def simulate_volatility_label(
    rows: Sequence[Dict[str, Any]],
    entry_idx: int,
    hold_bars: int,
    lookback_bars: int = 10,
) -> Tuple[Optional[str], Optional[int]]:
    """Volatility regime label: WIN if forward vol > trailing vol, else LOSS.

    Predicts whether the next hold_bars days will be more volatile than the
    past lookback_bars days. Volatility clusters (GARCH effect), making this
    more predictable than direction. Natural win rate is ~55-65% due to
    volatility persistence.

    Returns (outcome, resolution_ts) or (None, None) for incomplete horizons.
    """
    if entry_idx < lookback_bars or entry_idx >= len(rows):
        return None, None
    # Trailing vol: std of daily returns over lookback
    trailing_closes = [float(rows[j].get("close") or 0) for j in range(entry_idx - lookback_bars, entry_idx + 1)]
    if min(trailing_closes) <= 0:
        return None, None
    trailing_rets = [(trailing_closes[i+1] - trailing_closes[i]) / trailing_closes[i] for i in range(len(trailing_closes) - 1)]
    if len(trailing_rets) < 3:
        return None, None
    trailing_vol = float(np.std(trailing_rets))
    if trailing_vol <= 0:
        return None, None
    # Forward vol: std of daily returns over hold_bars
    predicted_at = entry_predicted_at(rows, entry_idx)
    fwd = forward_bars_after_entry(rows, predicted_at, hold_bars)
    if len(fwd) < max(1, int(hold_bars)):
        return None, None
    fwd_closes = [float(rows[entry_idx].get("close") or 0)] + [float(b.get("close") or 0) for b in fwd]
    if min(fwd_closes) <= 0:
        return None, None
    fwd_rets = [(fwd_closes[i+1] - fwd_closes[i]) / fwd_closes[i] for i in range(len(fwd_closes) - 1)]
    if len(fwd_rets) < 2:
        return None, None
    fwd_vol = float(np.std(fwd_rets))
    if fwd_vol <= 0:
        return None, None
    outcome = "WIN" if fwd_vol > trailing_vol else "LOSS"
    resolution_ts = _daily_bar_available_ts(fwd[hold_bars - 1].get("ts"))
    return outcome, (resolution_ts or None)


def simulate_tp_sl_label_detail(
    rows: Sequence[Dict[str, Any]],
    entry_idx: int,
    hold_bars: int,
    vol_pct: float,
    direction: str = "UP",
) -> Tuple[Optional[str], Optional[int]]:
    """Return a completed training outcome and the exact bar when it became known.

    A no-hit path is EXPIRED only after all promised forward bars are present.
    Incomplete horizons return ``(None, None)`` and cannot enter evidence.
    """
    if entry_idx < 0 or entry_idx >= len(rows):
        return None, None
    entry = float(rows[entry_idx].get("close") or 0)
    if entry <= 0:
        return None, None
    target, stop = tp_sl_prices_from_vol(entry, vol_pct, direction)
    if target <= 0 or stop <= 0:
        return None, None
    predicted_at = entry_predicted_at(rows, entry_idx)
    fwd = forward_bars_after_entry(rows, predicted_at, hold_bars)
    if len(fwd) < max(1, int(hold_bars)):
        return None, None
    outcome, resolution_ts, _bar_idx = resolve_tp_sl_bar_path_detail(
        fwd, target, stop, direction, max_bars=hold_bars,
    )
    if outcome:
        return outcome, resolution_ts
    expiry_ts = _daily_bar_available_ts(fwd[hold_bars - 1].get("ts"))
    return "EXPIRED", (expiry_ts or None)


def simulate_tp_sl_label(
    rows: Sequence[Dict[str, Any]],
    entry_idx: int,
    hold_bars: int,
    vol_pct: float,
    direction: str = "UP",
) -> str:
    """Compatibility outcome API; incomplete horizons are explicit, not expiries."""
    outcome, _resolution_ts = simulate_tp_sl_label_detail(
        rows, entry_idx, hold_bars, vol_pct, direction,
    )
    return outcome or "INCOMPLETE"


def simulate_down_tp_sl_label(
    rows: Sequence[Dict[str, Any]],
    entry_idx: int,
    hold_bars: int,
    vol_pct: float,
) -> str:
    """DOWN label generator — same forward-window rules as UP, but target is below entry.

    Phase 0 (PR #115): Ghost was structurally unable to bet down. This provides
    the training labels for a DOWN model so the engine can be accurate in bear
    markets instead of only being silent.
    """
    return simulate_tp_sl_label(rows, entry_idx, hold_bars, vol_pct, direction="DOWN")


def reconcile_training_label(
    *,
    rows: Sequence[Dict[str, Any]],
    entry_idx: int,
    hold_bars: int,
    vol_pct: float,
    direction: str = "UP",
    now: Optional[int] = None,
) -> str:
    """Compatibility wrapper over the canonical completed-label simulator."""
    outcome, _resolved_at = simulate_tp_sl_label_detail(
        rows, entry_idx, hold_bars, vol_pct, direction,
    )
    return outcome or "INCOMPLETE"
