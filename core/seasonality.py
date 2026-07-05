"""core/seasonality.py — calendar-window seasonal stats (PR #133).

Answers one question per (symbol, calendar date): over the past ~4 years, how
did this symbol perform in the N trading days starting at this point of the
calendar, relative to its own normal N-day return?

Born from the operator's post-July-4th study (2026-07-05): 8 of 42 watchlist
symbols were positive in that window all four years (2022-2025) vs ~2.6
expected by chance — a real but tide-like effect concentrated in speculative
small caps.

HONESTY CONTRACT: n is at most 4-5 yearly windows. That is thin evidence by
construction, so consumers (the seasonal shadow brain) must cap confidence low
and treat this as a lean, never a signal. Do not promote this into a trained
feature without a proper multi-decade study — at n=4 a "seasonal edge" is one
bad year away from being noise.
"""
from __future__ import annotations

import datetime
import logging
import time
from typing import Any, Dict, Optional

from core.quiet import note_suppressed

LOGGER = logging.getLogger("ghost.seasonality")

# In-process cache: seasonal stats change once per calendar day at most.
_CACHE: Dict[tuple, tuple] = {}
_CACHE_TTL_S = 24 * 3600
_CACHE_MAX = 2000

MIN_YEARS = 3          # fewer yearly windows than this -> unavailable
MIN_EXCESS_PCT = 2.5   # |excess| below this -> no lean
MIN_BARS = 300         # need enough history for a stable baseline


def _fetch_daily_5y(symbol: str):
    from core.signal_engine import _fetch_ohlcv
    return _fetch_ohlcv(symbol, "stock", period="5y") or []


def seasonal_window_stats(symbol: str, anchor: Optional[datetime.date] = None,
                          hold_days: int = 5) -> Dict[str, Any]:
    """Seasonal stats for the `hold_days`-bar window starting at `anchor`.

    Returns {"available": False, "reason": ...} on any shortfall — never raises.
    """
    anchor = anchor or datetime.date.today()
    key = (symbol.upper(), anchor.month, anchor.day, int(hold_days))
    now = time.time()
    hit = _CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
    try:
        stats = _compute(symbol, anchor, int(hold_days))
    except Exception as exc:
        stats = {"available": False, "reason": f"compute failed: {str(exc)[:80]}"}
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = (now + _CACHE_TTL_S, stats)
    return stats


def _compute(symbol: str, anchor: datetime.date, hold_days: int) -> Dict[str, Any]:
    rows = _fetch_daily_5y(symbol)
    ser = []
    for r in rows:
        d = str(r.get("date") or r.get("ts") or "")[:10]
        try:
            ser.append((datetime.date.fromisoformat(d), float(r["close"])))
        except Exception:
            note_suppressed()
    ser.sort()
    if len(ser) < MIN_BARS:
        return {"available": False, "reason": f"only {len(ser)} bars"}
    dates = [d for d, _ in ser]
    closes = [c for _, c in ser]

    fwd = [(closes[i + hold_days] / closes[i] - 1) * 100
           for i in range(len(closes) - hold_days) if closes[i] > 0]
    if not fwd:
        return {"available": False, "reason": "no baseline windows"}
    baseline = sum(fwd) / len(fwd)

    per_year: Dict[int, float] = {}
    for yr in range(anchor.year - 4, anchor.year):
        try:
            a = datetime.date(yr, anchor.month, anchor.day)
        except ValueError:  # Feb 29 in a non-leap year
            a = datetime.date(yr, anchor.month, 28)
        idx = next((i for i, d in enumerate(dates) if d > a), None)
        if idx is None or idx + hold_days >= len(closes):
            continue
        if (dates[idx] - a).days > 6:  # data gap — not a real window
            continue
        per_year[yr] = round((closes[idx + hold_days] / closes[idx] - 1) * 100, 2)

    n = len(per_year)
    if n < MIN_YEARS:
        return {"available": False, "reason": f"only {n} yearly windows"}

    vals = list(per_year.values())
    avg = sum(vals) / n
    excess = avg - baseline
    pos = sum(1 for v in vals if v > 0)
    neg = sum(1 for v in vals if v < 0)
    consistency = max(pos, neg) / n
    if excess >= MIN_EXCESS_PCT and pos > neg:
        lean = "UP"
    elif excess <= -MIN_EXCESS_PCT and neg > pos:
        lean = "DOWN"
    else:
        lean = "NONE"
    return {
        "available": True,
        "symbol": symbol.upper(),
        "anchor": anchor.isoformat(),
        "hold_days": hold_days,
        "n_years": n,
        "per_year": per_year,
        "avg_window_pct": round(avg, 2),
        "baseline_pct": round(baseline, 2),
        "excess_pct": round(excess, 2),
        "consistency": round(consistency, 2),
        "lean": lean,
    }
