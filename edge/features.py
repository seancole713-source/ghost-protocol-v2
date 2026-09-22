"""Model features -- ONE builder, used identically in the backtest and live.

Train/serve skew is how Ghost's models failed: columns that were zero in
training and populated in production. This module is the single place a
feature is computed, from inputs that are identical in both settings:

  * consolidated (SIP) premarket minute bars that CLOSED by 08:55 ET -- the
    latest the free data plan serves both historically and live (SIP data must
    be at least 15 minutes old, and the card runs from 09:05)
  * the prior session's close and PRIOR-session 20-day volume averages
  * catalyst events first seen before the card

The IEX price at card time is used for order levels only, never as a feature.
A missing input produces None, and a row with any None feature is not scored.
"""
from __future__ import annotations

import math
from datetime import date, datetime, time as dtime
from typing import Any, Dict, List, Optional, Sequence

from edge import catalysts as C
from edge.contracts import ET

FEATURES = ("gap_855", "log_price", "log_dollar_volume", "pre_volume_ratio", "pre_range_pos",
            "pre_trend", "catalyst_company", "catalyst_policy", "dilutive", "any_news", "dow")
CUTOFF = (8, 55)


def _at(day: date, hh: int, mm: int) -> int:
    return int(datetime.combine(day, dtime(hh, mm), tzinfo=ET).timestamp())


def premarket_until_cutoff(bars: Sequence[tuple], day: date) -> List[tuple]:
    start, cut = _at(day, 4, 0), _at(day, *CUTOFF)
    return [b for b in bars if start <= b[0] and b[0] + 60 <= cut]


def build(*, day: date, prev_close: Optional[float], avg_shares: Optional[float],
          avg_dollars: Optional[float], sip_bars: Sequence[tuple],
          events: Optional[List[C.CatalystEvent]]) -> Dict[str, Optional[float]]:
    pre = premarket_until_cutoff(sip_bars, day)
    f: Dict[str, Optional[float]] = {k: None for k in FEATURES}
    f["dow"] = float(day.weekday())
    if avg_dollars and avg_dollars > 0:
        f["log_dollar_volume"] = math.log10(avg_dollars)
    if events is not None:
        f["catalyst_company"] = 1.0 if any(e.company_specific for e in events) else 0.0
        f["catalyst_policy"] = 1.0 if any(e.kind == C.POLICY for e in events) else 0.0
        f["dilutive"] = 1.0 if any(e.dilutive or e.kind == C.REVERSE_SPLIT for e in events) else 0.0
        f["any_news"] = 1.0 if events else 0.0
    if not pre or not prev_close or prev_close <= 0:
        return f
    last = pre[-1][4]
    f["gap_855"] = (last / prev_close - 1) * 100
    f["log_price"] = math.log10(max(last, 0.01))
    hi, lo = max(b[2] for b in pre), min(b[3] for b in pre)
    f["pre_range_pos"] = (last - lo) / (hi - lo) if hi > lo else 0.5
    first_open = pre[0][1]
    f["pre_trend"] = (last / first_open - 1) * 100 if first_open > 0 else None
    if avg_shares and avg_shares > 0:
        f["pre_volume_ratio"] = sum(b[5] for b in pre) / avg_shares
    return f


def complete(f: Dict[str, Optional[float]]) -> bool:
    return all(f.get(k) is not None for k in FEATURES)


def vector(f: Dict[str, Optional[float]]) -> List[float]:
    return [float(f[k]) for k in FEATURES]
