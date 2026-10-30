"""Atomic admission reservations, not a provider-enforced billing cap.

Interrupted tasks retain their reservation for the day: releasing an uncertain
in-flight charge could fund another call with money already spent upstream.
"""

from __future__ import annotations

import math
import os
import uuid

TABLE = "edge_research_budget"
CALL_HEADROOM_USD = 0.75


def reserve(store, day, cap):
    try:
        amount = float(os.getenv("EDGE_RESEARCH_TASK_RESERVE_USD", "1.5"))
    except ValueError:
        amount = 1.5
    if not math.isfinite(amount) or amount < CALL_HEADROOM_USD:
        amount = 1.5
    token = uuid.uuid4().hex

    def change(row):
        row = dict(row or {"spent_usd": 0.0})
        holds = dict(row.get("reservations") or {})
        spent = float(row.get("spent_usd") or 0)
        if (
            math.isfinite(spent)
            and math.isfinite(cap)
            and spent + sum(holds.values()) + amount <= cap
        ):
            holds[token] = amount
        row.update(
            reservations=holds,
            admission_cap_usd=cap,
            policy="reserved_estimate_not_provider_billing_limit",
        )
        return row

    row = store.mutate(TABLE, day, change)
    return (token, amount) if token in row["reservations"] else (None, 0.0)


def release(store, day, token):
    def change(row):
        row = dict(row or {})
        holds = dict(row.get("reservations") or {})
        holds.pop(token, None)
        row["reservations"] = holds
        return row

    store.mutate(TABLE, day, change)


def charge(store, day, usd):
    if not math.isfinite(usd) or usd < 0:
        raise ValueError("invalid research charge")

    def change(row):
        row = dict(row or {"spent_usd": 0.0})
        row["spent_usd"] = round(float(row.get("spent_usd") or 0) + usd, 6)
        return row

    store.mutate(TABLE, day, change)
