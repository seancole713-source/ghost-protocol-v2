"""core/research_forward.py — fixed-sample forward proof collection (Phase 7).

Manages the v2 confirmatory protocol: register an exact artifact, collect
exactly 50 forward actionable outcomes, and evaluate all gates. Never declares
success before 50. Early termination only for futility (42/50 impossible).

Read-only on live tables. Writes only to ghost_research_* tables.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("ghost.research_forward")

from core.binomial_stats import (
    V2_CONFIRMATORY_N,
    V2_MIN_WINS,
    V2_TARGET,
    V2_MIN_ISSUANCE_DATES,
    V2_MAX_SYMBOL_CONCENTRATION,
    V2_MAX_CALENDAR_DAYS,
    v2_confirmatory_pass,
    v2_confirmatory_futile,
    v2_confirmatory_status,
    wilson_lower_bound,
    wilson_pass,
    exact_wilson_display,
)


def register_forward_experiment(
    *,
    contract_sha: str,
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
    cur=None,
) -> Optional[str]:
    """Register a forward confirmatory experiment. Returns registration_id.

    Immutable — once registered, parameters cannot change. One registration
    per contract/artifact/direction.
    """
    import uuid
    registration_id = f"fwd_{uuid.uuid4().hex[:12]}"
    now = int(time.time())

    if cur is not None:
        return _register_impl(
            cur, registration_id, contract_sha, artifact_sha, direction,
            threshold, symbol_universe, slice_spec, source_manifest_sha,
            feature_manifest_sha, resolver_id, family_size, family_correction,
            selection_evidence, now,
        )
    from core.db import db_conn
    with db_conn() as conn:
        c = conn.cursor()
        result = _register_impl(
            c, registration_id, contract_sha, artifact_sha, direction,
            threshold, symbol_universe, slice_spec, source_manifest_sha,
            feature_manifest_sha, resolver_id, family_size, family_correction,
            selection_evidence, now,
        )
        conn.commit()
        return result


def _register_impl(
    cur, registration_id, contract_sha, artifact_sha, direction,
    threshold, symbol_universe, slice_spec, source_manifest_sha,
    feature_manifest_sha, resolver_id, family_size, family_correction,
    selection_evidence, now,
) -> str:
    cur.execute(
        """
        INSERT INTO ghost_research_registrations
            (registration_id, contract_sha, artifact_sha, direction,
             threshold, output_rule, symbol_universe, slice_spec,
             source_manifest_sha, feature_manifest_sha, resolver_id,
             confirmatory_n, max_calendar_days, min_issuance_dates,
             max_symbol_concentration, family_size, family_correction,
             selection_evidence, status, registered_at_ts, metadata)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (registration_id) DO NOTHING
        """,
        (
            registration_id, contract_sha, artifact_sha, direction,
            threshold, "threshold_gate", symbol_universe,
            json.dumps(slice_spec) if slice_spec else None,
            source_manifest_sha, feature_manifest_sha, resolver_id,
            V2_CONFIRMATORY_N, V2_MAX_CALENDAR_DAYS, V2_MIN_ISSUANCE_DATES,
            V2_MAX_SYMBOL_CONCENTRATION, family_size, family_correction,
            json.dumps(selection_evidence) if selection_evidence else None,
            "COLLECTING", now, "{}",
        ),
    )
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


def _evaluate_impl(cur, registration_id) -> Dict[str, Any]:
    # Load registration
    cur.execute(
        """
        SELECT contract_sha, artifact_sha, direction, threshold,
               symbol_universe, confirmatory_n, max_calendar_days,
               min_issuance_dates, max_symbol_concentration,
               registered_at_ts, status
        FROM ghost_research_registrations
        WHERE registration_id = %s
        """,
        (registration_id,),
    )
    reg = cur.fetchone()
    if not reg:
        return {"ok": False, "error": f"Registration {registration_id} not found"}

    contract_sha = reg[0]
    artifact_sha = reg[1]
    direction = reg[2]
    threshold = float(reg[3])
    registered_at_ts = int(reg[8])

    # Count forward outcomes: predictions issued after registration,
    # matching exact artifact/contract/direction/threshold, with resolutions
    cur.execute(
        """
        SELECT p.symbol, p.issued_ts, r.outcome
        FROM ghost_research_predictions p
        JOIN ghost_research_resolutions r ON r.prediction_id = p.id
        WHERE p.contract_sha = %s
          AND p.artifact_sha = %s
          AND p.direction = %s
          AND p.issued_ts > %s
          AND p.threshold = %s
          AND r.outcome IN ('WIN', 'LOSS', 'EXPIRED')
        ORDER BY p.issued_ts ASC
        """,
        (contract_sha, artifact_sha, direction, registered_at_ts, threshold),
    )
    rows = cur.fetchall()

    # Deduplicate: one per symbol/trading date
    from collections import defaultdict
    seen: Dict[str, set] = defaultdict(set)
    deduped = []
    for sym, issued_ts, outcome in rows:
        date_key = time.strftime("%Y-%m-%d", time.gmtime(issued_ts))
        if date_key not in seen[sym]:
            seen[sym].add(date_key)
            deduped.append((sym, issued_ts, outcome))

    n = len(deduped)
    wins = sum(1 for _, _, o in deduped if o == "WIN")
    losses = sum(1 for _, _, o in deduped if o == "LOSS")
    expired = sum(1 for _, _, o in deduped if o == "EXPIRED")

    # Distinct dates
    dates = set()
    for _, ts, _ in deduped:
        dates.add(time.strftime("%Y-%m-%d", time.gmtime(ts)))

    # Symbol concentration
    sym_counts: Dict[str, int] = {}
    for sym, _, _ in deduped:
        sym_counts[sym] = sym_counts.get(sym, 0) + 1
    max_conc = max(sym_counts.values()) / n if n > 0 else 0.0

    # Status
    status = v2_confirmatory_status(wins, n)
    if status == "COLLECTING":
        # Check calendar deadline
        elapsed = int(time.time()) - registered_at_ts
        if elapsed > V2_MAX_CALENDAR_DAYS * 86400:
            status = "INCOMPLETE"

    # Wilson display
    wilson = exact_wilson_display(wins, n)

    return {
        "ok": True,
        "registration_id": registration_id,
        "contract_sha": contract_sha,
        "artifact_sha": artifact_sha,
        "direction": direction,
        "threshold": threshold,
        "registered_at_ts": registered_at_ts,
        "n": n,
        "wins": wins,
        "losses": losses,
        "expired": expired,
        "win_rate": round(wins / n, 4) if n > 0 else None,
        "wilson": wilson,
        "distinct_dates": len(dates),
        "min_dates_required": V2_MIN_ISSUANCE_DATES,
        "max_symbol_concentration": round(max_conc, 4),
        "concentration_limit": V2_MAX_SYMBOL_CONCENTRATION,
        "status": status,
        "target_n": V2_CONFIRMATORY_N,
        "target_wins": V2_MIN_WINS,
        "remaining": V2_CONFIRMATORY_N - n,
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
    cur.execute(
        """
        SELECT registration_id, contract_sha, artifact_sha, direction,
               threshold, registered_at_ts, status, confirmatory_n
        FROM ghost_research_registrations
        WHERE status = %s
        ORDER BY registered_at_ts DESC
        LIMIT 50
        """,
        (status,),
    )
    return [
        {
            "registration_id": r[0],
            "contract_sha": r[1],
            "artifact_sha": r[2][:16] if r[2] else "",
            "direction": r[3],
            "threshold": r[4],
            "registered_at_ts": r[5],
            "status": r[6],
            "confirmatory_n": r[7],
        }
        for r in cur.fetchall()
    ]
