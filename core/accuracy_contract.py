"""Ghost accuracy contract — single source of truth for the 70%+ target.

Set GHOST_ACCURACY_CONTRACT=70 (default) to align training gates, live firing,
objective mode, kill-switch floors, and precision targets without hunting env
vars across modules. Explicit per-knob env overrides still win when set.

Contracts:
  70      — production target: >=70% OOS precision to fire, balanced objective
  80      — north-star precision mode (stricter training + firing)
  legacy  — pre-audit aggressive settings (NOT recommended)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class ContractSpec:
    name: str
    target_win_rate: float
    min_holdout_acc: float
    min_wf_acc_mean: float
    min_wf_folds: int
    min_edge: float
    min_win_proba: float
    precision_target: float
    objective_mode: str
    objective_bootstrap_min_conf: float
    objective_min_samples: int
    kill_winrate_floor: float
    min_alert_confidence: float
    research_bypass_precision: bool


CONTRACTS: Dict[str, ContractSpec] = {
    "70": ContractSpec(
        name="70",
        target_win_rate=0.70,
        # Training admission: models must show ~60% OOS skill to be stored.
        # Live firing still requires precision_gate proof at 70% (below).
        min_holdout_acc=0.60,
        min_wf_acc_mean=0.60,
        min_wf_folds=4,
        min_edge=0.05,
        min_win_proba=0.55,
        precision_target=0.70,
        objective_mode="balanced",
        objective_bootstrap_min_conf=0.85,
        objective_min_samples=12,
        kill_winrate_floor=0.70,
        min_alert_confidence=0.80,
        research_bypass_precision=False,
    ),
    "80": ContractSpec(
        name="80",
        target_win_rate=0.80,
        min_holdout_acc=0.70,
        min_wf_acc_mean=0.70,
        min_wf_folds=5,
        min_edge=0.08,
        min_win_proba=0.60,
        precision_target=0.80,
        objective_mode="precision",
        objective_bootstrap_min_conf=0.90,
        objective_min_samples=20,
        kill_winrate_floor=0.70,
        min_alert_confidence=0.85,
        research_bypass_precision=False,
    ),
    "legacy": ContractSpec(
        name="legacy",
        target_win_rate=0.62,
        min_holdout_acc=0.38,
        min_wf_acc_mean=0.40,
        min_wf_folds=2,
        min_edge=0.0,
        min_win_proba=0.48,
        precision_target=0.62,
        objective_mode="aggressive",
        objective_bootstrap_min_conf=0.75,
        objective_min_samples=8,
        kill_winrate_floor=0.40,
        min_alert_confidence=0.75,
        research_bypass_precision=True,
    ),
}


def contract_name() -> str:
    raw = (os.getenv("GHOST_ACCURACY_CONTRACT") or "70").strip().lower()
    if raw in CONTRACTS:
        return raw
    if raw in ("aggressive", "old"):
        return "legacy"
    return "70"


def active_contract() -> ContractSpec:
    return CONTRACTS[contract_name()]


# Fields where env vars may only tighten the contract, never weaken it.
_FLOOR_FIELDS = frozenset({
    "min_holdout_acc",
    "min_wf_acc_mean",
    "min_edge",
    "min_win_proba",
    "precision_target",
    "target_win_rate",
    "kill_winrate_floor",
    "min_alert_confidence",
    "objective_bootstrap_min_conf",
})


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except Exception:
        return default


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(raw)
    except Exception:
        return default


def resolve_float(env_key: str, field: str, *, lo: Optional[float] = None, hi: Optional[float] = None) -> float:
    """Contract default; env may tighten floor fields but never weaken them."""
    spec = active_contract()
    default = float(getattr(spec, field))
    val = _env_float(env_key, default)
    if contract_name() in ("70", "80") and field in _FLOOR_FIELDS:
        val = max(val, default)
    if lo is not None:
        val = max(lo, val)
    if hi is not None:
        val = min(hi, val)
    return val


def resolve_int(env_key: str, field: str, *, lo: Optional[int] = None, hi: Optional[int] = None) -> int:
    spec = active_contract()
    default = int(getattr(spec, field))
    val = _env_int(env_key, default)
    if contract_name() in ("70", "80") and field == "min_wf_folds":
        val = max(val, default)
    if lo is not None:
        val = max(lo, val)
    if hi is not None:
        val = min(hi, val)
    return val


def research_bypasses_precision_gate() -> bool:
    """Research picks may skip the precision gate only in legacy contract or
    when RESEARCH_BYPASS_PRECISION=1 is explicitly set."""
    if (os.getenv("RESEARCH_BYPASS_PRECISION") or "").strip().lower() in ("1", "on", "true", "yes"):
        return True
    return active_contract().research_bypass_precision


def contract_summary() -> Dict[str, object]:
    spec = active_contract()
    return {
        "contract": spec.name,
        "target_win_rate": spec.target_win_rate,
        "precision_target": spec.precision_target,
        "objective_mode": spec.objective_mode,
        "research_bypass_precision": research_bypasses_precision_gate(),
    }
