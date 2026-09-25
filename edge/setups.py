"""Strategies are separate, and only a strategy's REQUIRED inputs can block it.

Each strategy lists required and optional detectors. The decision:
  any required FAIL      -> REJECTED, with every failing reason
  any required UNKNOWN   -> DATA_UNAVAILABLE, naming what is missing
  all required PASS      -> ELIGIBLE
Optional detectors are reported, never blocking. A confirmed catalyst breakout
is not made invisible because the borrow feed is down.

Thresholds in every strategy except premarket_continuation (the frozen
Gap-and-Go v1 rule) are UNVALIDATED v0 hypotheses. They exist to be evaluated
walk-forward and registered as experiments -- not to be believed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List

from edge.catalysts import MECHANICAL as _MECHANICAL
from edge.detectors import FAIL, PASS, UNKNOWN, Signal

ELIGIBLE, REJECTED, DATA_UNAVAILABLE = "ELIGIBLE", "REJECTED", "DATA_UNAVAILABLE"


@dataclass(frozen=True)
class Strategy:
    name: str
    required: tuple
    optional: tuple = ()
    validated: bool = False
    note: str = ""


@dataclass
class Decision:
    strategy: str
    verdict: str
    reasons: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    signals: Dict[str, Signal] = field(default_factory=dict)
    validated: bool = False


STRATEGIES = {
    s.name: s for s in [
        Strategy("premarket_continuation", required=("gap", "liquidity", "catalyst", "not_dilutive"),
                 optional=("rvol_tod",), validated=False,
                 note="the frozen Gap-and-Go v1 rule; validated only by its own forward record"),
        Strategy("gap_baseline", required=("gap", "liquidity"), optional=(),
                 note="baseline: the same gap and liquidity rules with NO catalyst or dilution check"),
        Strategy("catalyst_breakout", required=("catalyst", "not_dilutive", "liquidity", "orb_break", "rvol_tod"),
                 optional=("vwap_hold", "crowded_short"), note="v0 hypothesis"),
        Strategy("crowded_short_ignition", required=("crowded_short", "liquidity", "rvol_tod", "acceleration"),
                 optional=("catalyst", "vwap_hold"), note="v0 hypothesis"),
        Strategy("short_interest_ignition", required=("short_interest", "liquidity", "rvol_tod", "acceleration"),
                 optional=("catalyst", "crowded_short"), note="v0 hypothesis"),
        Strategy("intraday_continuation", required=("liquidity", "vwap_hold", "rvol_tod", "acceleration"),
                 optional=("catalyst",), note="v0 hypothesis"),
        # v2 = v1 + a dilution / reverse-split veto (rule E5). 2026-09-24: v1 bought GRML the
        # morning after a $12 registered direct offering and was stopped out. v1 keeps running
        # unchanged beside it, so the two records say whether the veto adds anything.
        Strategy("intraday_continuation_v2",
                 required=("liquidity", "vwap_hold", "rvol_tod", "acceleration", "not_dilutive"),
                 optional=("catalyst",), note="v0 hypothesis"),
    ]
}


def decide(strategy: str, signals: Dict[str, Signal]) -> Decision:
    s = STRATEGIES[strategy]
    d = Decision(strategy, ELIGIBLE, signals=dict(signals), validated=s.validated)
    for name in s.required:
        sig = signals.get(name)
        if sig is None or sig.state == UNKNOWN:
            d.missing.append(name if sig is None else f"{name}: {sig.evidence.get('missing', 'unknown')}")
        elif sig.state == FAIL:
            why = sig.evidence.get("reasons") or [f"{name} {sig.value} fails {sig.threshold}"]
            d.reasons.extend(why if isinstance(why, list) else [str(why)])
    if d.reasons:
        d.verdict = REJECTED
    elif d.missing:
        d.verdict = DATA_UNAVAILABLE
    return d


def catalyst_signal(events: list) -> Signal:
    """PASS for a dated company-specific event; FAIL when the feed answered with none."""
    if events is None:
        return Signal("catalyst", UNKNOWN, evidence={"missing": "catalyst feed"})
    specific = [e for e in events if getattr(e, "company_specific", False)]
    if specific:
        e = specific[0]
        return Signal("catalyst", PASS, evidence={"kind": e.kind, "headline": e.headline, "url": e.url})
    return Signal("catalyst", FAIL, threshold="company-specific event <24h",
                  evidence={"reasons": ["no dated company-specific catalyst (sector sympathy does not count)"]})


def dilution_signal(events: list) -> Signal:
    if events is None:
        return Signal("not_dilutive", UNKNOWN, evidence={"missing": "catalyst feed"})
    bad = [e for e in events if getattr(e, "dilutive", False)]
    if bad:
        return Signal("not_dilutive", FAIL, evidence={"reasons": [f"dilution: {bad[0].headline}"]})
    mech = [e for e in events if getattr(e, "kind", "") in _MECHANICAL]
    if mech:   # rule E5 is "not mechanical or dilutive"; a reverse split was slipping through
        return Signal("not_dilutive", FAIL, evidence={"reasons": [f"mechanical: {mech[0].headline}"]})
    return Signal("not_dilutive", PASS)
