"""The frozen forward ledger -- where a forecast becomes evidence, or doesn't.

Five refusals make the record trustworthy, and each is enforced in code rather
than left to discipline (ten months of Ghost showed discipline alone erodes):

  1. An experiment's spec is hashed at registration; a changed spec under the
     same id is REFUSED. A change is a new version with its own record.
  2. A forecast must be RECORDED before its window opens -- not merely claim an
     earlier issuance time.
  3. One forecast per experiment, symbol and session. Re-recording the same one
     is a no-op; recording a DIFFERENT one under that key is refused.
  4. Terminal outcomes are immutable. Only UNRESOLVED (data missing) may be
     overwritten by a later resolver run. Each forecast carries THREE separate
     outcomes -- "forecast" (did the market do it), "simulated" (could the
     order have made money), "actual" (what the operator's broker did) -- and
     none may stand in for another.
  5. Abstentions are recorded too. A record of only the trades taken cannot
     show how selective the system was, or what it declined.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Protocol

from edge import stats
from edge.contracts import (
    COUNTED, EXCLUDED, TERMINAL, WIN, ContractError, ExperimentSpec, Forecast, FrozenSpecError,
)
from edge.resolver import Resolution


class Store(Protocol):
    def get(self, table: str, key: str) -> Optional[Dict[str, Any]]: ...
    def put(self, table: str, key: str, row: Dict[str, Any]) -> None: ...
    def scan(self, table: str, **where: Any) -> List[Dict[str, Any]]: ...


class MemoryStore:
    """In-process store for tests and dry runs."""

    def __init__(self) -> None:
        self._t: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def get(self, table, key):
        row = self._t.get(table, {}).get(key)
        return dict(row) if row is not None else None

    def put(self, table, key, row):
        self._t.setdefault(table, {})[key] = dict(row)

    def claim(self, name, *, owner, ttl_s, now=None):
        import time as _time
        now = int(now if now is not None else _time.time())
        cur = self._t.setdefault("edge_lease", {}).get(name)
        if cur and cur["owner"] != owner and now - cur["at"] < ttl_s:
            return False
        self._t["edge_lease"][name] = {"owner": owner, "at": now}
        return True

    def prune(self, table, *, older_than):
        rows = self._t.get(table, {})
        drop = [k for k, v in rows.items() if int(v.get("_at", v.get("written_at", 0)) or 0) < older_than]
        for k in drop:
            rows.pop(k)
        return len(drop)

    def release(self, name, *, owner):
        cur = self._t.get("edge_lease", {}).get(name)
        if cur and cur["owner"] == owner:
            cur["at"] = 0

    def scan(self, table, **where):
        return [dict(r) for r in self._t.get(table, {}).values()
                if all(r.get(k) == v for k, v in where.items())]


class Ledger:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ------------------------------------------------------------ experiments
    def register(self, spec: ExperimentSpec, *, now: int) -> Dict[str, Any]:
        eid, h = spec.experiment_id, spec.spec_hash()
        existing = self.store.get("experiments", eid)
        if existing:
            if existing["spec_hash"] != h:
                raise FrozenSpecError(
                    f"{eid} is frozen (hash {existing['spec_hash'][:12]}); "
                    f"register the change as v{spec.version + 1}")
            return existing
        row = {"experiment_id": eid, "spec_hash": h, "spec": spec.canonical(),
               "registered_at": int(now), "status": "active"}
        self.store.put("experiments", eid, row)
        return row

    def _spec_hash(self, eid: str) -> str:
        row = self.store.get("experiments", eid)
        if not row:
            raise ContractError(f"experiment {eid} is not registered")
        return row["spec_hash"]

    # -------------------------------------------------------------- forecasts
    def record(self, f: Forecast, *, now: int) -> Dict[str, Any]:
        if f.spec_hash != self._spec_hash(f.experiment_id):
            raise FrozenSpecError("forecast was built from a different spec than the one registered")
        if int(now) >= f.window_start:
            raise ContractError("recorded after the window opened -- that is hindsight, not a forecast")
        if f.issued_at > int(now):
            raise ContractError("issued_at is in the future relative to the recording time")
        row = {**f.to_dict(), "recorded_at": int(now)}
        existing = self.store.get("forecasts", f.forecast_id)
        if existing:
            same = {k: v for k, v in existing.items() if k != "recorded_at"} == f.to_dict()
            if not same:
                raise ContractError("a different forecast already exists for this symbol and session")
            return existing
        self.store.put("forecasts", f.forecast_id, row)
        return row

    def abstain(self, *, experiment_id: str, symbol: str, session_date: str,
                reasons: List[str], now: int) -> Dict[str, Any]:
        self._spec_hash(experiment_id)
        key = f"{experiment_id}|{symbol.upper()}|{session_date}"
        row = {"key": key, "experiment_id": experiment_id, "symbol": symbol.upper(),
               "session_date": session_date, "reasons": list(reasons), "recorded_at": int(now)}
        if not self.store.get("abstentions", key):
            self.store.put("abstentions", key, row)
        return row

    # --------------------------------------------------------------- outcomes
    RECORDS = ("forecast", "simulated", "actual")

    def settle(self, forecast_id: str, r: Resolution, *, now: int,
               record: str = "simulated") -> Dict[str, Any]:
        if record not in self.RECORDS:
            raise ContractError(f"record must be one of {self.RECORDS}")
        if not self.store.get("forecasts", forecast_id):
            raise ContractError("no such forecast")
        if self.store.get("exclusions", forecast_id):
            raise ContractError("forecast is excluded; it takes no outcomes")
        key = f"{forecast_id}|{record}"
        prior = self.store.get("outcomes", key)
        if prior and prior["outcome"] in TERMINAL:
            if prior["outcome"] == r.outcome and prior.get("exit_price") == r.exit_price:
                return prior
            raise ContractError(f"outcome already final ({prior['outcome']}); it cannot be rewritten")
        row = {"forecast_id": forecast_id, "record": record, **asdict(r),
               "flags": list(r.flags), "settled_at": int(now)}
        self.store.put("outcomes", key, row)
        return row

    def exclude(self, forecast_id: str, *, reason: str, now: int) -> Dict[str, Any]:
        """Remove a forecast from the counted sample -- with a reason, on the record.

        Only for events outside the contract (a halt through the window, a
        corporate action discovered late). Never for an outcome someone
        dislikes: a settled trade cannot be excluded.
        """
        if not reason.strip():
            raise ContractError("an exclusion needs a reason")
        if not self.store.get("forecasts", forecast_id):
            raise ContractError("no such forecast")
        for rec in self.RECORDS:
            prior = self.store.get("outcomes", f"{forecast_id}|{rec}")
            if prior and prior["outcome"] in TERMINAL:
                raise ContractError("a settled outcome cannot be excluded")
        row = {"forecast_id": forecast_id, "outcome": EXCLUDED, "reason": reason,
               "excluded_at": int(now)}
        self.store.put("exclusions", forecast_id, row)
        return row

    # ----------------------------------------------------------------- report
    def report(self, experiment_id: str) -> Dict[str, Any]:
        """One report per record, side by side. Never merged into one number."""
        spec_row = self.store.get("experiments", experiment_id)
        if not spec_row:
            raise ContractError(f"experiment {experiment_id} is not registered")
        forecasts = self.store.scan("forecasts", experiment_id=experiment_id)
        excluded = [f for f in forecasts if self.store.get("exclusions", f["forecast_id"])]
        live = [f for f in forecasts if not self.store.get("exclusions", f["forecast_id"])]
        abstentions = self.store.scan("abstentions", experiment_id=experiment_id)
        base = {
            "experiment_id": experiment_id, "spec_hash": spec_row["spec_hash"],
            "forecasts": len(forecasts), "excluded": len(excluded),
            "abstentions": len(abstentions),
            "records": {rec: self._record_report(spec_row, live, rec) for rec in self.RECORDS},
        }
        base.update(base["records"]["simulated"])     # legacy flat view = simulated
        return base

    def _record_report(self, spec_row: Dict[str, Any], forecasts: List[Dict[str, Any]],
                       record: str) -> Dict[str, Any]:
        outcomes = {f["forecast_id"]: self.store.get("outcomes", f"{f['forecast_id']}|{record}")
                    for f in forecasts}
        by = {}
        for o in outcomes.values():
            key = o["outcome"] if o else "PENDING"
            by[key] = by.get(key, 0) + 1
        counted = [o for o in outcomes.values() if o and o["outcome"] in COUNTED]
        n, wins = len(counted), sum(1 for o in counted if o["outcome"] == WIN)
        lo, hi = stats.wilson(wins, n)
        spec = json.loads(spec_row["spec"])
        be = (1 - spec["stop_mult"]) / ((spec["target_mult"] - 1) + (1 - spec["stop_mult"]))
        probs = [(f["prob"], outcomes[f["forecast_id"]]["outcome"] == WIN)
                 for f in forecasts
                 if f.get("prob") is not None and outcomes.get(f["forecast_id"])
                 and outcomes[f["forecast_id"]]["outcome"] in COUNTED]
        verdict = "no filled trades yet"
        if n:
            if lo > be:
                verdict = "edge shown: the whole interval is above break-even"
            elif hi < be:
                verdict = "no edge: the whole interval is below break-even"
            else:
                verdict = f"undecided: the interval straddles break-even ({be:.1%})"
        return {
            "by_outcome": by, "filled": n, "wins": wins,
            "win_rate": wins / n if n else None, "win_rate_ci": [lo, hi] if n else None,
            "break_even": be,
            "ambiguous_losses": sum(1 for o in counted if o.get("ambiguous")),
            "expectancy_usd": stats.expectancy([o["pnl_usd"] for o in counted]),
            "calibration": stats.calibration_bands([p for p, _ in probs], [h for _, h in probs]) if probs else None,
            "verdict": verdict,
        }
