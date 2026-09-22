"""The radar notices widely; the engine decides narrowly. This is the radar.

Every symbol it touches moves through named states, and every move carries a
reason. That solves the worst UX failure of a strict engine: silence. A stock
the system saw but did not approve is SHOWN, with why -- neither hidden nor
dressed up as a pick to chase.

    DETECTED -> WATCHING -> SETUP_FORMING -> ENTRY_ELIGIBLE -> POSITION_OPEN -> CLOSED
    side exits: EXPIRED, DATA_UNAVAILABLE, REJECTED (reason required)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

DETECTED, WATCHING, SETUP_FORMING = "DETECTED", "WATCHING", "SETUP_FORMING"
ENTRY_ELIGIBLE, POSITION_OPEN, CLOSED = "ENTRY_ELIGIBLE", "POSITION_OPEN", "CLOSED"
EXPIRED, DATA_UNAVAILABLE, REJECTED = "EXPIRED", "DATA_UNAVAILABLE", "REJECTED"

_ALLOWED = {
    DETECTED: {WATCHING, REJECTED, DATA_UNAVAILABLE, EXPIRED},
    WATCHING: {SETUP_FORMING, REJECTED, DATA_UNAVAILABLE, EXPIRED},
    SETUP_FORMING: {ENTRY_ELIGIBLE, WATCHING, REJECTED, DATA_UNAVAILABLE, EXPIRED},
    ENTRY_ELIGIBLE: {POSITION_OPEN, EXPIRED, REJECTED, DATA_UNAVAILABLE},
    POSITION_OPEN: {CLOSED},                       # an open position never "expires"
    DATA_UNAVAILABLE: {WATCHING, SETUP_FORMING, EXPIRED},   # data came back
    EXPIRED: {WATCHING},                           # only for a NEW, separate setup
    REJECTED: set(), CLOSED: set(),
}
_NEEDS_REASON = {REJECTED, EXPIRED, DATA_UNAVAILABLE}


class TransitionError(ValueError):
    pass


@dataclass
class RadarItem:
    symbol: str
    session_date: str
    detected_at: int
    detected_move_pct: float
    strategy: Optional[str] = None
    state: str = DETECTED
    history: List[Dict[str, Any]] = field(default_factory=list)

    def transition(self, to: str, *, ts: int, reason: str = "",
                   strategy: Optional[str] = None, evidence: Optional[Dict[str, Any]] = None) -> None:
        if to not in _ALLOWED.get(self.state, set()):
            raise TransitionError(f"{self.symbol}: {self.state} -> {to} is not allowed")
        if to in _NEEDS_REASON and not reason.strip():
            raise TransitionError(f"{to} requires a reason")
        if self.state == EXPIRED and to == WATCHING and not strategy:
            raise TransitionError("re-watching an expired name needs a separately named setup")
        self.history.append({"ts": ts, "from": self.state, "to": to, "reason": reason,
                             "strategy": strategy or self.strategy, "evidence": evidence or {}})
        if strategy:
            self.strategy = strategy
        self.state = to

    def describe(self, *, current_move_pct: Optional[float] = None) -> str:
        """One line a person reads on a phone."""
        parts = [f"Detected at {self.detected_move_pct:+.1f}%."]
        if current_move_pct is not None:
            parts.append(f"Currently {current_move_pct:+.1f}%.")
        last = self.history[-1] if self.history else None
        if self.state == EXPIRED:
            parts.append(f"Original entry expired: {last['reason']}.")
        elif self.state == WATCHING and last and last["from"] == EXPIRED:
            parts.append(f"Original entry expired; waiting for a separately validated {self.strategy} setup.")
        elif self.state == REJECTED:
            parts.append(f"Rejected: {last['reason']}.")
        elif self.state == DATA_UNAVAILABLE:
            parts.append(f"Data unavailable: {last['reason']}.")
        elif self.state == ENTRY_ELIGIBLE:
            parts.append(f"Entry eligible under {self.strategy}.")
        elif self.state == SETUP_FORMING:
            parts.append(f"Setup forming ({self.strategy}); not yet eligible.")
        elif self.state == POSITION_OPEN:
            parts.append("Position open.")
        elif self.state == CLOSED:
            parts.append("Closed.")
        else:
            parts.append("Watching.")
        return " ".join(parts)
