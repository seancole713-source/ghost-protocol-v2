"""Target-before-stop outcomes from minute bars -- with ambiguity made explicit.

A bar is (ts_epoch_s, open, high, low, close, volume), ts = bar START. A daily
bar cannot say which level came first; a minute bar usually can, and when it
cannot (one minute touching both), this module says so and grades it LOSS.
Every conservative choice below is deliberate: an optimistic resolver is the
quickest way to manufacture an edge that does not exist.

Two of the three records live here (the third, the operator's real broker
fills, is edge/fills.py):

  resolve_market()     -- FORECAST OUTCOME. Did the market do what was
                          predicted? Once price touched the trigger, did it
                          reach the target before the stop and the deadline?
                          No order mechanics, no costs.
  resolve_execution()  -- SIMULATED EXECUTION. Could the stated order have
                          made money? Limit caps, gap fills and per-side costs.

A forecast can be right while the order never fills. Keeping the two apart is
the only way that difference stays visible.

Order model (a buy stop-limit, then an OCO bracket):
  * TRIGGER  when a bar's high reaches entry_trigger, before entry_expiry.
  * FILL     on the trigger bar: at the trigger if the bar opened below it,
             at the open if it gapped through (only if open <= limit).
             Otherwise the limit order rests; a later bar fills AT THE LIMIT
             when its low comes back to it, still before entry_expiry.
  * EXIT     after the fill, first of: stop (low <= stop; a gap below fills at
             the open), target (high >= target; a gap above fills at the
             open), or the time exit (the open of the first bar at/after it).
  * The FILL BAR itself: its internal path is unknown, so a stop touch there
             is a LOSS and a target touch there counts only if the stop was
             not also touched.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable, List, Optional, Sequence, Tuple

from edge.contracts import (
    COUNTED, LOSS, NO_FILL, TIME_EXIT, UNRESOLVED, WIN, Forecast,
)

Bar = Tuple[int, float, float, float, float, float]


@dataclass(frozen=True)
class Resolution:
    outcome: str
    entry_fill: Optional[float] = None
    entry_ts: Optional[int] = None
    exit_price: Optional[float] = None
    exit_ts: Optional[int] = None
    pnl_usd: Optional[float] = None
    pnl_pct: Optional[float] = None
    ambiguous: bool = False
    max_adverse_pct: Optional[float] = None     # worst drawdown while open
    max_favorable_pct: Optional[float] = None   # best run-up while open
    note: str = ""
    flags: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def counted(self) -> bool:
        return self.outcome in COUNTED


def _exit(f: Forecast, entry: float, entry_ts: int, price: float, ts: int, outcome: str,
          lo: float, hi: float, *, ambiguous: bool = False, note: str = "") -> Resolution:
    pnl = round(f.shares * (price - entry), 2)
    return Resolution(
        outcome=outcome, entry_fill=round(entry, 4), entry_ts=entry_ts,
        exit_price=round(price, 4), exit_ts=ts, pnl_usd=pnl,
        pnl_pct=round((price / entry - 1) * 100, 3), ambiguous=ambiguous,
        max_adverse_pct=round((lo / entry - 1) * 100, 3),
        max_favorable_pct=round((hi / entry - 1) * 100, 3), note=note,
    )


def resolve_market(f: Forecast, bars: Iterable[Bar], *, bar_seconds: int = 60) -> Resolution:
    """Forecast outcome: entry at the trigger the moment it trades, no limit, no costs."""
    loose = replace(f, entry_limit=float("inf"))
    r = _walk(loose, bars, bar_seconds=bar_seconds, market=True)
    return replace(r, flags=tuple(r.flags) + ("record:forecast",))


def resolve_execution(f: Forecast, bars: Iterable[Bar], *, bar_seconds: int = 60,
                      cost_bps_per_side: float = 10.0) -> Resolution:
    """Simulated execution: the order as written, plus a cost on each side.

    10 bps a side is a placeholder for spread + slippage on liquid names. It is
    stated on every record so it can be replaced by measured costs, never
    silently tuned.
    """
    r = _walk(f, bars, bar_seconds=bar_seconds, market=False)
    if r.entry_fill is None or r.exit_price is None:
        return replace(r, flags=tuple(r.flags) + ("record:simulated", f"cost_bps:{cost_bps_per_side:g}"))
    cost = cost_bps_per_side / 10_000
    entry = r.entry_fill * (1 + cost)
    exit_ = r.exit_price * (1 - cost)
    return replace(
        r, pnl_usd=round(f.shares * (exit_ - entry), 2),
        pnl_pct=round((exit_ / entry - 1) * 100, 3),
        flags=tuple(r.flags) + ("record:simulated", f"cost_bps:{cost_bps_per_side:g}"),
    )


def resolve(f: Forecast, bars: Iterable[Bar], *, bar_seconds: int = 60) -> Resolution:
    """Simulated execution without costs -- the order mechanics alone."""
    return _walk(f, bars, bar_seconds=bar_seconds, market=False)


def _walk(f: Forecast, bars: Iterable[Bar], *, bar_seconds: int, market: bool) -> Resolution:
    rows: List[Bar] = sorted(
        (b for b in bars if b[0] >= f.window_start - bar_seconds), key=lambda b: b[0],
    )
    rows = [b for b in rows if b[0] >= f.window_start]
    if not rows:
        return Resolution(UNRESOLVED, note="no bars in the window")

    triggered = False
    entry: Optional[float] = None
    entry_ts: Optional[int] = None
    lo = hi = None
    for i, (ts, o, h, l, c, _v) in enumerate(rows):
        if entry is None:
            if ts >= f.entry_expiry:
                return Resolution(NO_FILL, note=(
                    "triggered but never filled at the limit" if triggered
                    else "never reached the buy stop before expiry"))
            if not triggered and h >= f.entry_trigger:
                triggered = True
                if market:                        # the prediction, not the order
                    entry, entry_ts = f.entry_trigger, ts
                elif o >= f.entry_trigger:        # gapped through the trigger
                    if o <= f.entry_limit:
                        entry, entry_ts = o, ts
                    elif l <= f.entry_limit:      # came back into range this bar
                        entry, entry_ts = f.entry_limit, ts
                elif c <= f.entry_limit:
                    # Triggered inside the bar and it CLOSED inside the stop-limit band: it traded there.
                    entry, entry_ts = f.entry_trigger, ts
                # else: the bar ran through the trigger AND past the limit. A stop-limit then rests at
                # the limit; it fills only if price comes back to it before expiry (later bars). A
                # minute bar cannot show the order ever traded inside a narrow band -- 2026-09-23 GLND
                # (band $2.94-$2.96, 85x volume) was graded a fill here while the broker never filled.
            elif triggered and l <= f.entry_limit:
                entry, entry_ts = f.entry_limit, ts
            if entry is None:
                continue
            lo, hi = l, h
            # The fill bar: path inside the minute is unknown. Conservative.
            if l <= f.stop:
                return _exit(f, entry, entry_ts, f.stop, ts, LOSS, lo, hi,
                             ambiguous=True, note="stop touched in the fill bar")
            if h >= f.target:
                return _exit(f, entry, entry_ts, f.target, ts, WIN, lo, hi,
                             note="target touched in the fill bar")
            continue

        if ts >= f.time_exit:
            return _exit(f, entry, entry_ts, o, ts, TIME_EXIT, lo, hi, note="time exit")
        lo, hi = min(lo, l), max(hi, h)
        hit_stop, hit_target = l <= f.stop, h >= f.target
        if hit_stop and hit_target:
            return _exit(f, entry, entry_ts, min(o, f.stop), ts, LOSS, lo, hi, ambiguous=True,
                         note="one bar touched both levels; graded LOSS")
        if hit_stop:
            return _exit(f, entry, entry_ts, min(o, f.stop), ts, LOSS, lo, hi)
        if hit_target:
            return _exit(f, entry, entry_ts, max(o, f.target), ts, WIN, lo, hi)

    if entry is None:
        last_ts = rows[-1][0]
        if last_ts + bar_seconds >= f.entry_expiry:
            return Resolution(NO_FILL, note="never filled before expiry")
        return Resolution(UNRESOLVED, note="bars end before the entry expired")
    return Resolution(UNRESOLVED, entry_fill=round(entry, 4), entry_ts=entry_ts,
                      note="bars end before an exit; resolve again when data arrives")
