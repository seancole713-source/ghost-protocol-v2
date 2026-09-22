"""What the operator reads on a phone. Short, and never louder than the evidence."""
from __future__ import annotations

from typing import Dict, List, Optional

from edge import radar as R


def morning_headline(items: List[R.RadarItem], health_banner: str) -> str:
    eligible = sum(1 for i in items if i.state == R.ENTRY_ELIGIBLE)
    forming = sum(1 for i in items if i.state in (R.SETUP_FORMING, R.WATCHING))
    total = eligible + forming
    if not total:
        return health_banner
    word = lambda n, s: f"{n} {s}{'' if n == 1 else 's'}"
    parts = [word(total, "developing setup") + "."]
    if eligible:
        parts.append(f"{eligible} currently eligible.")
    if forming:
        parts.append(f"{forming} waiting for confirmation.")
    tail = health_banner.split(". ", 1)[-1] if ". " in health_banner else health_banner
    return " ".join(parts) + " " + tail


def setup_card(*, symbol: str, why: str, sources: List[str], strategy: str, entry_rule: str,
               max_entry: float, expires_et: str, target: float, stop: float, time_exit_et: str,
               prob: Optional[float], prob_n: Optional[int], validated: bool,
               exposure_note: str = "") -> str:
    if validated and prob is not None and prob_n:
        conf = f"Validated estimate {prob:.0%} (n={prob_n}, interval on the scoreboard)."
    else:
        conf = "Experimental: no validated probability yet."
    lines = [
        f"{symbol} — {strategy}",
        f"Why: {why}",
        "Sources: " + (" · ".join(sources) if sources else "none — do not act"),
        f"Entry: {entry_rule}; no fill above {max_entry:.2f}; expires {expires_et} ET.",
        f"Target {target:.2f} · Stop {stop:.2f} · Out by {time_exit_et} ET.",
        conf,
    ]
    if exposure_note:
        lines.append(exposure_note)
    return "\n".join(lines)
