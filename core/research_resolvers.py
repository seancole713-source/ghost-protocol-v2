"""core/research_resolvers.py — task-specific outcome resolvers (Phase 4).

Each resolver is a pure function that accepts a prediction context and returns
a canonical ResearchResolution. Resolvers are registered by exact ID/version
and validate their own output domain. They reuse only pure bar/timestamp
helpers from core.tp_sl_resolve — never the live ledgers.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Sequence

LOGGER = logging.getLogger("ghost.research_resolvers")


@dataclass(frozen=True)
class ResearchResolution:
    """One resolved outcome for a research prediction."""
    outcome: str            # WIN, LOSS, EXPIRED, DATA_INVALID
    observed_value: Optional[float]  # e.g. realized return, vol ratio
    resolved_ts: int        # when the outcome became knowable
    available_ts: int       # when the evidence became available
    evidence: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def __post_init__(self):
        if self.outcome not in ("WIN", "LOSS", "EXPIRED", "DATA_INVALID"):
            raise ValueError(f"Invalid outcome: {self.outcome}")
        if self.resolved_ts <= 0:
            raise ValueError(f"resolved_ts must be > 0, got {self.resolved_ts}")
        if self.available_ts <= 0:
            raise ValueError(f"available_ts must be > 0, got {self.available_ts}")


# ── resolver protocol ──────────────────────────────────────────────────────

ResolverFn = Callable[..., Optional[ResearchResolution]]

_RESOLVER_REGISTRY: Dict[str, ResolverFn] = {}


def register_resolver(resolver_id: str, resolver_version: str, fn: ResolverFn) -> None:
    """Register a resolver function by exact ID and version."""
    key = f"{resolver_id}@{resolver_version}"
    if key in _RESOLVER_REGISTRY:
        raise ValueError(f"Resolver {key} already registered")
    _RESOLVER_REGISTRY[key] = fn
    LOGGER.info("Registered resolver %s", key)


def get_resolver(resolver_id: str, resolver_version: str) -> Optional[ResolverFn]:
    """Look up a registered resolver."""
    return _RESOLVER_REGISTRY.get(f"{resolver_id}@{resolver_version}")


def resolve_pending_tp_sl_prediction(
    prediction: Dict[str, Any],
    *,
    daily_bars: Optional[Sequence[Dict[str, Any]]] = None,
    now: Optional[int] = None,
) -> Optional[ResearchResolution]:
    """Resolve one pending TP/SL row from its geometry frozen at issuance."""
    import time as _time

    try:
        issued_ts = int(prediction.get("issued_ts") or 0)
    except (TypeError, ValueError, OverflowError):
        issued_ts = 0
    evidence_now = max(int(now or _time.time()), issued_ts + 1)
    context = prediction.get("context")
    if not isinstance(context, dict):
        context = {}
    try:
        entry_price = float(context["entry_price"])
        target_price = float(context["target_price"])
        stop_price = float(context["stop_price"])
        hold_bars = int(context["hold_bars"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return ResearchResolution(
            outcome="DATA_INVALID",
            observed_value=None,
            resolved_ts=evidence_now,
            available_ts=evidence_now,
            reason="missing_frozen_tp_sl_context",
        )
    if issued_ts <= 0 or hold_bars < 1:
        return ResearchResolution(
            outcome="DATA_INVALID",
            observed_value=None,
            resolved_ts=evidence_now,
            available_ts=evidence_now,
            reason="invalid_frozen_tp_sl_context",
        )
    resolver = get_resolver("tp_sl_bar_path/v1", "1.0.0")
    if resolver is None:
        return None
    return resolver(
        symbol=str(prediction.get("symbol") or "").upper(),
        direction=str(prediction.get("direction") or "").upper(),
        entry_price=entry_price,
        target_price=target_price,
        stop_price=stop_price,
        predicted_at=issued_ts,
        hold_bars=hold_bars,
        daily_bars=daily_bars,
        now=evidence_now,
    )


# ── TP/SL bar-path resolver ─────────────────────────────────────────────────

def resolve_tp_sl_swing(
    *,
    symbol: str,
    direction: str,
    entry_price: float,
    target_price: float,
    stop_price: float,
    predicted_at: int,
    hold_bars: int,
    daily_bars: Optional[Sequence[Dict[str, Any]]] = None,
    now: Optional[int] = None,
) -> Optional[ResearchResolution]:
    """Resolve a TP/SL swing prediction using daily bar-path rules.

    Reuses the canonical resolve_tp_sl_bar_path_detail from core.tp_sl_resolve.
    Same-bar target+stop collision is LOSS. No touch after complete horizon is
    EXPIRED. Incomplete horizons return None (not yet mature).
    """
    from core.tp_sl_resolve import (
        resolve_open_prediction_detail,
    )

    direction = (direction or "UP").upper()
    if direction not in ("UP", "DOWN"):
        return None
    if entry_price <= 0 or target_price <= 0 or stop_price <= 0:
        return None

    evidence_now = now or __import__("time").time()
    outcome, resolved_at, exit_price = resolve_open_prediction_detail(
        direction=direction,
        target=target_price,
        stop=stop_price,
        predicted_at=predicted_at,
        hold_bars=hold_bars,
        daily_bars=daily_bars,
        now=int(evidence_now),
    )

    if outcome is None:
        return None  # not yet mature

    return ResearchResolution(
        outcome=outcome,
        observed_value=float(exit_price) if exit_price else None,
        resolved_ts=resolved_at or int(evidence_now),
        available_ts=resolved_at or int(evidence_now),
        evidence={
            "entry_price": entry_price,
            "target_price": target_price,
            "stop_price": stop_price,
            "exit_price": exit_price,
        },
        reason=f"bar_path:{outcome}",
    )


# ── intraday continuation resolver ──────────────────────────────────────────

def resolve_intraday_continuation(
    *,
    symbol: str,
    direction: str,
    entry_price: float,
    predicted_at: int,
    hourly_bars: Optional[Sequence[Dict[str, Any]]] = None,
    cost_band_pct: float = 0.001,  # 10bp cost/neutral band
    now: Optional[int] = None,
) -> Optional[ResearchResolution]:
    """Resolve an intraday continuation prediction.

    Entry at first eligible 1-hour bar after issuance. Fixed 60-minute horizon.
    Correct directional close-to-close net return after cost band is WIN.
    Missing/incomplete session evidence is DATA_INVALID.
    """
    import time as _time
    from core.feature_schema import feature_asof_unix

    direction = (direction or "UP").upper()
    if direction not in ("UP", "DOWN"):
        return None
    if entry_price <= 0:
        return None

    evidence_now = int(now or _time.time())
    if not hourly_bars:
        return ResearchResolution(
            outcome="DATA_INVALID",
            observed_value=None,
            resolved_ts=evidence_now,
            available_ts=evidence_now,
            reason="no_hourly_bars",
        )

    # Find the first bar after predicted_at
    entry_bar = None
    exit_bar = None
    for i, bar in enumerate(hourly_bars):
        bar_ts = feature_asof_unix(bar.get("ts"), default_now=False)
        if bar_ts == 0:
            continue
        if bar_ts > predicted_at:
            if entry_bar is None:
                entry_bar = dict(bar)
                entry_bar["_parsed_ts"] = bar_ts
            elif exit_bar is None and bar_ts >= entry_bar["_parsed_ts"] + 3600:
                exit_bar = bar
                break

    if entry_bar is None:
        return ResearchResolution(
            outcome="DATA_INVALID",
            observed_value=None,
            resolved_ts=evidence_now,
            available_ts=evidence_now,
            reason="no_entry_bar_after_prediction",
        )

    entry_close = float(entry_bar.get("close", 0))
    if entry_close <= 0:
        return ResearchResolution(
            outcome="DATA_INVALID",
            observed_value=None,
            resolved_ts=evidence_now,
            available_ts=evidence_now,
            reason="invalid_entry_close",
        )

    if exit_bar is None:
        return None  # not yet mature — wait for the exit bar

    exit_close = float(exit_bar.get("close", 0))
    if exit_close <= 0:
        return ResearchResolution(
            outcome="DATA_INVALID",
            observed_value=None,
            resolved_ts=evidence_now,
            available_ts=evidence_now,
            reason="invalid_exit_close",
        )

    net_return = (exit_close - entry_close) / entry_close
    exit_ts = feature_asof_unix(exit_bar.get("ts"), default_now=False)

    if direction == "UP":
        won = net_return > cost_band_pct
    else:
        won = net_return < -cost_band_pct

    return ResearchResolution(
        outcome="WIN" if won else "LOSS",
        observed_value=round(net_return, 6),
        resolved_ts=exit_ts or evidence_now,
        available_ts=exit_ts or evidence_now,
        evidence={
            "entry_close": entry_close,
            "exit_close": exit_close,
            "net_return": round(net_return, 6),
            "cost_band_pct": cost_band_pct,
        },
        reason=f"intraday:{'WIN' if won else 'LOSS'}",
    )


# ── volatility expansion resolver ──────────────────────────────────────────

def resolve_volatility_expansion(
    *,
    symbol: str,
    predicted_at: int,
    hold_bars: int = 5,
    lookback_bars: int = 10,
    expansion_ratio: float = 1.0,
    daily_bars: Optional[Sequence[Dict[str, Any]]] = None,
    now: Optional[int] = None,
) -> Optional[ResearchResolution]:
    """Resolve a volatility expansion prediction.

    Compare annualized realized vol over next hold_bars days with prior
    lookback_bars days. EXPAND wins if forward_vol > trailing_vol * ratio.
    Never emits UP/DOWN or prices.
    """
    import time as _time
    import numpy as np
    from core.tp_sl_resolve import forward_bars_after_entry, _daily_bar_available_ts

    evidence_now = int(now or _time.time())
    if not daily_bars:
        return ResearchResolution(
            outcome="DATA_INVALID",
            observed_value=None,
            resolved_ts=evidence_now,
            available_ts=evidence_now,
            reason="no_daily_bars",
        )

    # Find the entry bar index
    entry_idx = None
    for i, bar in enumerate(daily_bars):
        bar_ts = _daily_bar_available_ts(bar.get("ts"))
        if bar_ts > predicted_at:
            entry_idx = i
            break

    if entry_idx is None or entry_idx < lookback_bars:
        return ResearchResolution(
            outcome="DATA_INVALID",
            observed_value=None,
            resolved_ts=evidence_now,
            available_ts=evidence_now,
            reason="insufficient_history",
        )

    # Trailing vol
    trailing_closes = [
        float(daily_bars[j].get("close", 0))
        for j in range(entry_idx - lookback_bars, entry_idx + 1)
    ]
    if min(trailing_closes) <= 0:
        return ResearchResolution(
            outcome="DATA_INVALID",
            observed_value=None,
            resolved_ts=evidence_now,
            available_ts=evidence_now,
            reason="invalid_trailing_prices",
        )

    trailing_rets = [
        (trailing_closes[i + 1] - trailing_closes[i]) / trailing_closes[i]
        for i in range(len(trailing_closes) - 1)
    ]
    if len(trailing_rets) < 3:
        return ResearchResolution(
            outcome="DATA_INVALID",
            observed_value=None,
            resolved_ts=evidence_now,
            available_ts=evidence_now,
            reason="too_few_trailing_returns",
        )

    trailing_vol = float(np.std(trailing_rets)) * math.sqrt(252)
    if trailing_vol <= 0:
        return ResearchResolution(
            outcome="DATA_INVALID",
            observed_value=None,
            resolved_ts=evidence_now,
            available_ts=evidence_now,
            reason="zero_trailing_vol",
        )

    # Forward bars
    fwd = forward_bars_after_entry(daily_bars, predicted_at, hold_bars)
    if len(fwd) < hold_bars:
        return None  # not yet mature

    fwd_closes = [float(daily_bars[entry_idx].get("close", 0))] + [
        float(b.get("close", 0)) for b in fwd
    ]
    if min(fwd_closes) <= 0:
        return ResearchResolution(
            outcome="DATA_INVALID",
            observed_value=None,
            resolved_ts=evidence_now,
            available_ts=evidence_now,
            reason="invalid_forward_prices",
        )

    fwd_rets = [
        (fwd_closes[i + 1] - fwd_closes[i]) / fwd_closes[i]
        for i in range(len(fwd_closes) - 1)
    ]
    if len(fwd_rets) < 2:
        return ResearchResolution(
            outcome="DATA_INVALID",
            observed_value=None,
            resolved_ts=evidence_now,
            available_ts=evidence_now,
            reason="too_few_forward_returns",
        )

    fwd_vol = float(np.std(fwd_rets)) * math.sqrt(252)
    if fwd_vol <= 0:
        return ResearchResolution(
            outcome="DATA_INVALID",
            observed_value=None,
            resolved_ts=evidence_now,
            available_ts=evidence_now,
            reason="zero_forward_vol",
        )

    vol_ratio = fwd_vol / trailing_vol
    won = vol_ratio > expansion_ratio
    resolved_ts = _daily_bar_available_ts(fwd[-1].get("ts"))

    return ResearchResolution(
        outcome="WIN" if won else "LOSS",
        observed_value=round(vol_ratio, 4),
        resolved_ts=resolved_ts or evidence_now,
        available_ts=resolved_ts or evidence_now,
        evidence={
            "trailing_vol": round(trailing_vol, 4),
            "forward_vol": round(fwd_vol, 4),
            "vol_ratio": round(vol_ratio, 4),
            "expansion_ratio": expansion_ratio,
        },
        reason=f"vol_expansion:{'WIN' if won else 'LOSS'}",
    )


# ── cross-sectional ranking resolver ────────────────────────────────────────

def resolve_cross_sectional_ranking(
    *,
    symbol: str,
    output: str,
    predicted_at: int,
    hold_bars: int = 5,
    symbol_returns: Optional[Dict[str, float]] = None,
    now: Optional[int] = None,
) -> Optional[ResearchResolution]:
    """Resolve a cross-sectional ranking prediction.

    TOP_QUARTILE wins if symbol's return is in the top 25% of peers.
    BOTTOM_QUARTILE wins if in the bottom 25%. Middle-half actuals are LOSS.
    Inadequate cohort coverage is DATA_INVALID.
    """
    import time as _time

    evidence_now = int(now or _time.time())
    output = (output or "").upper()

    if output not in ("TOP_QUARTILE", "BOTTOM_QUARTILE"):
        return None

    if not symbol_returns or len(symbol_returns) < 4:
        return ResearchResolution(
            outcome="DATA_INVALID",
            observed_value=None,
            resolved_ts=evidence_now,
            available_ts=evidence_now,
            reason="insufficient_cohort",
        )

    sym = symbol.upper()
    sym_ret = symbol_returns.get(sym)
    if sym_ret is None:
        return ResearchResolution(
            outcome="DATA_INVALID",
            observed_value=None,
            resolved_ts=evidence_now,
            available_ts=evidence_now,
            reason="symbol_not_in_cohort",
        )

    all_rets = sorted(v for v in symbol_returns.values() if v is not None)
    if len(all_rets) < 4:
        return ResearchResolution(
            outcome="DATA_INVALID",
            observed_value=None,
            resolved_ts=evidence_now,
            available_ts=evidence_now,
            reason="too_few_valid_returns",
        )

    n = len(all_rets)
    p25_idx = n // 4
    p75_idx = (3 * n) // 4
    top_quartile = all_rets[p75_idx] if p75_idx < n else all_rets[-1]
    bottom_quartile = all_rets[p25_idx] if p25_idx > 0 else all_rets[0]

    if output == "TOP_QUARTILE":
        won = sym_ret >= top_quartile
    else:
        won = sym_ret <= bottom_quartile

    return ResearchResolution(
        outcome="WIN" if won else "LOSS",
        observed_value=round(sym_ret, 6),
        resolved_ts=evidence_now,
        available_ts=evidence_now,
        evidence={
            "symbol_return": round(sym_ret, 6),
            "cohort_size": n,
            "top_quartile_threshold": round(top_quartile, 6),
            "bottom_quartile_threshold": round(bottom_quartile, 6),
        },
        reason=f"cross_sectional:{'WIN' if won else 'LOSS'}",
    )


# ── event reaction resolver ─────────────────────────────────────────────────

def resolve_event_reaction(
    *,
    symbol: str,
    output: str,
    event_ts: int,
    hold_bars: int = 3,
    symbol_bars: Optional[Sequence[Dict[str, Any]]] = None,
    spy_bars: Optional[Sequence[Dict[str, Any]]] = None,
    cost_band_pct: float = 0.005,  # 50bp cost/neutral band
    now: Optional[int] = None,
) -> Optional[ResearchResolution]:
    """Resolve an event reaction prediction.

    POSITIVE wins if (symbol_return - spy_return) > cost_band over hold_bars
    sessions. NEGATIVE wins if the spread is < -cost_band. Missing evidence
    is DATA_INVALID.
    """
    import time as _time
    from core.tp_sl_resolve import forward_bars_after_entry

    evidence_now = int(now or _time.time())
    output = (output or "").upper()

    if output not in ("POSITIVE", "NEGATIVE"):
        return None

    if not symbol_bars:
        return ResearchResolution(
            outcome="DATA_INVALID",
            observed_value=None,
            resolved_ts=evidence_now,
            available_ts=evidence_now,
            reason="no_symbol_bars",
        )

    # Find entry bar (first bar after event_ts)
    entry_idx = None
    for i, bar in enumerate(symbol_bars):
        from core.tp_sl_resolve import _daily_bar_available_ts
        bar_ts = _daily_bar_available_ts(bar.get("ts"))
        if bar_ts > event_ts:
            entry_idx = i
            break

    if entry_idx is None:
        return ResearchResolution(
            outcome="DATA_INVALID",
            observed_value=None,
            resolved_ts=evidence_now,
            available_ts=evidence_now,
            reason="no_entry_bar_after_event",
        )

    entry_close = float(symbol_bars[entry_idx].get("close", 0))
    if entry_close <= 0:
        return ResearchResolution(
            outcome="DATA_INVALID",
            observed_value=None,
            resolved_ts=evidence_now,
            available_ts=evidence_now,
            reason="invalid_entry_price",
        )

    # Forward symbol bars
    fwd_sym = forward_bars_after_entry(symbol_bars, event_ts, hold_bars)
    if len(fwd_sym) < hold_bars:
        return None  # not yet mature

    exit_close = float(fwd_sym[-1].get("close", 0))
    if exit_close <= 0:
        return ResearchResolution(
            outcome="DATA_INVALID",
            observed_value=None,
            resolved_ts=evidence_now,
            available_ts=evidence_now,
            reason="invalid_exit_price",
        )

    sym_return = (exit_close - entry_close) / entry_close

    # SPY return (if available)
    spy_return = 0.0
    if spy_bars:
        spy_entry_idx = None
        for i, bar in enumerate(spy_bars):
            from core.tp_sl_resolve import _daily_bar_available_ts
            bar_ts = _daily_bar_available_ts(bar.get("ts"))
            if bar_ts > event_ts:
                spy_entry_idx = i
                break
        if spy_entry_idx is not None:
            spy_entry = float(spy_bars[spy_entry_idx].get("close", 0))
            fwd_spy = forward_bars_after_entry(spy_bars, event_ts, hold_bars)
            if len(fwd_spy) >= hold_bars and spy_entry > 0:
                spy_exit = float(fwd_spy[-1].get("close", 0))
                if spy_exit > 0:
                    spy_return = (spy_exit - spy_entry) / spy_entry

    excess_return = sym_return - spy_return
    from core.tp_sl_resolve import _daily_bar_available_ts
    resolved_ts = _daily_bar_available_ts(fwd_sym[-1].get("ts"))

    if output == "POSITIVE":
        won = excess_return > cost_band_pct
    else:
        won = excess_return < -cost_band_pct

    return ResearchResolution(
        outcome="WIN" if won else "LOSS",
        observed_value=round(excess_return, 6),
        resolved_ts=resolved_ts or evidence_now,
        available_ts=resolved_ts or evidence_now,
        evidence={
            "sym_return": round(sym_return, 6),
            "spy_return": round(spy_return, 6),
            "excess_return": round(excess_return, 6),
            "cost_band_pct": cost_band_pct,
        },
        reason=f"event_reaction:{'WIN' if won else 'LOSS'}",
    )


# ── register v1 resolvers ───────────────────────────────────────────────────

register_resolver("tp_sl_bar_path/v1", "1.0.0", resolve_tp_sl_swing)
register_resolver("intraday_continuation/v1", "1.0.0", resolve_intraday_continuation)
register_resolver("volatility_expansion/v1", "1.0.0", resolve_volatility_expansion)
register_resolver("cross_sectional_ranking/v1", "1.0.0", resolve_cross_sectional_ranking)
register_resolver("event_reaction/v1", "1.0.0", resolve_event_reaction)
