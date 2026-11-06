"""Cash accounting within one account/store snapshot, separate from rule eligibility."""

from __future__ import annotations

import math


def _number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def order_snapshot_groups(orders):
    """Link typed ID aliases only within this caller's account/store snapshot.

    An ID/client-ID pair attests the alias; no process-global identity registry is
    used. Multiple broker IDs linked to one client ID remain a flagged conflict.
    """
    parents = {}

    def root(token):
        parents.setdefault(token, token)
        if parents[token] != token:
            parents[token] = root(parents[token])
        return parents[token]

    tokens = []
    for i, order in enumerate(orders):
        ids = [(kind, str(order[field])) for kind, field in
               (("broker", "id"), ("client", "client_order_id")) if order.get(field)]
        if not ids:
            ids = [("missing", str(i))]
        for token in ids:
            parents[root(token)] = root(ids[0])
        tokens.append(ids)
    grouped = {}
    for i, ids in enumerate(tokens):
        grouped.setdefault(root(ids[0]), []).append(i)
    out = []
    for indices in grouped.values():
        aliases = {token for i in indices for token in tokens[i]}
        broker = {value for kind, value in aliases if kind == "broker"}
        client = {value for kind, value in aliases if kind == "client"}
        identity = min(broker) if broker else min(client) if client else None
        out.append((identity, indices, len(broker) > 1 or len(client) > 1))
    return out


def _fill(order):
    return (_number(order.get("filled_qty")), _number(order.get("filled_avg_price")))


def paper_summary(rows):
    # Readout retains every broker snapshot instead of overwriting same-client
    # records. Expand evidence here while preserving one displayed forecast row.
    expanded = []
    for row in rows:
        snapshots = row.get("broker_snapshots")
        if snapshots:
            expanded.extend(dict(row, broker_entry=s["entry"], broker_exits=s["exits"])
                            for s in snapshots)
        else:
            expanded.append(row)
    eligible, completed, issues = [], [], []
    shared = unfilled = unsettled = duplicates = missing_entries = 0
    for row in expanded:
        entry = row.get("broker_entry") or {}
        if entry:
            eligible.append(row)
        elif row.get("shared_order_source"):
            shared += 1
        elif (row.get("paper_state") == "submitted" or row.get("actual") in {"WIN", "LOSS"}
              or row.get("broker_exits")
              or (row.get("actual_pnl_usd") is not None and row.get("actual") != "NO_FILL")):
            missing_entries += 1
            unsettled += 1
            issues.append(f"{row.get('forecast_id') or 'unknown forecast'}: broker entry evidence missing")
    seen = set()
    entries = [r["broker_entry"] for r in eligible]
    for identity, indices, alias_conflict in order_snapshot_groups(entries):
        group = [eligible[i] for i in indices]
        if identity is None:
            unsettled += 1
            issues.append(f"{group[0].get('forecast_id') or 'unknown forecast'}: entry lacks broker order identity")
            continue
        seen.add(tuple(indices))
        duplicates += len(group) - 1
        fills = {_fill(r["broker_entry"]) for r in group}
        if alias_conflict or len(fills) != 1 or len({r.get("symbol") for r in group}) != 1:
            unsettled += 1
            issues.append(f"{identity}: conflicting broker fill snapshots")
            continue
        qty, price = next(iter(fills))
        if qty == 0:
            unfilled += 1
            continue
        if qty is None or qty < 0 or price is None or price <= 0:
            unsettled += 1
            issues.append(f"{identity}: incomplete entry evidence")
            continue
        exits = [o for r in group for o in r.get("broker_exits") or []]
        canonical, exit_values, invalid = {}, {}, False
        for key, positions, conflict in order_snapshot_groups(exits):
            values = {_fill(exits[i]) for i in positions}
            if conflict or len(values) != 1:
                invalid = True
                continue
            n, p = next(iter(values))
            if n == 0:
                continue
            if key is None or n is None or n < 0 or p is None or p <= 0:
                invalid = True
                continue
            # Group index is local and typed, so broker/client strings cannot
            # collide merely because they contain the same characters.
            token = positions[0]
            exit_values[token] = (n, p)
            for i in positions:
                canonical[i] = token
        offset, evidence_sets = 0, []
        for r in group:
            orders = r.get("broker_exits") or []
            evidence_sets.append(frozenset(canonical[i] for i in range(offset, offset + len(orders))
                                          if i in canonical))
            offset += len(orders)
        # Do not union partial/different exit sets into invented complete fills.
        # Every duplicate snapshot must independently carry the same filled exits.
        if len(set(evidence_sets)) != 1:
            invalid = True
        sold = sum(n for n, _ in exit_values.values())
        proceeds = sum(n * p for n, p in exit_values.values())
        if invalid or not math.isclose(qty, sold, abs_tol=1e-6, rel_tol=0):
            unsettled += 1
            issues.append(f"{identity}: conflicting broker fill snapshots or incomplete exit evidence; quantities may not reconcile")
            continue
        pnl = round(proceeds - qty * price, 2)
        for r in group:
            stored = _number(r.get("actual_pnl_usd"))
            if stored is not None and abs(stored - pnl) > 0.02:
                issues.append(f"{identity}: stored P&L disagrees with broker fills")
                break
        completed.append({
            "order_id": identity, "symbol": group[0]["symbol"], "pnl": pnl,
            "target_hit": all(r.get("actual") == "WIN" for r in group),
            "outside_rule": any(not r.get("actual_counted", False) for r in group),
        })
    n = len(completed)
    return {
        "basis": "unique_stored_broker_fills_before_fees_and_operating_costs",
        "broker_orders": len(seen),
        "completed_orders": n,
        "unfilled_orders": unfilled,
        "unreconciled_orders": unsettled,
        "missing_entry_orders": missing_entries,
        "shared_results_excluded": shared,
        "duplicate_order_rows_excluded": duplicates,
        "profitable_orders": sum(r["pnl"] > 0 for r in completed),
        "losing_orders": sum(r["pnl"] < 0 for r in completed),
        "profitable_rate": sum(r["pnl"] > 0 for r in completed) / n if n else None,
        "target_hits": sum(r["target_hit"] for r in completed),
        "gross_pnl_usd": round(sum(r["pnl"] for r in completed), 2),
        "outside_rule_completed": sum(r["outside_rule"] for r in completed),
        "outside_rule_pnl_usd": round(
            sum(r["pnl"] for r in completed if r["outside_rule"]), 2
        ),
        "issues": issues,
        "independent_predictions": False,
        "note": "All reconciled broker P&L included, even outside-rule fills. Correlated strategy orders "
        "are financial trades, not independent accuracy samples. Not a fresh broker statement.",
    }
