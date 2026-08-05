"""core/research_forward.py — fixed-sample forward proof collection (Phase 7).

Manages the v2 confirmatory protocol: register an exact artifact, collect
exactly 50 forward actionable outcomes, and evaluate all gates. Never declares
success before 50. Early termination only for futility (42/50 impossible).

Read-only on live tables. Writes only to ghost_research_* tables.
"""
from __future__ import annotations

import json
import hashlib
import logging
import math
import time
from typing import Any, Dict, List, Optional

from core.binomial_stats import (
    V2_CONFIRMATORY_N,
    V2_MIN_WINS,
    V2_TARGET,
    V2_MIN_ISSUANCE_DATES,
    V2_MAX_SYMBOL_CONCENTRATION,
    V2_MAX_CALENDAR_DAYS,
    v2_confirmatory_status,
    exact_wilson_display,
    moving_block_bootstrap_lower_bound,
)

LOGGER = logging.getLogger("ghost.research_forward")
_TERMINAL_STATUSES = frozenset({"PROVEN", "FALSIFIED", "FUTILE", "INCOMPLETE"})
_MAX_INVALID_RATE = 0.10
_MIN_COVERAGE = 0.01
_MAX_BRIER = 0.25
_MAX_CALIBRATION_GAP = 0.10
_MAX_DRAWDOWN = 0.10
_BOOTSTRAP_BLOCK_SIZE = 5
_BOOTSTRAP_SAMPLES = 10000
_CALIBRATION_BINS = (
    ("<50", 0.0, 0.50),
    ("50-55", 0.50, 0.55),
    ("55-60", 0.55, 0.60),
    ("60-70", 0.60, 0.70),
    ("70+", 0.70, 1.01),
)


def register_forward_experiment(
    *,
    contract_id: str,
    artifact_sha: str,
    direction: str,
    threshold: float,
    symbol_universe: List[str],
    slice_spec: Optional[Dict[str, Any]] = None,
    source_manifest_sha: str = "",
    feature_manifest_sha: str = "",
    resolver_id: str = "tp_sl_bar_path/v1",
    family_size: int = 1,
    family_correction: str = "",
    selection_evidence: Optional[Dict[str, Any]] = None,
    round_trip_slippage_bps: Optional[float] = None,
    round_trip_commission_bps: Optional[float] = None,
    cur=None,
) -> Optional[str]:
    """Register a forward confirmatory experiment. Returns registration_id.

    Immutable — once registered, parameters cannot change. One registration
    per contract/artifact/direction.
    """
    import uuid
    contract_id = str(contract_id or "").strip()
    artifact_sha = str(artifact_sha or "").strip().lower()
    direction = str(direction or "").strip().upper()
    symbol_universe = sorted({str(symbol).strip().upper()
                              for symbol in symbol_universe if str(symbol).strip()})
    resolver_id = str(resolver_id or "").strip()
    family_correction = str(family_correction or "").strip()
    source_manifest_sha = str(source_manifest_sha or "").strip()
    feature_manifest_sha = str(feature_manifest_sha or "").strip()
    if not contract_id:
        raise ValueError("contract_id is required")
    if len(artifact_sha) != 64 or any(
        character not in "0123456789abcdef" for character in artifact_sha
    ):
        raise ValueError("artifact_sha must be a 64-character hexadecimal identity")
    if direction not in ("UP", "DOWN"):
        raise ValueError("direction must be UP or DOWN")
    if not symbol_universe:
        raise ValueError("symbol_universe must not be empty")
    if not resolver_id:
        raise ValueError("resolver_id is required")
    if isinstance(family_size, bool) or int(family_size) < 1:
        raise ValueError("family_size must be >= 1")
    family_size = int(family_size)
    threshold = float(threshold)
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be finite and in [0,1]")
    if round_trip_slippage_bps is None or round_trip_commission_bps is None:
        raise ValueError("round-trip slippage and commission must be declared")
    slippage_bps = float(round_trip_slippage_bps)
    commission_bps = float(round_trip_commission_bps)
    if not 0.0 <= slippage_bps <= 1000.0:
        raise ValueError("round_trip_slippage_bps must be in [0,1000]")
    if not 0.0 <= commission_bps <= 1000.0:
        raise ValueError("round_trip_commission_bps must be in [0,1000]")
    if slippage_bps + commission_bps > 1000.0:
        raise ValueError("total round-trip cost must be <= 1000 bps")
    registration_id = f"fwd_{uuid.uuid4().hex[:12]}"
    now = int(time.time())

    if cur is not None:
        return _register_impl(
            cur, registration_id, contract_id, artifact_sha, direction,
            threshold, symbol_universe, slice_spec, source_manifest_sha,
            feature_manifest_sha, resolver_id, family_size, family_correction,
            selection_evidence, slippage_bps, commission_bps, now,
        )
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        result = _register_impl(
            c, registration_id, contract_id, artifact_sha, direction,
            threshold, symbol_universe, slice_spec, source_manifest_sha,
            feature_manifest_sha, resolver_id, family_size, family_correction,
            selection_evidence, slippage_bps, commission_bps, now,
        )
        conn.commit()
        return result


def _register_impl(
    cur, registration_id, contract_id, artifact_sha, direction,
    threshold, symbol_universe, slice_spec, source_manifest_sha,
    feature_manifest_sha, resolver_id, family_size, family_correction,
    selection_evidence, slippage_bps, commission_bps, now,
) -> str:
    from core.research_artifacts import artifact_integrity_error, get_artifact

    artifact = get_artifact(artifact_sha, cur=cur)
    if not artifact:
        raise ValueError(f"artifact_not_found:{artifact_sha}")
    integrity_error = artifact_integrity_error(artifact)
    if integrity_error:
        raise ValueError(f"artifact_integrity_failed:{integrity_error}")
    if artifact.get("status") != "ACTIVE":
        raise ValueError(f"artifact_not_active:{artifact.get('status')}")
    if artifact.get("contract_id") != contract_id:
        raise ValueError("registration_contract_mismatch")
    output_domain = {
        str(output).upper() for output in (artifact.get("output_domain") or ())
    }
    if output_domain != {direction}:
        raise ValueError("registration_direction_mismatch")
    calibration_proof = artifact.get("calibration_proof")
    if not isinstance(calibration_proof, dict):
        raise ValueError("artifact_threshold_missing")
    try:
        artifact_threshold = float(calibration_proof["threshold"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("artifact_threshold_missing") from exc
    if not math.isfinite(artifact_threshold) or not math.isclose(
        threshold, artifact_threshold, rel_tol=0.0, abs_tol=1e-12,
    ):
        raise ValueError("registration_threshold_mismatch")
    artifact_scope = {
        str(symbol).upper() for symbol in (artifact.get("symbol_scope") or ())
    }
    if artifact_scope != {"__UNIVERSE__"} and artifact_scope != set(symbol_universe):
        raise ValueError("registration_symbol_scope_mismatch")

    metadata = {
        "registration_schema": "fixed50/v2",
        "coverage_schema": "eligible_evaluation/v1",
        "cost_model": {
            "schema": "round_trip_bps/v1",
            "slippage_bps": slippage_bps,
            "commission_bps": commission_bps,
            "total_bps": slippage_bps + commission_bps,
        },
    }
    expected = {
        "threshold": threshold,
        "output_rule": "threshold_gate",
        "symbol_universe": symbol_universe,
        "slice_spec": slice_spec,
        "source_manifest_sha": source_manifest_sha,
        "feature_manifest_sha": feature_manifest_sha,
        "resolver_id": resolver_id,
        "confirmatory_n": V2_CONFIRMATORY_N,
        "max_calendar_days": V2_MAX_CALENDAR_DAYS,
        "min_issuance_dates": V2_MIN_ISSUANCE_DATES,
        "max_symbol_concentration": V2_MAX_SYMBOL_CONCENTRATION,
        "family_size": family_size,
        "family_correction": family_correction,
        "selection_evidence": selection_evidence,
        "metadata": metadata,
    }
    lock_digest = hashlib.sha256(
        f"{contract_id}:{artifact_sha}:{direction}".encode("ascii")
    ).hexdigest()
    lock_key = int(lock_digest[:16], 16) & 0x7FFFFFFFFFFFFFFF
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
    cur.execute(
        """
        SELECT registration_id, threshold, output_rule, symbol_universe,
               slice_spec, source_manifest_sha, feature_manifest_sha,
               resolver_id, confirmatory_n, max_calendar_days,
               min_issuance_dates, max_symbol_concentration, family_size,
               family_correction, selection_evidence, metadata
        FROM ghost_research_registrations
        WHERE contract_id = %s AND artifact_sha = %s AND direction = %s
        ORDER BY id ASC
        LIMIT 1
        """,
        (contract_id, artifact_sha, direction),
    )
    existing = cur.fetchone()
    if existing:
        persisted = {
            "threshold": float(existing[1]),
            "output_rule": str(existing[2] or ""),
            "symbol_universe": sorted(existing[3] or []),
            "slice_spec": _json_object(existing[4]) if existing[4] is not None else None,
            "source_manifest_sha": str(existing[5] or ""),
            "feature_manifest_sha": str(existing[6] or ""),
            "resolver_id": str(existing[7] or ""),
            "confirmatory_n": int(existing[8]),
            "max_calendar_days": int(existing[9]),
            "min_issuance_dates": int(existing[10]),
            "max_symbol_concentration": float(existing[11]),
            "family_size": int(existing[12] or 1),
            "family_correction": str(existing[13] or ""),
            "selection_evidence": (
                _json_object(existing[14]) if existing[14] is not None else None
            ),
            "metadata": _json_object(existing[15]),
        }
        mismatches = [key for key, value in expected.items()
                      if persisted.get(key) != value]
        if mismatches:
            raise ValueError(
                "artifact_already_registered_with_different_parameters:"
                + ",".join(mismatches)
            )
        return str(existing[0])

    cur.execute(
        """
        INSERT INTO ghost_research_registrations
            (registration_id, contract_id, artifact_sha, direction,
             threshold, output_rule, symbol_universe, slice_spec,
             source_manifest_sha, feature_manifest_sha, resolver_id,
             confirmatory_n, max_calendar_days, min_issuance_dates,
             max_symbol_concentration, family_size, family_correction,
             selection_evidence, status, registered_at_ts, metadata)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            registration_id, contract_id, artifact_sha, direction,
            threshold, "threshold_gate", symbol_universe,
            json.dumps(slice_spec) if slice_spec else None,
            source_manifest_sha, feature_manifest_sha, resolver_id,
            V2_CONFIRMATORY_N, V2_MAX_CALENDAR_DAYS, V2_MIN_ISSUANCE_DATES,
            V2_MAX_SYMBOL_CONCENTRATION, family_size, family_correction,
            json.dumps(selection_evidence) if selection_evidence else None,
            "COLLECTING", now,
            json.dumps(metadata, sort_keys=True, separators=(",", ":")),
        ),
    )
    if cur.rowcount != 1:
        raise RuntimeError("forward_registration_insert_failed")
    return registration_id


def evaluate_forward_proof(
    registration_id: str,
    cur=None,
) -> Dict[str, Any]:
    """Evaluate the current state of a forward experiment.

    Counts only predictions issued strictly after registration that match
    the exact artifact, contract, direction, and threshold. Enforces one
    observation per artifact/symbol/trading date.
    """
    if cur is not None:
        return _evaluate_impl(cur, registration_id)
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        return _evaluate_impl(c, registration_id)


def update_forward_proof_status(
    registration_id: str,
    cur=None,
) -> Dict[str, Any]:
    """Evaluate and persist one registration transition explicitly."""
    if cur is not None:
        return _update_status_impl(cur, registration_id)
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        proof = _update_status_impl(c, registration_id)
        conn.commit()
        return proof


def _update_status_impl(cur, registration_id: str) -> Dict[str, Any]:
    proof = _evaluate_impl(cur, registration_id)
    if not proof.get("ok"):
        return proof
    status = str(proof["status"])
    terminal = status in _TERMINAL_STATUSES
    transition_ts = int(time.time())
    cur.execute(
        """
        UPDATE ghost_research_registrations
        SET status = %s,
            closed_at_ts = CASE
                WHEN %s THEN COALESCE(closed_at_ts, %s)
                ELSE closed_at_ts
            END
        WHERE registration_id = %s
        """,
        (status, terminal, transition_ts, registration_id),
    )
    if cur.rowcount != 1:
        raise RuntimeError(f"Failed to update registration {registration_id}")
    proof["persisted_status"] = status
    if terminal:
        proof["closed_at_ts"] = proof.get("closed_at_ts") or transition_ts
    return proof


def _json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _proof_date_key(issued_ts: int, evaluation_date: Any) -> str:
    """Prefer the frozen Central session date; legacy rows fall back to UTC."""
    candidate = str(evaluation_date or "")
    try:
        time.strptime(candidate, "%Y-%m-%d")
        return candidate
    except (TypeError, ValueError):
        return time.strftime("%Y-%m-%d", time.gmtime(issued_ts))


def _declared_cost_fraction(metadata: Dict[str, Any]) -> Optional[float]:
    cost_model = metadata.get("cost_model")
    if not isinstance(cost_model, dict):
        return None
    if cost_model.get("schema") != "round_trip_bps/v1":
        return None
    try:
        slippage = float(cost_model["slippage_bps"])
        commission = float(cost_model["commission_bps"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    total = slippage + commission
    if not all(math.isfinite(value) and value >= 0.0
               for value in (slippage, commission)):
        return None
    if total > 1000.0:
        return None
    return total / 10000.0


def _trade_net_return(
    row: Dict[str, Any],
    cost_fraction: float,
) -> Optional[float]:
    context = _json_object(row.get("context"))
    try:
        entry = float(context["entry_price"])
        exit_price = float(row["observed_value"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(value) and value > 0.0 for value in (entry, exit_price)):
        return None
    direction = str(row.get("direction") or "").upper()
    if direction == "UP":
        gross_return = (exit_price - entry) / entry
    elif direction == "DOWN":
        gross_return = (entry - exit_price) / entry
    else:
        return None
    return gross_return - cost_fraction


def _metric_gate(
    *,
    passed: bool,
    value: Any,
    threshold: Any,
    reason: str = "",
    **details: Any,
) -> Dict[str, Any]:
    gate = {"passed": passed, "value": value, "threshold": threshold}
    if reason:
        gate["reason"] = reason
    gate.update(details)
    return gate


def _secondary_metric_gates(
    actionable_rows: List[Dict[str, Any]],
    *,
    data_invalid: int,
    eligible_evaluations: int,
    fired_evaluations: int,
    registration_metadata: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    """Compute protocol secondary metrics from one frozen proof window."""
    n = len(actionable_rows)
    total_predictions = n + data_invalid

    invalid_rate = (data_invalid / total_predictions
                    if total_predictions > 0 else None)
    invalid_pass = invalid_rate is not None and invalid_rate <= _MAX_INVALID_RATE
    gates: Dict[str, Dict[str, Any]] = {
        "invalid_rate": _metric_gate(
            passed=invalid_pass,
            value=round(invalid_rate, 6) if invalid_rate is not None else None,
            threshold=_MAX_INVALID_RATE,
            reason="" if invalid_rate is not None else "no_predictions",
            data_invalid=data_invalid,
            total_predictions=total_predictions,
        ),
    }

    coverage_schema_ok = (
        registration_metadata.get("coverage_schema") == "eligible_evaluation/v1"
    )
    coverage = (fired_evaluations / eligible_evaluations
                if eligible_evaluations > 0 else None)
    coverage_pass = (
        coverage_schema_ok
        and coverage is not None
        and coverage >= _MIN_COVERAGE
    )
    coverage_reason = ""
    if not coverage_schema_ok:
        coverage_reason = "coverage_schema_not_registered"
    elif coverage is None:
        coverage_reason = "no_eligible_evaluations"
    gates["coverage"] = _metric_gate(
        passed=coverage_pass,
        value=round(coverage, 6) if coverage is not None else None,
        threshold=_MIN_COVERAGE,
        reason=coverage_reason,
        fired_evaluations=fired_evaluations,
        eligible_evaluations=eligible_evaluations,
    )

    probability_rows: List[tuple[float, int]] = []
    for row in actionable_rows:
        try:
            probability = float(row["calibrated_prob"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            continue
        probability_rows.append((probability, 1 if row["outcome"] == "WIN" else 0))

    complete_probabilities = n > 0 and len(probability_rows) == n
    brier = (sum((probability - actual) ** 2
                 for probability, actual in probability_rows) / n
             if complete_probabilities else None)
    gates["brier_score"] = _metric_gate(
        passed=brier is not None and brier <= _MAX_BRIER,
        value=round(brier, 6) if brier is not None else None,
        threshold=_MAX_BRIER,
        reason="" if complete_probabilities else "missing_or_invalid_probability",
        scored=n,
    )

    calibration_bins: List[Dict[str, Any]] = []
    calibration_gaps: List[float] = []
    if complete_probabilities:
        for label, lower, upper in _CALIBRATION_BINS:
            members = [(probability, actual)
                       for probability, actual in probability_rows
                       if lower <= probability < upper]
            if not members:
                continue
            mean_probability = sum(probability for probability, _ in members) / len(members)
            observed_rate = sum(actual for _, actual in members) / len(members)
            absolute_gap = abs(mean_probability - observed_rate)
            calibration_gaps.append(absolute_gap)
            calibration_bins.append({
                "label": label,
                "n": len(members),
                "mean_probability": round(mean_probability, 6),
                "observed_rate": round(observed_rate, 6),
                "absolute_gap": round(absolute_gap, 6),
            })
    max_gap = max(calibration_gaps) if calibration_gaps else None
    gates["calibration_gap"] = _metric_gate(
        passed=max_gap is not None and max_gap <= _MAX_CALIBRATION_GAP,
        value=round(max_gap, 6) if max_gap is not None else None,
        threshold=_MAX_CALIBRATION_GAP,
        reason="" if calibration_bins else "no_complete_calibration_bins",
        bins=calibration_bins,
    )

    cost_fraction = _declared_cost_fraction(registration_metadata)
    net_returns: List[float] = []
    if cost_fraction is not None:
        for row in actionable_rows:
            net_return = _trade_net_return(row, cost_fraction)
            if net_return is None or not math.isfinite(net_return):
                net_returns = []
                break
            net_returns.append(net_return)
    complete_returns = n > 0 and len(net_returns) == n
    return_reason = ""
    if cost_fraction is None:
        return_reason = "cost_model_not_registered"
    elif not complete_returns:
        return_reason = "missing_or_invalid_frozen_exit"

    expectancy = sum(net_returns) / n if complete_returns else None
    gates["net_expectancy"] = _metric_gate(
        passed=expectancy is not None and expectancy > 0.0,
        value=round(expectancy, 6) if expectancy is not None else None,
        threshold="> 0",
        reason=return_reason,
        cost_fraction=cost_fraction,
    )

    gross_profit = (sum(value for value in net_returns if value > 0.0)
                    if complete_returns else None)
    gross_loss = (sum(-value for value in net_returns if value < 0.0)
                  if complete_returns else None)
    profit_factor: Any = None
    if gross_profit is None or gross_loss is None:
        profit_factor_pass = False
    elif gross_loss == 0.0:
        profit_factor = "Infinity" if gross_profit > 0.0 else None
        profit_factor_pass = gross_profit > 0.0
    else:
        profit_factor = gross_profit / gross_loss
        profit_factor_pass = profit_factor > 1.0
    gates["profit_factor"] = _metric_gate(
        passed=profit_factor_pass,
        value=(round(profit_factor, 6)
               if isinstance(profit_factor, float) else profit_factor),
        threshold="> 1.0",
        reason=return_reason,
        gross_profit=round(gross_profit, 6) if gross_profit is not None else None,
        gross_loss=round(gross_loss, 6) if gross_loss is not None else None,
    )

    max_drawdown = None
    if complete_returns:
        equity = 1.0
        peak = 1.0
        max_drawdown = 0.0
        for net_return in net_returns:
            equity = max(0.0, equity * (1.0 + net_return))
            peak = max(peak, equity)
            drawdown = (peak - equity) / peak if peak > 0.0 else 1.0
            max_drawdown = max(max_drawdown, drawdown)
    gates["max_drawdown"] = _metric_gate(
        passed=max_drawdown is not None and max_drawdown <= _MAX_DRAWDOWN,
        value=round(max_drawdown, 6) if max_drawdown is not None else None,
        threshold=_MAX_DRAWDOWN,
        reason=return_reason,
    )

    outcomes = [1 if row["outcome"] == "WIN" else 0 for row in actionable_rows]
    bootstrap_low = (moving_block_bootstrap_lower_bound(
        outcomes,
        n_bootstrap=_BOOTSTRAP_SAMPLES,
        block_size=_BOOTSTRAP_BLOCK_SIZE,
    ) if len(outcomes) >= _BOOTSTRAP_BLOCK_SIZE else None)
    bootstrap_complete = n == V2_CONFIRMATORY_N
    gates["block_bootstrap"] = _metric_gate(
        passed=(bootstrap_complete and bootstrap_low is not None
                and bootstrap_low >= V2_TARGET),
        value=round(bootstrap_low, 6) if bootstrap_low is not None else None,
        threshold=V2_TARGET,
        reason="" if bootstrap_complete else "confirmatory_sample_incomplete",
        block_size=_BOOTSTRAP_BLOCK_SIZE,
        samples=_BOOTSTRAP_SAMPLES,
    )
    return gates


def _evaluate_impl(cur, registration_id) -> Dict[str, Any]:
    # Load registration
    cur.execute(
        """
        SELECT contract_id, artifact_sha, direction, threshold,
               symbol_universe, confirmatory_n, max_calendar_days,
               min_issuance_dates, max_symbol_concentration,
             registered_at_ts, status, closed_at_ts, metadata
        FROM ghost_research_registrations
        WHERE registration_id = %s
        """,
        (registration_id,),
    )
    reg = cur.fetchone()
    if not reg:
        return {"ok": False, "error": f"Registration {registration_id} not found"}

    contract_id = reg[0]
    artifact_sha = reg[1]
    direction = reg[2]
    threshold = float(reg[3])
    symbol_universe = reg[4] or []
    confirmatory_n = int(reg[5])
    max_calendar_days = int(reg[6])
    min_issuance_dates = int(reg[7])
    max_symbol_concentration = float(reg[8])
    registered_at_ts = int(reg[9])
    persisted_status = str(reg[10])
    closed_at_ts = int(reg[11]) if reg[11] is not None else None
    registration_metadata = _json_object(reg[12])
    from core.research_artifacts import artifact_integrity_error, get_artifact
    artifact = get_artifact(artifact_sha, cur=cur)
    integrity_error = artifact_integrity_error(artifact)
    if integrity_error:
        return {
            "ok": False,
            "error": f"artifact_integrity_failed:{integrity_error}",
            "registration_id": registration_id,
            "artifact_sha": artifact_sha,
        }
    if confirmatory_n != V2_CONFIRMATORY_N:
        return {
            "ok": False,
            "error": f"confirmatory_n must be {V2_CONFIRMATORY_N}, got {confirmatory_n}",
        }
    if persisted_status in _TERMINAL_STATUSES and closed_at_ts is None:
        return {
            "ok": False,
            "error": "terminal_registration_missing_closed_at_ts",
            "registration_id": registration_id,
        }

    evaluation_now = int(time.time())
    evidence_window_end = closed_at_ts or evaluation_now

    # Count forward outcomes: predictions issued after registration,
    # matching exact artifact/contract/direction/threshold, with resolutions.
    # Filter by symbol_universe if the registration specifies one.
    universe_filter = ""
    universe_params: List[Any] = []
    if symbol_universe and len(symbol_universe) > 0:
        universe_filter = " AND p.symbol = ANY(%s)"
        universe_params = [symbol_universe]

    cur.execute(
        f"""
         SELECT p.id, p.symbol, p.issued_ts, p.calibrated_prob, p.context,
             p.direction, r.outcome, r.observed_value, e.evaluation_date
        FROM ghost_research_predictions p
         LEFT JOIN ghost_research_resolutions r ON r.prediction_id = p.id
         LEFT JOIN ghost_research_evaluations e
           ON e.contract_id = p.contract_id
          AND e.artifact_sha = p.artifact_sha
          AND e.symbol = p.symbol
          AND e.direction = p.direction
          AND e.evaluated_ts = p.issued_ts
        WHERE p.contract_id = %s
          AND p.artifact_sha = %s
          AND p.direction = %s
          AND p.issued_ts > %s
          AND p.issued_ts <= %s
          AND p.threshold = %s
                    {universe_filter}
                ORDER BY p.issued_ts ASC, p.id ASC
        """,
        (
            contract_id, artifact_sha, direction, registered_at_ts,
            evidence_window_end, threshold, *universe_params,
        ),
    )
    rows = cur.fetchall()

    # Deduplicate in issuance order. An unresolved earlier row blocks later
    # rows so resolution speed cannot select a favorable confirmatory sample.
    from collections import defaultdict
    seen: Dict[str, set] = defaultdict(set)
    actionable_rows: List[Dict[str, Any]] = []
    data_invalid = 0
    blocked_prediction_id = None
    freeze_ts = None
    for (prediction_id, sym, issued_ts, probability, context, row_direction,
         outcome, observed_value, evaluation_date) in rows:
        date_key = _proof_date_key(int(issued_ts), evaluation_date)
        if date_key in seen[sym]:
            continue
        seen[sym].add(date_key)
        if outcome is None:
            blocked_prediction_id = int(prediction_id)
            break
        if outcome == "DATA_INVALID":
            data_invalid += 1
            continue
        if outcome not in ("WIN", "LOSS", "EXPIRED"):
            blocked_prediction_id = int(prediction_id)
            break
        actionable_rows.append({
            "prediction_id": int(prediction_id),
            "symbol": str(sym),
            "issued_ts": int(issued_ts),
            "evaluation_date": date_key,
            "calibrated_prob": probability,
            "context": context,
            "direction": row_direction,
            "outcome": outcome,
            "observed_value": observed_value,
        })
        if len(actionable_rows) >= confirmatory_n:
            freeze_ts = int(issued_ts)
            break

    n = len(actionable_rows)
    wins = sum(1 for row in actionable_rows if row["outcome"] == "WIN")
    losses = sum(1 for row in actionable_rows if row["outcome"] == "LOSS")
    expired = sum(1 for row in actionable_rows if row["outcome"] == "EXPIRED")

    # Distinct dates
    dates = set()
    for row in actionable_rows:
        dates.add(row["evaluation_date"])

    # Symbol concentration
    sym_counts: Dict[str, int] = {}
    for row in actionable_rows:
        sym = row["symbol"]
        sym_counts[sym] = sym_counts.get(sym, 0) + 1
    max_conc = max(sym_counts.values()) / n if n > 0 else 0.0

    # Status
    status = v2_confirmatory_status(wins, n)
    if status == "COLLECTING":
        # Check calendar deadline
        elapsed = evaluation_now - registered_at_ts
        if elapsed > max_calendar_days * 86400:
            status = "INCOMPLETE"
    if persisted_status in _TERMINAL_STATUSES:
        status = persisted_status

    # Wilson display
    wilson = exact_wilson_display(wins, n)

    coverage_cutoff = freeze_ts or evidence_window_end
    coverage_universe_filter = (
        " AND symbol = ANY(%s)" if symbol_universe else ""
    )
    cur.execute(
        f"""
        SELECT COUNT(*) FILTER (WHERE eligible),
               COUNT(*) FILTER (WHERE eligible AND fired)
        FROM ghost_research_evaluations
        WHERE contract_id = %s
          AND artifact_sha = %s
          AND direction = %s
          AND evaluated_ts > %s
          AND evaluated_ts <= %s
          AND threshold = %s{coverage_universe_filter}
        """,
        (
            contract_id, artifact_sha, direction, registered_at_ts,
            coverage_cutoff, threshold, *universe_params,
        ),
    )
    coverage_row = cur.fetchone() or (0, 0)
    eligible_evaluations = int(coverage_row[0] or 0)
    fired_evaluations = int(coverage_row[1] or 0)

    # ── Secondary gates ──────────────────────────────────────────────────
    secondary_gates: Dict[str, Dict[str, Any]] = {}

    # Diversity: distinct dates
    dates_ok = len(dates) >= min_issuance_dates
    secondary_gates["diversity"] = {
        "passed": dates_ok,
        "value": len(dates),
        "threshold": min_issuance_dates,
    }

    # Concentration: no single symbol > max_symbol_concentration.
    # Per-symbol artifacts (single-symbol universe) are exempt.
    is_per_symbol = len(symbol_universe) == 1 if symbol_universe else False
    conc_ok = True if (is_per_symbol or n == 0) else max_conc <= max_symbol_concentration
    secondary_gates["concentration"] = {
        "passed": conc_ok,
        "value": round(max_conc, 4),
        "threshold": max_symbol_concentration,
        "exempt": is_per_symbol,
    }
    secondary_gates.update(_secondary_metric_gates(
        actionable_rows,
        data_invalid=data_invalid,
        eligible_evaluations=eligible_evaluations,
        fired_evaluations=fired_evaluations,
        registration_metadata=registration_metadata,
    ))

    # Only declare PROVEN when all secondary gates pass
    all_secondary_pass = all(g["passed"] for g in secondary_gates.values())
    if status == "PROVEN" and not all_secondary_pass:
        status = "FALSIFIED"

    return {
        "ok": True,
        "registration_id": registration_id,
        "contract_id": contract_id,
        "artifact_sha": artifact_sha,
        "direction": direction,
        "threshold": threshold,
        "registered_at_ts": registered_at_ts,
        "n": n,
        "wins": wins,
        "losses": losses,
        "expired": expired,
        "data_invalid": data_invalid,
        "total_predictions": n + data_invalid,
        "win_rate": round(wins / n, 4) if n > 0 else None,
        "wilson": wilson,
        "distinct_dates": len(dates),
        "min_dates_required": min_issuance_dates,
        "max_symbol_concentration": round(max_conc, 4),
        "concentration_limit": max_symbol_concentration,
        "secondary_gates": secondary_gates,
        "all_secondary_pass": all_secondary_pass,
        "status": status,
        "persisted_status": persisted_status,
        "closed_at_ts": closed_at_ts,
        "blocked_prediction_id": blocked_prediction_id,
        "freeze_ts": freeze_ts,
        "target_n": confirmatory_n,
        "target_wins": V2_MIN_WINS,
        "remaining": confirmatory_n - n,
        "note": (
            "Fixed 50-outcome confirmatory test. No early success. "
            "Status PROVEN requires exactly 50 outcomes with >=42 wins "
            "and all secondary gates."
        ),
    }


def get_active_registrations(status: str = "COLLECTING", cur=None) -> List[Dict[str, Any]]:
    """List registrations by status."""
    if cur is not None:
        return _list_regs_impl(cur, status)
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        return _list_regs_impl(c, status)


def _list_regs_impl(cur, status) -> List[Dict[str, Any]]:
    where = "WHERE status = %s" if status is not None else ""
    params = (status,) if status is not None else ()
    cur.execute(
        f"""
        SELECT registration_id, contract_id, artifact_sha, direction,
               threshold, registered_at_ts, status, confirmatory_n
        FROM ghost_research_registrations
        {where}
        ORDER BY registered_at_ts DESC
        LIMIT 50
        """,
        params,
    )
    return [
        {
            "registration_id": r[0],
            "contract_id": r[1],
            "artifact_sha": r[2],
            "direction": r[3],
            "threshold": r[4],
            "registered_at_ts": r[5],
            "status": r[6],
            "confirmatory_n": r[7],
        }
        for r in cur.fetchall()
    ]
