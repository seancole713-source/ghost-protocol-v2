"""Data health, reported beside every result -- because silence has two meanings.

"No qualifying setups, coverage healthy" and "signals paused, coverage
incomplete" produce the same empty screen. They are opposite situations. Each
strategy declares the sources it REQUIRES; only those gate it. Missing borrow
data pauses a crowded-short strategy and leaves a catalyst breakout running.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

FRESH, STALE, DOWN, UNKNOWN = "FRESH", "STALE", "DOWN", "UNKNOWN"


@dataclass(frozen=True)
class SourceHealth:
    name: str
    last_event_ts: Optional[int]
    max_age_s: int                    # older than this = STALE
    covered: Optional[int] = None     # symbols with usable data
    expected: Optional[int] = None    # symbols that should have it
    min_coverage: float = 0.9
    error: Optional[str] = None       # last hard failure (403, timeout...)

    def status(self, now: int) -> str:
        if self.error and self.last_event_ts is None:
            return DOWN
        if self.last_event_ts is None:
            return UNKNOWN
        if now - self.last_event_ts > self.max_age_s:
            return STALE
        if self.expected and self.covered is not None and self.covered / self.expected < self.min_coverage:
            return STALE
        return FRESH

    def coverage(self) -> Optional[float]:
        if not self.expected or self.covered is None:
            return None
        return self.covered / self.expected


def assess(sources: Iterable[SourceHealth], now: int) -> Dict[str, Dict[str, object]]:
    return {s.name: {"status": s.status(now), "coverage": s.coverage(),
                     "age_s": (now - s.last_event_ts) if s.last_event_ts else None,
                     "error": s.error} for s in sources}


def release_allowed(required: Iterable[str], report: Dict[str, Dict[str, object]]) -> List[str]:
    """Blocking problems for one strategy. Empty list == its data is healthy."""
    problems = []
    for name in required:
        st = report.get(name, {}).get("status", UNKNOWN)
        if st != FRESH:
            problems.append(f"{name}: {st}")
    return problems


def banner(n_setups: int, blocking: Dict[str, List[str]]) -> str:
    """The first line of every morning answer.

    The shadow card and paper orders are recorded regardless of this check; the pause is what
    a LIVE release would do. So a card that issued forecasts never reads "Trading signals
    paused" beside them (audit 2026-09-25 U52): it says how many setups were recorded and that
    a live release would have been paused."""
    paused = {k: v for k, v in blocking.items() if v}
    head = (f"{n_setups} setup{'s' if n_setups != 1 else ''}." if n_setups
            else "No qualifying setups.")
    if paused and len(paused) == len(blocking):
        problems = "; ".join(sorted({p for v in paused.values() for p in v}))
        if n_setups:
            return (f"{n_setups} setup{'s' if n_setups != 1 else ''} recorded (shadow + paper only). "
                    f"A live release would be paused: market-data coverage incomplete: {problems}")
        return "Trading signals paused. Market-data coverage incomplete: " + problems
    if paused:
        return head + " Partial coverage -- paused: " + ", ".join(sorted(paused)) + "."
    return head + " Coverage healthy."
