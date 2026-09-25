"""The third record: what the operator's broker actually did.

"Sell alert sent" and "position closed" are different events, and a system that
conflates them will one day tell someone from a boat that they are flat while a
position sits open and unprotected. This module rebuilds position state from
broker order events -- never from prices, never from alerts.

It is broker-agnostic: an adapter (Alpaca paper first) translates its own order
updates into OrderEvent. Two facts shape it:
  * a triggered stop becomes a MARKET order, so its fill can be far from the
    stop price (SEC investor guidance on stop orders) -- the realised price is
    recorded, never the stop level;
  * broker features differ by session: Alpaca documents that bracket orders do
    not support extended hours. The adapter must check, not assume.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Tuple

from edge.contracts import LOSS, NO_FILL, TIME_EXIT, UNRESOLVED, WIN, Forecast
from edge.resolver import Resolution

# Order roles in one bracket.
ENTRY, TARGET, STOP, TIME_EXIT_ROLE, MANUAL = "entry", "target", "stop", "time_exit", "manual"
# Order statuses, normalised across brokers.
SUBMITTED, ACCEPTED, REJECTED = "submitted", "accepted", "rejected"
PARTIAL, FILLED, CANCELED, EXPIRED, TRIGGERED = "partially_filled", "filled", "canceled", "expired", "triggered"


def filled_after_window(fill_ts: Optional[int], entry_expiry: int) -> bool:
    """Did the BROKER's entry fill land at or after the forecast's entry expiry?

    Judged on the broker's own fill time, never on what a simulation or the market
    record said: the resolver takes no entry on a bar starting at/after entry_expiry,
    so neither may the actual record. A fill with no known time is not called late.
    """
    return fill_ts is not None and int(fill_ts) >= int(entry_expiry)


LATE_FLAG = "entry_after_window"
LATE_WARNING = "ENTRY FILLED AFTER THE ENTRY WINDOW: outside the rule, not counted"


@dataclass(frozen=True)
class OrderEvent:
    ts: int
    order_id: str
    role: str            # entry | target | stop | time_exit | manual
    status: str
    fill_qty: int = 0    # quantity filled BY THIS EVENT (incremental)
    fill_price: Optional[float] = None
    note: str = ""


@dataclass
class Position:
    state: str = "NOT_PLACED"
    qty_bought: int = 0
    qty_sold: int = 0
    cost: float = 0.0
    proceeds: float = 0.0
    protected: bool = False
    last_exit_role: Optional[str] = None
    entry_filled_at: Optional[int] = None     # the broker's FIRST entry fill time
    messages: List[Tuple[int, str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def qty_open(self) -> int:
        return self.qty_bought - self.qty_sold

    @property
    def avg_entry(self) -> Optional[float]:
        return self.cost / self.qty_bought if self.qty_bought else None

    @property
    def avg_exit(self) -> Optional[float]:
        return self.proceeds / self.qty_sold if self.qty_sold else None


def reconcile(f: Forecast, events: Iterable[OrderEvent], *, now: Optional[int] = None) -> Position:
    """Replay broker events into the position they describe."""
    p = Position()
    protective_live = set()
    announced = False
    for e in sorted(events, key=lambda x: (x.ts, x.order_id)):
        say = lambda msg: p.messages.append((e.ts, msg))
        if e.role == ENTRY:
            if e.status in (SUBMITTED, ACCEPTED) and p.state == "NOT_PLACED":
                p.state = "ENTRY_WORKING"; say("Entry order working.")
            elif e.status == REJECTED:
                p.state = "ENTRY_REJECTED"; say(f"Entry rejected by the broker. {e.note}".strip())
            elif e.status in (EXPIRED, CANCELED) and p.qty_bought == 0:
                p.state = "ENTRY_EXPIRED"
                say("Entry expired -- price never came into the permitted range."
                    if e.status == EXPIRED else "Entry canceled before it filled.")
        if e.fill_qty and e.fill_price is not None:
            if e.role == ENTRY:
                p.qty_bought += e.fill_qty; p.cost += e.fill_qty * e.fill_price
                if p.entry_filled_at is None:
                    p.entry_filled_at = int(e.ts)
                say(f"Bought {e.fill_qty} at {e.fill_price:.2f}.")
                if filled_after_window(e.ts, f.entry_expiry) and LATE_WARNING not in p.warnings:
                    p.warnings.append(LATE_WARNING)
                    say("Entry filled AFTER the entry window: outside the rule, not counted.")
            else:
                p.qty_sold += e.fill_qty; p.proceeds += e.fill_qty * e.fill_price
                p.last_exit_role = e.role
                say(f"Sold {e.fill_qty} at {e.fill_price:.2f} ({e.role}).")
        if e.role in (TARGET, STOP):
            if e.status == ACCEPTED:
                protective_live.add(e.role)
            elif e.status in (REJECTED, CANCELED, EXPIRED, FILLED):
                protective_live.discard(e.role)
                if e.status == REJECTED:
                    say(f"Protective {e.role} order REJECTED. {e.note}".strip())
            if e.status == TRIGGERED:
                say("Exit triggered. Order submitted; fill pending.")
                p.state = "EXIT_PENDING"
        if e.role in (TIME_EXIT_ROLE, MANUAL) and e.status in (SUBMITTED, ACCEPTED):
            say("Exit order submitted; fill pending."); p.state = "EXIT_PENDING"

        if p.qty_bought:
            if p.qty_open == 0:
                p.state = "CLOSED"
            elif p.state not in ("EXIT_PENDING",) or p.qty_sold:
                p.state = "PARTIAL_EXIT" if p.qty_sold else "OPEN"
        p.protected = STOP in protective_live
        if p.qty_open > 0 and p.protected and not announced:
            announced = True
            say("Position filled. Protective order confirmed.")

    if p.qty_open > 0:
        if not p.protected:
            p.warnings.append("OPEN WITHOUT A CONFIRMED PROTECTIVE STOP")
        if p.qty_bought and p.qty_bought < f.shares:
            p.warnings.append(f"entry partially filled ({p.qty_bought}/{f.shares}); bracket size must match")
        if now is not None and now >= f.time_exit:
            p.warnings.append("still open past the time exit")
    return p


def to_resolution(f: Forecast, p: Position) -> Resolution:
    """The "actual" record, from real fills. Only a CLOSED position is final."""
    if p.qty_bought == 0:
        if p.state in ("ENTRY_EXPIRED", "ENTRY_REJECTED"):
            return Resolution(NO_FILL, note=p.state.lower(), flags=("record:actual",))
        return Resolution(UNRESOLVED, note="no fills reported", flags=("record:actual",))
    late = filled_after_window(p.entry_filled_at, f.entry_expiry)
    flags = ("record:actual",) + ((LATE_FLAG,) if late else ())
    if p.state != "CLOSED":
        return Resolution(UNRESOLVED, entry_fill=round(p.avg_entry, 4), entry_ts=p.entry_filled_at,
                          note=f"position {p.state.lower()} ({p.qty_open} open)", flags=flags)
    role = p.last_exit_role
    exit_px = p.avg_exit
    if role == TARGET:
        outcome = WIN
    elif role == STOP:
        outcome = LOSS
    elif role == TIME_EXIT_ROLE:
        outcome = TIME_EXIT
    else:   # manual: judge by where it actually closed, not by intent
        outcome = WIN if exit_px >= f.target else LOSS if exit_px <= f.stop else TIME_EXIT
    pnl = round(p.proceeds - p.cost, 2)
    # A late entry keeps the broker's true outcome on the record (it is what happened), but the
    # flag and entry_ts make the ledger show it as OUTSIDE_RULE and never count it.
    return Resolution(
        outcome=outcome, entry_fill=round(p.avg_entry, 4), entry_ts=p.entry_filled_at,
        exit_price=round(exit_px, 4),
        pnl_usd=pnl, pnl_pct=round((exit_px / p.avg_entry - 1) * 100, 3),
        note=f"closed via {role}" + ("; entry filled after the entry window (outside the rule)" if late else ""),
        flags=flags,
    )
