"""The daily miss review -- disciplined, so it does not teach the system to chase.

"List everything that rose 5%" is not a review; it rewards whatever would have
caught yesterday's winners and ignores the losers that change would admit. So:

1. EXECUTABILITY FIRST. A move counts as an opportunity only if, after a signal
   could realistically have arrived, a +5% target was still reachable before a
   -3% stop under the same rules. A stock that gapped +20% and offered nothing
   after the open was never an intraday opportunity -- it is labelled GAP_ONLY,
   not a miss.
2. ONE LABEL PER MISS, in a fixed order:
     UNIVERSE_COVERAGE        not in the supported universe
     DATA_INTERRUPTION        in universe, but its data source was down/stale
     CATALYST_MISSED          a catalyst existed in the store but was not linked
     DETECTION_FAILURE        never detected by the radar
     STRATEGY_REJECTION       detected, rejected by a strategy rule
     RISK_LIQUIDITY_EXCLUSION detected, excluded by liquidity or risk
     ALERT_EXECUTION_FAILURE  forecast issued, but the alert or the fill failed
   plus CAUGHT (forecast issued in time).
3. CORRECT REJECTIONS COUNT TOO. Every rejected name that did NOT offer an
   executable move is a rejection the rules got right. Recall is reported
   beside it, never alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

Bar = Tuple[int, float, float, float, float, float]

LABELS = ("UNIVERSE_COVERAGE", "DATA_INTERRUPTION", "CATALYST_MISSED", "DETECTION_FAILURE",
          "STRATEGY_REJECTION", "RISK_LIQUIDITY_EXCLUSION", "ALERT_EXECUTION_FAILURE")
_LIQUIDITY_WORDS = ("liquidity", "avg volume", "dollar volume", "spread", "price ", "risk", "theme")


@dataclass(frozen=True)
class DayMove:
    symbol: str
    prev_close: float
    open: float
    high: float
    low: float
    close: float


@dataclass
class RadarRecord:
    first_seen_ts: Optional[int] = None
    first_seen_price: Optional[float] = None
    rejected_reason: Optional[str] = None
    forecast_issued: bool = False
    alert_delivered_ts: Optional[int] = None
    filled: Optional[bool] = None


def executable_from(entry: float, bars: Sequence[Bar], after_ts: int, *,
                    target: float = 1.05, stop: float = 0.97) -> Optional[bool]:
    """From `entry` at `after_ts`, did +5% come before -3%? None = no bars to say."""
    later = [b for b in bars if b[0] >= after_ts]
    if not later:
        return None
    tp, sl = entry * target, entry * stop
    for b in later:
        if b[3] <= sl:
            return False          # stop first (or same bar: conservative)
        if b[2] >= tp:
            return True
    return False


def opportunity(m: DayMove, *, bars: Optional[Sequence[Bar]] = None,
                rth_open_ts: Optional[int] = None, min_move: float = 0.05) -> str:
    """NONE / GAP_ONLY / EXECUTABLE / UNKNOWN_ORDERING for one day's move."""
    if m.prev_close <= 0 or m.high / m.prev_close - 1 < min_move:
        return "NONE"
    if m.open <= 0 or m.high / m.open - 1 < min_move:
        return "GAP_ONLY"         # the whole gain happened before anyone could buy
    if bars and rth_open_ts is not None:
        ok = executable_from(m.open, bars, rth_open_ts)
        if ok is None:
            return "UNKNOWN_ORDERING"
        return "EXECUTABLE" if ok else "GAP_ONLY"
    if m.low <= m.open * 0.97:
        return "UNKNOWN_ORDERING"  # daily bar: stop and target both touched, order unknown
    return "EXECUTABLE"


def label(symbol: str, *, universe: Set[str], data_down: Set[str], catalyst_symbols: Set[str],
          radar: Dict[str, RadarRecord], alert_deadline_ts: Optional[int] = None) -> str:
    if symbol not in universe:
        return "UNIVERSE_COVERAGE"
    if symbol in data_down:
        return "DATA_INTERRUPTION"
    r = radar.get(symbol)
    if r is None or r.first_seen_ts is None:
        return "CATALYST_MISSED" if symbol in catalyst_symbols else "DETECTION_FAILURE"
    if r.forecast_issued:
        late = alert_deadline_ts is not None and (r.alert_delivered_ts is None or r.alert_delivered_ts > alert_deadline_ts)
        if late or r.filled is False:
            return "ALERT_EXECUTION_FAILURE"
        return "CAUGHT"
    if r.rejected_reason and any(w in r.rejected_reason.lower() for w in _LIQUIDITY_WORDS):
        return "RISK_LIQUIDITY_EXCLUSION"
    return "STRATEGY_REJECTION"


@dataclass
class AuditReport:
    session_date: str
    movers: int = 0
    executable: int = 0
    gap_only: int = 0
    unknown_ordering: int = 0
    caught: int = 0
    labels: Dict[str, int] = field(default_factory=lambda: {k: 0 for k in LABELS})
    correct_rejections: int = 0
    wrong_rejections: int = 0
    rows: List[Dict[str, object]] = field(default_factory=list)

    @property
    def recall(self) -> Optional[float]:
        return self.caught / self.executable if self.executable else None

    @property
    def rejection_precision(self) -> Optional[float]:
        n = self.correct_rejections + self.wrong_rejections
        return self.correct_rejections / n if n else None


def audit(session_date: str, moves: Iterable[DayMove], *, universe: Set[str], data_down: Set[str],
          catalyst_symbols: Set[str], radar: Dict[str, RadarRecord],
          minute_bars: Optional[Dict[str, Sequence[Bar]]] = None, rth_open_ts: Optional[int] = None,
          alert_deadline_ts: Optional[int] = None) -> AuditReport:
    rep = AuditReport(session_date)
    minute_bars = minute_bars or {}
    seen_moves = set()
    for m in moves:
        seen_moves.add(m.symbol)
        opp = opportunity(m, bars=minute_bars.get(m.symbol), rth_open_ts=rth_open_ts)
        if opp == "NONE":
            continue
        rep.movers += 1
        row = {"symbol": m.symbol, "opportunity": opp,
               # move_pct is the intraday PEAK vs the prior close (the best exit anyone had);
               # close_pct is where it finished -- PAAI 2026-09-22 peaked +44% and closed -7%.
               "move_pct": round((m.high / m.prev_close - 1) * 100, 2),
               "close_pct": round((m.close / m.prev_close - 1) * 100, 2)}
        if opp == "GAP_ONLY":
            rep.gap_only += 1
        elif opp == "UNKNOWN_ORDERING":
            rep.unknown_ordering += 1
        else:
            rep.executable += 1
            lab = label(m.symbol, universe=universe, data_down=data_down,
                        catalyst_symbols=catalyst_symbols, radar=radar, alert_deadline_ts=alert_deadline_ts)
            row["label"] = lab
            if lab == "CAUGHT":
                rep.caught += 1
            else:
                rep.labels[lab] += 1
            if lab in ("STRATEGY_REJECTION", "RISK_LIQUIDITY_EXCLUSION"):
                rep.wrong_rejections += 1
        rep.rows.append(row)
    # Rejected names that offered NO executable move: the rules were right.
    exec_syms = {r["symbol"] for r in rep.rows if r.get("opportunity") == "EXECUTABLE"}
    for sym, r in radar.items():
        if r.rejected_reason and sym not in exec_syms:
            rep.correct_rejections += 1
    return rep
