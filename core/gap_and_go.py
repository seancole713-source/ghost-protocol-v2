"""Gap-and-Go v1 -- the frozen rule's arithmetic, in one place.

See docs/gap_and_go_v1.md. Every number here is FROZEN until 20 filled trades
are logged; a change is v2 with a new ledger, never an edit to this file.

Why code and not a prompt: the morning card's levels were going to be computed
by an LLM doing arithmetic at 9am, every day, for real money. A rounding slip
on a stop price is a real loss. These are pure functions, pinned by tests, and
the morning routine calls them:

    python3 -m core.gap_and_go levels 9.40
    python3 -m core.gap_and_go grade --entry-stop 9.49 --shares 105 \\
        --open 9.30 --high 10.10 --low 9.20
"""
from __future__ import annotations

import argparse
import json
import math
from typing import Any, Dict, Optional

RULE_VERSION = "gap_and_go_v1"
SIZE_USD = 1000.0
ENTRY_TRIGGER = 1.01   # buy stop at ref x 1.01
ENTRY_LIMIT = 1.02     # ... no fill above ref x 1.02
TARGET = 1.05          # +5% from the entry stop
STOP = 0.97            # -3% from the entry stop
MIN_MOVE_PCT, MAX_MOVE_PCT = 5.0, 40.0
MIN_PRICE, MAX_PRICE = 2.0, 500.0
MIN_AVG_SHARES = 500_000
MIN_AVG_DOLLARS = 5_000_000.0
MAX_TRADES_PER_DAY = 2
PAUSE_LINE_USD = -300.0
BREAK_EVEN_WIN_RATE = (1 - STOP) / ((TARGET - 1) + (1 - STOP))   # 0.375


def _cents(x: float) -> float:
    return round(x + 1e-9, 2)


def levels(ref_price: float, *, size_usd: float = SIZE_USD) -> Dict[str, Any]:
    """The bracket for one name, from its reference premarket price."""
    ref = float(ref_price)
    if not math.isfinite(ref) or ref <= 0:
        raise ValueError("reference price must be a positive number")
    entry_stop = _cents(ref * ENTRY_TRIGGER)
    entry_limit = _cents(ref * ENTRY_LIMIT)
    target = _cents(entry_stop * TARGET)
    stop = _cents(entry_stop * STOP)
    shares = int(size_usd // entry_stop)
    return {
        "rule": RULE_VERSION, "ref_price": _cents(ref),
        "entry_stop": entry_stop, "entry_limit": entry_limit,
        "target": target, "stop": stop, "shares": shares,
        "max_loss_usd": round(shares * (entry_stop - stop), 2),
        "max_gain_usd": round(shares * (target - entry_stop), 2),
    }


def eligibility_failures(
    *, move_pct: float, price: float, avg_shares: float, avg_dollars: float,
    has_dated_catalyst: bool, dilutive_or_mechanical: bool,
) -> list:
    """Every mechanical rule that fails. Empty list == eligible.

    E4 (catalyst) and E5 (offering / ex-div / split) are judged from research,
    so they arrive as booleans; everything numeric is checked here.
    """
    fails = []
    if not (MIN_MOVE_PCT <= move_pct <= MAX_MOVE_PCT):
        fails.append(f"E1 move {move_pct:+.1f}% outside +{MIN_MOVE_PCT:g}% to +{MAX_MOVE_PCT:g}%")
    if not (MIN_PRICE <= price <= MAX_PRICE):
        fails.append(f"E2 price ${price:.2f} outside ${MIN_PRICE:g}-${MAX_PRICE:g}")
    if avg_shares < MIN_AVG_SHARES:
        fails.append(f"E3 avg volume {avg_shares:,.0f} < {MIN_AVG_SHARES:,}")
    if avg_dollars < MIN_AVG_DOLLARS:
        fails.append(f"E3 avg dollar volume ${avg_dollars:,.0f} < ${MIN_AVG_DOLLARS:,.0f}")
    if not has_dated_catalyst:
        fails.append("E4 no dated company catalyst in the last 24h")
    if dilutive_or_mechanical:
        fails.append("E5 offering/dilution or ex-dividend/split day")
    return fails


def grade(
    *, entry_stop: float, shares: int, open_: float, high: float, low: float,
    close_at_exit: Optional[float] = None, entry_limit: Optional[float] = None,
) -> Dict[str, Any]:
    """Grade one call from the day's bar -- the CONSERVATIVE estimate.

    A daily bar cannot say whether the target or the stop came first, so a bar
    that touches both is graded LOSS. It also cannot say whether the entry
    triggered before the 10:30 ET expiry; the operator's real fills override
    this grade whenever they are reported (graded_from = "operator_fill").
    """
    target = _cents(entry_stop * TARGET)
    stop = _cents(entry_stop * STOP)
    limit = entry_limit if entry_limit is not None else _cents(entry_stop / ENTRY_TRIGGER * ENTRY_LIMIT)
    out: Dict[str, Any] = {"target": target, "stop": stop, "graded_from": "daily_bar_estimate"}
    if open_ > limit:
        return {**out, "outcome": "NO_FILL", "note": "gapped above the limit at the open"}
    if high < entry_stop:
        return {**out, "outcome": "NO_FILL", "note": "never traded up to the buy stop"}
    entry = max(entry_stop, open_)          # a gap through the stop fills at the open
    if low <= stop and high >= target:
        exit_, outcome, note = stop, "LOSS", "bar touched both; graded LOSS (conservative)"
    elif high >= target:
        exit_, outcome, note = target, "WIN", ""
    elif low <= stop:
        exit_, outcome, note = stop, "LOSS", ""
    else:
        if close_at_exit is None:
            return {**out, "outcome": "UNGRADED", "note": "need the 3:30pm ET price"}
        exit_, outcome, note = float(close_at_exit), "TIME_EXIT", "sold at 3:30pm ET"
    return {**out, "outcome": outcome, "entry_fill": round(entry, 4), "exit_price": round(exit_, 4),
            "pnl_usd": round(shares * (exit_ - entry), 2),
            "pnl_pct": round((exit_ / entry - 1) * 100, 2), "note": note}


def grade_from_fills(*, entry_fill: float, exit_price: float, shares: int, entry_stop: float) -> Dict[str, Any]:
    """Grade from the operator's real broker fills -- the source of truth."""
    target, stop = _cents(entry_stop * TARGET), _cents(entry_stop * STOP)
    if exit_price >= target - 0.005:
        outcome = "WIN"
    elif exit_price <= stop + 0.005:
        outcome = "LOSS"
    else:
        outcome = "TIME_EXIT"
    return {"outcome": outcome, "entry_fill": entry_fill, "exit_price": exit_price,
            "pnl_usd": round(shares * (exit_price - entry_fill), 2),
            "pnl_pct": round((exit_price / entry_fill - 1) * 100, 2),
            "graded_from": "operator_fill"}


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m core.gap_and_go")
    sub = p.add_subparsers(dest="cmd", required=True)
    lv = sub.add_parser("levels"); lv.add_argument("ref", type=float)
    gr = sub.add_parser("grade")
    for name in ("entry-stop", "open", "high", "low"):
        gr.add_argument("--" + name, type=float, required=True)
    gr.add_argument("--shares", type=int, required=True)
    gr.add_argument("--close-at-exit", type=float)
    a = p.parse_args(argv)
    if a.cmd == "levels":
        print(json.dumps(levels(a.ref), indent=2))
    else:
        print(json.dumps(grade(entry_stop=a.entry_stop, shares=a.shares, open_=a.open,
                               high=a.high, low=a.low, close_at_exit=a.close_at_exit), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
