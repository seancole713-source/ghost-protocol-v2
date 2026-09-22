"""Named, measurable setup components. Each answers PASS, FAIL -- or UNKNOWN.

UNKNOWN is a first-class answer. A missing borrow feed is not "no short
interest", and a missing volume baseline is not "normal volume". A detector
that silently turns absent data into a number is how a system manufactures
confidence it does not have.

Bars are (ts_s, open, high, low, close, volume), ts = bar start, minute bars.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"
Bar = Tuple[int, float, float, float, float, float]


@dataclass(frozen=True)
class Signal:
    name: str
    state: str
    value: Optional[float] = None
    threshold: Optional[str] = None
    evidence: Dict[str, Any] = field(default_factory=dict)


def _unknown(name: str, why: str) -> Signal:
    return Signal(name, UNKNOWN, evidence={"missing": why})


def gap(prev_close: Optional[float], price: Optional[float], *, lo: float = 5.0, hi: float = 40.0) -> Signal:
    if not prev_close or not price or prev_close <= 0:
        return _unknown("gap", "prev_close or current price")
    pct = (price / prev_close - 1) * 100
    return Signal("gap", PASS if lo <= pct <= hi else FAIL, round(pct, 3), f"{lo:g}..{hi:g}%")


def rvol_time_of_day(cum_volume_now: Optional[float], same_minute_history: Sequence[float], *,
                     min_ratio: float = 2.0, min_days: int = 10) -> Signal:
    """Volume so far vs the MEDIAN cumulative volume at this same minute on prior days.

    Daily-average RVOL flatters the open (every stock trades most at 9:31);
    time-of-day RVOL compares like with like. Fewer than `min_days` of history
    is UNKNOWN, not a guess.
    """
    hist = [v for v in same_minute_history if v and v > 0]
    if cum_volume_now is None:
        return _unknown("rvol_tod", "current cumulative volume")
    if len(hist) < min_days:
        return _unknown("rvol_tod", f"only {len(hist)} days of same-minute history (< {min_days})")
    base = statistics.median(hist)
    ratio = cum_volume_now / base
    return Signal("rvol_tod", PASS if ratio >= min_ratio else FAIL, round(ratio, 2), f">= {min_ratio:g}x",
                  {"baseline_median": base, "days": len(hist)})


def opening_range(bars: Sequence[Bar], rth_open_ts: int, minutes: int = 5) -> Optional[Tuple[float, float]]:
    first = [b for b in bars if rth_open_ts <= b[0] < rth_open_ts + minutes * 60]
    if len(first) < max(1, minutes // 2):
        return None
    return max(b[2] for b in first), min(b[3] for b in first)


def orb_break(bars: Sequence[Bar], rth_open_ts: int, *, minutes: int = 5, now_ts: Optional[int] = None) -> Signal:
    """A 1-minute CLOSE above the opening-range high (a wick is not a break)."""
    rng = opening_range(bars, rth_open_ts, minutes)
    if rng is None:
        return _unknown("orb_break", "opening-range bars")
    hi, lo = rng
    after = [b for b in bars if b[0] >= rth_open_ts + minutes * 60 and (now_ts is None or b[0] < now_ts)]
    for b in after:
        if b[4] > hi:
            return Signal("orb_break", PASS, hi, f"close > OR high {hi:g}", {"break_ts": b[0], "or_low": lo})
    return Signal("orb_break", FAIL, hi, f"close > OR high {hi:g}", {"or_low": lo})


def vwap(bars: Sequence[Bar]) -> Optional[float]:
    pv = sum(((b[2] + b[3] + b[4]) / 3) * b[5] for b in bars)
    v = sum(b[5] for b in bars)
    return pv / v if v > 0 else None


def vwap_hold(bars: Sequence[Bar], *, last_n: int = 5) -> Signal:
    """The last N minute closes all above the session VWAP."""
    if len(bars) < last_n + 1:
        return _unknown("vwap_hold", f"fewer than {last_n + 1} bars")
    vw = vwap(bars)
    if vw is None:
        return _unknown("vwap_hold", "zero volume")
    held = all(b[4] > vw for b in bars[-last_n:])
    return Signal("vwap_hold", PASS if held else FAIL, round(vw, 4), f"last {last_n} closes > VWAP")


def acceleration(bars: Sequence[Bar], *, k: int = 5, min_ratio: float = 1.5) -> Signal:
    """Is the move speeding up? Return over the last k bars vs the k before."""
    if len(bars) < 2 * k + 1:
        return _unknown("acceleration", f"fewer than {2 * k + 1} bars")
    a = bars[-2 * k - 1][4]; m = bars[-k - 1][4]; z = bars[-1][4]
    if a <= 0 or m <= 0:
        return _unknown("acceleration", "non-positive price")
    prev, last = m / a - 1, z / m - 1
    if last <= 0:
        return Signal("acceleration", FAIL, round(last * 100, 3), "rising and faster")
    ratio = (last / prev) if prev > 0 else float("inf")
    return Signal("acceleration", PASS if ratio >= min_ratio else FAIL,
                  round(min(ratio, 99.0), 2), f">= {min_ratio:g}x prior window")


def crowded_short(*, borrow_fee_pct: Optional[float], available_shares: Optional[float],
                  short_interest_pct_float: Optional[float], short_interest_age_days: Optional[float],
                  days_to_cover: Optional[float] = None,
                  min_fee: float = 20.0, min_si: float = 20.0, min_dtc: float = 5.0,
                  max_si_age_days: float = 20.0) -> Signal:
    """Crowded short = expensive to borrow AND a large, reasonably recent short interest.

    "Large" is SI >= 20% of float when float is known; otherwise days-to-cover
    (short shares / average daily volume) >= 5, a standard crowding measure. The
    basis used is recorded. Short interest is reported twice a month; a report
    older than 20 days is UNKNOWN, not evidence. Daily short VOLUME is a
    different measurement and is not accepted here.
    """
    if borrow_fee_pct is None:
        return _unknown("crowded_short", "borrow fee")
    if short_interest_age_days is None or (short_interest_pct_float is None and days_to_cover is None):
        return _unknown("crowded_short", "short interest report")
    if short_interest_age_days > max_si_age_days:
        return _unknown("crowded_short", f"short interest is {short_interest_age_days:.0f} days old")
    if short_interest_pct_float is not None:
        large, basis, value = short_interest_pct_float >= min_si, f"SI >= {min_si:g}% float", short_interest_pct_float
    else:
        large, basis, value = days_to_cover >= min_dtc, f"days to cover >= {min_dtc:g}", days_to_cover
    crowded = borrow_fee_pct >= min_fee and large
    return Signal("crowded_short", PASS if crowded else FAIL, round(value, 2),
                  f"fee >= {min_fee:g}% and {basis}",
                  {"borrow_fee_pct": borrow_fee_pct, "available_shares": available_shares,
                   "si_age_days": short_interest_age_days, "basis": basis})


def liquidity(*, price: Optional[float], avg_shares: Optional[float], spread_bps: Optional[float] = None,
              min_price: float = 2.0, max_price: float = 500.0, min_avg_shares: float = 500_000,
              min_avg_dollars: float = 5_000_000, max_spread_bps: float = 50.0) -> Signal:
    if price is None or avg_shares is None:
        return _unknown("liquidity", "price or average volume")
    reasons = []
    if not (min_price <= price <= max_price):
        reasons.append(f"price {price:g} outside {min_price:g}-{max_price:g}")
    if avg_shares < min_avg_shares:
        reasons.append(f"avg volume {avg_shares:,.0f} < {min_avg_shares:,.0f}")
    if price * avg_shares < min_avg_dollars:
        reasons.append(f"avg dollar volume {price * avg_shares:,.0f} < {min_avg_dollars:,.0f}")
    if spread_bps is not None and spread_bps > max_spread_bps:
        reasons.append(f"spread {spread_bps:.0f}bps > {max_spread_bps:g}")
    return Signal("liquidity", FAIL if reasons else PASS, round(price * avg_shares),
                  "tradeable size", {"reasons": reasons})


def high_short_interest(*, days_to_cover: Optional[float], short_interest_age_days: Optional[float],
                        min_dtc: float = 5.0, max_si_age_days: float = 20.0) -> Signal:
    """Heavily shorted on the latest report: days-to-cover >= 5, report <= 20 days old.

    Deliberately NOT called "crowded": crowding also needs an expensive borrow,
    and no free borrow source reaches production (IBKR FTP and iBorrowDesk both
    refused from Railway, 2026-09-22). This says only what the data shows.
    """
    if days_to_cover is None or short_interest_age_days is None:
        return _unknown("short_interest", "short interest report")
    if short_interest_age_days > max_si_age_days:
        return _unknown("short_interest", f"short interest is {short_interest_age_days:.0f} days old")
    return Signal("short_interest", PASS if days_to_cover >= min_dtc else FAIL, round(days_to_cover, 2),
                  f"days to cover >= {min_dtc:g}", {"si_age_days": short_interest_age_days})
