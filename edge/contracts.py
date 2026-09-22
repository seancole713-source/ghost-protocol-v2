"""What a forecast is -- fixed before its window opens.

A forecast that can be reinterpreted after the fact is not a forecast. Every
field an outcome depends on is set at issuance: the entry reference, the
trigger, the target, the stop, the deadline. "The stock went up sometime
afterwards" is not an outcome; target-before-stop inside the window is.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dtime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


class FrozenSpecError(ValueError):
    """An experiment's spec was changed after registration."""


class ContractError(ValueError):
    """A forecast breaks its own contract (issued late, levels inconsistent...)."""


# Terminal outcomes are immutable once written. UNRESOLVED is the only state a
# later resolver run may overwrite -- it means "data missing", never "lost".
WIN, LOSS, TIME_EXIT, NO_FILL = "WIN", "LOSS", "TIME_EXIT", "NO_FILL"
EXCLUDED, UNRESOLVED = "EXCLUDED", "UNRESOLVED"
TERMINAL = frozenset({WIN, LOSS, TIME_EXIT, NO_FILL, EXCLUDED})
COUNTED = frozenset({WIN, LOSS, TIME_EXIT})   # a filled trade


@dataclass(frozen=True)
class ExperimentSpec:
    """Everything that defines an experiment. Hashed; any change is a new version.

    Times are exchange-local ("HH:MM" America/New_York). Multipliers apply to
    the entry reference price captured at issuance.
    """
    name: str
    version: int
    setup: str                       # e.g. "gap_and_go"
    description: str
    trigger_mult: float              # buy-stop = ref x trigger_mult
    limit_mult: float                # no fill above ref x limit_mult
    target_mult: float               # take-profit = entry_trigger x target_mult
    stop_mult: float                 # stop-loss   = entry_trigger x stop_mult
    entry_expiry_et: str             # unfilled entry cancelled at this time
    time_exit_et: str                # anything open is sold at this time
    size_usd: float
    max_per_day: int
    min_prob: Optional[float] = None # abstain below this calibrated probability
    eligibility: Dict[str, Any] = field(default_factory=dict)

    @property
    def experiment_id(self) -> str:
        return f"{self.name}@v{self.version}"

    def canonical(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def spec_hash(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()

    def break_even_win_rate(self) -> float:
        gain = self.target_mult - 1.0
        loss = 1.0 - self.stop_mult
        return loss / (gain + loss)


def _clock(day: date, hhmm: str) -> int:
    hh, mm = (int(x) for x in hhmm.split(":"))
    return int(datetime.combine(day, dtime(hh, mm), tzinfo=ET).timestamp())


def _cents(x: float) -> float:
    return round(x + 1e-9, 2)


@dataclass(frozen=True)
class Forecast:
    forecast_id: str
    experiment_id: str
    spec_hash: str
    symbol: str
    session_date: str                # YYYY-MM-DD, exchange date
    issued_at: int                   # epoch s -- must precede window_start
    window_start: int                # epoch s -- the regular open
    entry_expiry: int
    time_exit: int
    entry_ref: float
    entry_trigger: float
    entry_limit: float
    target: float
    stop: float
    shares: int
    prob: Optional[float] = None     # calibrated P(target before stop); None = uncalibrated
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def forecast_id_for(experiment_id: str, symbol: str, session_date: str) -> str:
    """Deterministic: one forecast per experiment, symbol and session.

    Repeated scans must never inflate the sample. A second issuance for the
    same key is the same forecast, not another one.
    """
    raw = f"{experiment_id}|{symbol.upper()}|{session_date}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def issue(
    spec: ExperimentSpec, *, symbol: str, session_date: date, entry_ref: float,
    issued_at: int, prob: Optional[float] = None,
    evidence: Optional[Dict[str, Any]] = None, window_open_et: str = "09:30",
) -> Forecast:
    """Build a forecast from a spec. Refuses anything the contract forbids."""
    ref = float(entry_ref)
    if not math.isfinite(ref) or ref <= 0:
        raise ContractError("entry reference must be a positive price")
    window_start = _clock(session_date, window_open_et)
    if issued_at >= window_start:
        raise ContractError("a forecast must be issued before its window opens")
    if prob is not None and not (0.0 <= prob <= 1.0):
        raise ContractError("prob must be a probability in [0, 1]")
    if spec.min_prob is not None and (prob is None or prob < spec.min_prob):
        raise ContractError("below the spec's abstention threshold: record an abstention instead")
    trigger = _cents(ref * spec.trigger_mult)
    limit = _cents(ref * spec.limit_mult)
    target = _cents(trigger * spec.target_mult)
    stop = _cents(trigger * spec.stop_mult)
    if not (stop < trigger <= limit and trigger < target):
        raise ContractError("levels are inconsistent")
    shares = int(spec.size_usd // trigger)
    if shares < 1:
        raise ContractError("size buys less than one share")
    day = session_date.isoformat()
    return Forecast(
        forecast_id=forecast_id_for(spec.experiment_id, symbol, day),
        experiment_id=spec.experiment_id, spec_hash=spec.spec_hash(),
        symbol=symbol.upper(), session_date=day, issued_at=int(issued_at),
        window_start=window_start,
        entry_expiry=_clock(session_date, spec.entry_expiry_et),
        time_exit=_clock(session_date, spec.time_exit_et),
        entry_ref=_cents(ref), entry_trigger=trigger, entry_limit=limit,
        target=target, stop=stop, shares=shares, prob=prob,
        evidence=dict(evidence or {}),
    )


# The rule the operator is trading from 2026-09-23 (docs/gap_and_go_v1.md),
# expressed as a spec so the edge ledger can shadow it. Its canonical record is
# the operator's scoreboard; this copy exists so the two can be reconciled.
GAP_AND_GO_V1 = ExperimentSpec(
    name="gap_and_go", version=1, setup="gap_and_go",
    description="Premarket +5..40% gapper with a dated company catalyst; "
                "buy-stop continuation, +5% / -3% bracket, flat by 15:30 ET.",
    trigger_mult=1.01, limit_mult=1.02, target_mult=1.05, stop_mult=0.97,
    entry_expiry_et="10:30", time_exit_et="15:30", size_usd=1000.0, max_per_day=2,
    eligibility={
        "move_pct": [5.0, 40.0], "price": [2.0, 500.0],
        "min_avg_shares": 500_000, "min_avg_dollars": 5_000_000,
        "catalyst": "dated company-specific event <24h, sourced",
        "exclude": ["offering/ATM/dilution <24h", "ex-dividend day", "split day"],
        "rank": "avg_dollar_volume desc",
    },
)


def issue_intraday(
    spec: ExperimentSpec, *, symbol: str, session_date: date, entry_ref: float, issued_at: int,
    prob: Optional[float] = None, evidence: Optional[Dict[str, Any]] = None,
) -> Forecast:
    """An intraday forecast: its window opens the minute AFTER issuance.

    Same freeze as a premarket forecast -- recorded before its window opens --
    but the window is relative. The entry expires `entry_window_min` (from the
    spec's eligibility, so it is inside the hash) after issuance; the time exit
    is the spec's fixed clock time.
    """
    ref = float(entry_ref)
    if not math.isfinite(ref) or ref <= 0:
        raise ContractError("entry reference must be a positive price")
    minutes = int((spec.eligibility or {}).get("entry_window_min") or 0)
    if minutes <= 0:
        raise ContractError("an intraday spec must state entry_window_min")
    window_start = int(issued_at) + 60
    time_exit = _clock(session_date, spec.time_exit_et)
    if window_start + minutes * 60 >= time_exit:
        raise ContractError("too late in the session for this setup's entry window")
    if spec.min_prob is not None and (prob is None or prob < spec.min_prob):
        raise ContractError("below the spec's abstention threshold: record an abstention instead")
    trigger = _cents(ref * spec.trigger_mult)
    limit = _cents(ref * spec.limit_mult)
    target = _cents(trigger * spec.target_mult)
    stop = _cents(trigger * spec.stop_mult)
    if not (stop < trigger <= limit and trigger < target):
        raise ContractError("levels are inconsistent")
    shares = int(spec.size_usd // trigger)
    if shares < 1:
        raise ContractError("size buys less than one share")
    day = session_date.isoformat()
    return Forecast(
        forecast_id=forecast_id_for(spec.experiment_id, symbol, day),
        experiment_id=spec.experiment_id, spec_hash=spec.spec_hash(),
        symbol=symbol.upper(), session_date=day, issued_at=int(issued_at),
        window_start=window_start, entry_expiry=window_start + minutes * 60, time_exit=time_exit,
        entry_ref=_cents(ref), entry_trigger=trigger, entry_limit=limit,
        target=target, stop=stop, shares=shares, prob=prob, evidence=dict(evidence or {}),
    )
