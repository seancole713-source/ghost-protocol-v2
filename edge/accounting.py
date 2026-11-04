"""Cash accounting from unique broker orders, separate from experiment eligibility."""

from __future__ import annotations

import math


def _number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def paper_summary(rows):
    rows = list(rows)
    entry_fills, exit_fills, conflicts = {}, {}, set()
    for row in rows:
        entry = row.get("broker_entry") or {}
        identity = entry.get("id") or entry.get("client_order_id")
        if not identity:
            continue
        fill = (_number(entry.get("filled_qty")), _number(entry.get("filled_avg_price")))
        if identity in entry_fills and entry_fills[identity] != fill:
            conflicts.add(identity)
        entry_fills[identity] = fill
        for order in row.get("broker_exits") or []:
            oid = order.get("id") or order.get("client_order_id")
            if not oid:
                continue
            key = (identity, oid)
            fill = (_number(order.get("filled_qty")), _number(order.get("filled_avg_price")))
            if key in exit_fills and exit_fills[key] != fill:
                conflicts.add(identity)
            exit_fills[key] = fill
    seen, completed, issues = set(), [], []
    shared = unfilled = unsettled = duplicates = missing_entries = 0
    for row in rows:
        entry = row.get("broker_entry") or {}
        if not entry:
            if row.get("shared_order_source"):
                shared += 1
            elif (
                row.get("paper_state") == "submitted"
                or row.get("actual") in {"WIN", "LOSS"}
                or row.get("broker_exits")
                or (row.get("actual_pnl_usd") is not None and row.get("actual") != "NO_FILL")
            ):
                # Forecasts without an order are normal. An observed order or
                # financial result without its entry snapshot cannot reconcile.
                missing_entries += 1
                unsettled += 1
                identity = row.get("forecast_id") or "unknown forecast"
                issues.append(f"{identity}: broker entry evidence missing")
            continue
        identity = entry.get("id") or entry.get("client_order_id")
        if not identity:
            unsettled += 1
            issues.append(f"{row.get('forecast_id') or 'unknown forecast'}: entry lacks broker order identity")
            continue
        if identity in seen:
            duplicates += 1
            continue
        seen.add(identity)
        if identity in conflicts:
            unsettled += 1
            issues.append(f"{identity}: conflicting broker fill snapshots")
            continue
        qty, price = (
            _number(entry.get("filled_qty")),
            _number(entry.get("filled_avg_price")),
        )
        if qty is not None and qty == 0:
            unfilled += 1
            continue
        if qty is None or qty < 0 or price is None or price <= 0:
            unsettled += 1
            issues.append(f"{identity}: incomplete entry evidence")
            continue
        proceeds, sold, exit_ids, invalid = 0.0, 0.0, set(), False
        for order in row.get("broker_exits") or []:
            n = _number(order.get("filled_qty"))
            if n == 0:
                continue
            oid = order.get("id") or order.get("client_order_id")
            if not oid or n is None or n < 0:
                invalid = True
                continue
            if oid in exit_ids:
                continue
            exit_ids.add(oid)
            p = _number(order.get("filled_avg_price"))
            if p is None or p <= 0:
                invalid = True
                continue
            sold += n
            proceeds += n * p
        if invalid or not math.isclose(qty, sold, abs_tol=1e-6, rel_tol=0):
            unsettled += 1
            issues.append(
                f"{identity}: exit evidence incomplete or quantities do not reconcile"
            )
            continue
        pnl = round(proceeds - qty * price, 2)
        stored = _number(row.get("actual_pnl_usd"))
        if stored is not None and abs(stored - pnl) > 0.02:
            issues.append(f"{identity}: stored P&L disagrees with broker fills")
        completed.append(
            {
                "order_id": identity,
                "symbol": row["symbol"],
                "pnl": pnl,
                "target_hit": row.get("actual") == "WIN",
                "outside_rule": not row.get("actual_counted", False),
            }
        )
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
