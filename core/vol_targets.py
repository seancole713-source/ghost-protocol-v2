"""Shared volatility targets for UP trades — must match live TP/SL in prediction.py.

Training labels (_simulate_up_tp_sl) and live reconcile (core.tp_sl_resolve) both
derive target/stop from base_vol_pct + stop_pct_from_vol and use calendar
forward bars after entry (Phase 5: tp_sl_fwd_v1). Do not override live vol
from DB without also changing the label generator.

Phase 4: forecast_band_vol_pct widens scorecard OHLC bands from recent realized
range — telemetry only; pick TP/SL still use base_vol_pct.
"""
from __future__ import annotations

import hashlib
import json
import os
import statistics
from typing import Any, Dict, List, Optional, Sequence

STOCK_DEFAULT_VOL_PCT = 0.020
NON_STOCK_DEFAULT_VOL_PCT = 0.025
VOL_MAP = {
    "WOLF": 0.025,
}


def base_vol_pct(symbol: str, asset_type: str) -> float:
    """Default target move fraction (e.g. 0.025 = +2.5%). Same defaults as predict_symbol."""
    default = (
        STOCK_DEFAULT_VOL_PCT
        if (asset_type or "").lower() == "stock"
        else NON_STOCK_DEFAULT_VOL_PCT
    )
    return float(VOL_MAP.get((symbol or "").upper(), default))


def _stop_vol_mult() -> float:
    """Stop distance as a multiple of the target vol fraction.

    0.65 (default) = tight stop, 1.54:1 reward:risk, breakeven win rate 40%.
    Higher values widen the stop: win rate rises (fewer stop-outs) while
    reward:risk falls. Phase 3 precision experiments sweep this to find the
    geometry where >=70% OOS precision is provable with positive expectancy.
    Training labels and live TP/SL both read it — they can never diverge.
    """
    try:
        return max(0.1, float(os.getenv("V3_STOP_VOL_MULT", "0.65")))
    except Exception:
        return 0.65


def stop_pct_from_vol(vol_pct: float) -> float:
    """Stop distance as fraction of entry (vol * V3_STOP_VOL_MULT)."""
    return float(vol_pct) * _stop_vol_mult()


def tp_sl_geometry_contract(*, hold_bars: Optional[int] = None) -> Dict[str, Any]:
    """Canonical label target/stop geometry that participates in model identity."""
    if hold_bars is None:
        from core.engine_config import V3_LABEL_HOLD_BARS
        hold_bars = V3_LABEL_HOLD_BARS
    return {
        "contract": "tp_sl_geometry_v1",
        "direction_formulas": {
            "DOWN": {"target": "entry*(1-vol)", "stop": "entry*(1+vol*stop_mult)"},
            "UP": {"target": "entry*(1+vol)", "stop": "entry*(1-vol*stop_mult)"},
        },
        "hold_bars": int(hold_bars),
        "non_stock_default_vol_pct": NON_STOCK_DEFAULT_VOL_PCT,
        "resolution": "daily_forward_after_entry;stop_first_on_same_bar;expiry_non_win",
        "stock_default_vol_pct": STOCK_DEFAULT_VOL_PCT,
        "stop_vol_mult": _stop_vol_mult(),
        "vol_map": {key: float(VOL_MAP[key]) for key in sorted(VOL_MAP)},
        # Adaptive bands change what every label MEANS, so they must change
        # model identity too. Recording the mode here makes the label_schema
        # hash differ, which makes model_serve_guard reject models trained
        # under the other geometry instead of serving them against labels they
        # were never fit for. Absent this, flipping the env var would silently
        # rewrite the contract beneath the stored fleet.
        "adaptive_vol": (
            {
                "enabled": True,
                "floor_pct": _adaptive_vol_floor("stock"),
                "cap_pct": _forecast_band_vol_cap("stock"),
                "realized_scale": _forecast_band_realized_scale(),
                "lookback_bars": _forecast_band_lookback(),
            }
            if adaptive_vol_enabled()
            else {"enabled": False}
        ),
    }


def tp_sl_geometry_schema(*, hold_bars: Optional[int] = None) -> str:
    payload = json.dumps(
        tp_sl_geometry_contract(hold_bars=hold_bars),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return "tp_sl_geometry_v1:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _forecast_band_lookback() -> int:
    try:
        return max(3, int(os.getenv("V3_FORECAST_BAND_LOOKBACK", "10")))
    except Exception:
        return 10


def _forecast_band_vol_cap(asset_type: str) -> float:
    try:
        default = 0.08 if (asset_type or "").lower() == "stock" else 0.12
        return max(0.03, float(os.getenv("V3_FORECAST_BAND_VOL_CAP", str(default))))
    except Exception:
        return 0.08


def _forecast_band_realized_scale() -> float:
    """Fraction of median daily (H-L)/close used when widening bands."""
    try:
        return max(0.5, float(os.getenv("V3_FORECAST_BAND_REALIZED_SCALE", "0.85")))
    except Exception:
        return 0.85


def adaptive_vol_enabled() -> bool:
    """Two-sided per-symbol TP/SL bands. Off by default: enabling changes the
    geometry contract, which changes every model's label_schema and requires a
    full retrain before anything can serve."""
    return (os.getenv("V3_ADAPTIVE_VOL_BANDS", "0") or "0").strip().lower() in (
        "1", "on", "true", "yes"
    )


def _adaptive_vol_floor(asset_type: str) -> float:
    """Smallest band a symbol may be given, so a pinned name still needs a real
    move rather than resolving on noise."""
    try:
        default = 0.006 if (asset_type or "").lower() == "stock" else 0.010
        return max(0.002, float(os.getenv("V3_ADAPTIVE_VOL_FLOOR", str(default))))
    except Exception:
        return 0.006


def adaptive_vol_pct(
    symbol: str,
    asset_type: str,
    rows: Optional[Sequence[Dict[str, Any]]] = None,
    *,
    end_idx: Optional[int] = None,
) -> Dict[str, Any]:
    """Per-symbol band that scales BOTH ways from realized range.

    forecast_band_vol_pct only ever widens -- `max(base, realized*scale)` -- so
    a quiet symbol keeps the flat 2%. That is the half of the problem PR #163
    did not solve, and it is the expensive half: a 2% target inside five bars is
    unreachable for a mega-cap or an event-pinned name, so the trade EXPIRES and
    expiry counts as a loss (SE-4). Ghost is then scored wrong on a trade that
    never actually ran.

    Measured over 4,424 resolved shadow outcomes on 2026-09-04: 6.6% expired
    overall, concentrated in a handful of symbols -- APGE 100% expired (49/49),
    JPM 74.4%, GOOG 58.3%, COST 37.8%, AMZN 34.1%, V 31.8%. Those are not bad
    predictions; they are an instrument that cannot register a reading. Pooled
    Wilson LB was 53.89% with them and 56.29% without.

    So this narrows as well as widens, floored so a pinned symbol still has to
    make a real move, and capped exactly as the widen-only path is.

    Point-in-time by construction: pass ``end_idx`` and only bars up to that
    index are used, so a label never sees range from after its own entry.
    """
    base = base_vol_pct(symbol, asset_type)
    out: Dict[str, Any] = {
        "vol_pct": round(base, 4),
        "base_vol_pct": round(base, 4),
        "realized_range_pct": None,
        "source": "base",
    }
    if not adaptive_vol_enabled():
        out["source"] = "base_adaptive_disabled"
        return out

    hist = list(rows or [])
    if end_idx is not None:
        hist = hist[: end_idx + 1]
    if len(hist) < 3:
        out["source"] = "base_insufficient_history"
        return out

    realized = median_realized_range_pct(hist, _forecast_band_lookback())
    if realized is None or realized <= 0:
        out["source"] = "base_no_realized_range"
        return out

    floor = _adaptive_vol_floor(asset_type)
    cap = _forecast_band_vol_cap(asset_type)
    scaled = float(realized) * _forecast_band_realized_scale()
    vol = min(max(scaled, floor), cap)

    out["vol_pct"] = round(vol, 4)
    out["realized_range_pct"] = round(float(realized), 4)
    out["floor_pct"] = round(floor, 4)
    out["cap_pct"] = round(cap, 4)
    if vol <= floor + 1e-9:
        out["source"] = "realized_range_floored"
    elif vol >= cap - 1e-9:
        out["source"] = "realized_range_capped"
    else:
        out["source"] = "realized_range"
    return out


def median_realized_range_pct(rows: Sequence[Dict[str, Any]], lookback: int) -> Optional[float]:
    """Median daily true range as fraction of close over the last ``lookback`` bars."""
    if not rows or lookback < 1:
        return None
    window = list(rows)[-lookback:]
    pcts: List[float] = []
    for bar in window:
        close = float(bar.get("close") or 0)
        if close <= 0:
            continue
        hi = float(bar.get("high") or close)
        lo = float(bar.get("low") or close)
        pcts.append(max(0.0, (hi - lo) / close))
    if not pcts:
        return None
    return float(statistics.median(pcts))


def forecast_band_vol_pct(
    symbol: str,
    asset_type: str,
    rows: Optional[Sequence[Dict[str, Any]]] = None,
    *,
    end_idx: Optional[int] = None,
) -> Dict[str, Any]:
    """Vol fraction for daily forecast OHLC bands (scorecard telemetry only).

    Uses max(base_vol_pct, scaled median recent range) capped at
    V3_FORECAST_BAND_VOL_CAP. Does not change training labels or live TP/SL.
    """
    base = base_vol_pct(symbol, asset_type)
    lookback = _forecast_band_lookback()
    cap = _forecast_band_vol_cap(asset_type)
    scale = _forecast_band_realized_scale()
    hist = list(rows or [])
    if end_idx is not None:
        hist = hist[: end_idx + 1]
    realized = median_realized_range_pct(hist, lookback) if len(hist) >= 3 else None
    widened = max(base, float(realized) * scale) if realized is not None else base
    vol = min(widened, cap)
    source = "base"
    if realized is not None and vol > base + 1e-9:
        source = "realized_range"
    elif vol >= cap - 1e-9 and realized is not None and realized * scale > cap:
        source = "realized_range_capped"
    return {
        "vol_pct": round(vol, 4),
        "base_vol_pct": round(base, 4),
        "realized_range_pct": round(realized, 4) if realized is not None else None,
        "lookback_bars": lookback,
        "cap_pct": round(cap, 4),
        "source": source,
    }
