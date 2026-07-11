"""
core/studios.py — Studio booking management
============================================
Data models and business logic for Pink Studio, Retro Studio, Red Studio.
Reconciliation invariants enforced at write time:
  S1: HRS × RATE = GROSS
  S2: GROSS − FEES = NET
  S3: Full-month ≥ MTD
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

LOGGER = logging.getLogger("ghost.studios")

STUDIO_NAMES = ["Pink Studio", "Retro Studio", "Red Studio"]
STUDIO_RATES = {"Pink Studio": 100.0, "Retro Studio": 100.0, "Red Studio": 100.0}

# ── Database ──────────────────────────────────────────────────────────

def ensure_studios_tables(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS studios (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            rate REAL NOT NULL DEFAULT 100.0,
            created_at BIGINT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS studio_bookings (
            id SERIAL PRIMARY KEY,
            studio_id INTEGER NOT NULL REFERENCES studios(id),
            booking_date TEXT NOT NULL,
            hours REAL NOT NULL,
            rate REAL NOT NULL,
            gross REAL NOT NULL,
            fees REAL NOT NULL DEFAULT 0,
            net REAL NOT NULL,
            source TEXT NOT NULL DEFAULT 'direct',
            status TEXT NOT NULL DEFAULT 'expected',
            guest_name TEXT,
            guest_email TEXT,
            notes TEXT,
            created_at BIGINT NOT NULL,
            updated_at BIGINT NOT NULL
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_studio_bookings_date
        ON studio_bookings (studio_id, booking_date DESC)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_studio_bookings_status
        ON studio_bookings (studio_id, status)
    """)


def seed_studios(cur) -> None:
    """Idempotent seed — inserts studios if they don't exist."""
    now = int(datetime.now().timestamp())
    for name in STUDIO_NAMES:
        cur.execute(
            "INSERT INTO studios (name, rate, created_at) VALUES (%s, %s, %s) ON CONFLICT (name) DO NOTHING",
            (name, STUDIO_RATES[name], now),
        )


# ── Reconciliation ────────────────────────────────────────────────────

def reconcile_booking(hours: float, rate: float, fees: float) -> Tuple[float, float]:
    """Return (gross, net) enforcing S1 and S2 invariants."""
    gross = round(hours * rate, 2)
    net = round(gross - fees, 2)
    return gross, net


def validate_booking(hours: float, rate: float, gross: float, fees: float, net: float) -> List[str]:
    """Return list of invariant violations (empty = valid)."""
    issues = []
    expected_gross = round(hours * rate, 2)
    if abs(gross - expected_gross) > 0.02:
        issues.append(f"S1: HRS×RATE={expected_gross} ≠ GROSS={gross}")
    expected_net = round(gross - fees, 2)
    if abs(net - expected_net) > 0.02:
        issues.append(f"S2: GROSS−FEES={expected_net} ≠ NET={net}")
    return issues


# ── Queries ───────────────────────────────────────────────────────────

def _current_month_range() -> Tuple[str, str]:
    """Return (first_of_month, today) as ISO dates."""
    today = date.today()
    first = today.replace(day=1)
    return first.isoformat(), today.isoformat()


def _full_month_range() -> Tuple[str, str]:
    """Return (first_of_month, last_of_month) as ISO dates."""
    today = date.today()
    first = today.replace(day=1)
    if today.month == 12:
        last = today.replace(year=today.year + 1, month=1, day=1) - timedelta(days=1)
    else:
        last = today.replace(month=today.month + 1, day=1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def _prior_month_range() -> Tuple[str, str]:
    """Return (first_of_prior_month, last_of_prior_month)."""
    today = date.today()
    first = today.replace(day=1)
    last_prev = first - timedelta(days=1)
    first_prev = last_prev.replace(day=1)
    return first_prev.isoformat(), last_prev.isoformat()


def get_studio_card(cur, studio_id: int, studio_name: str) -> Dict[str, Any]:
    """Build one studio card with MTD + full-month + prior-month data."""
    mtd_start, mtd_end = _current_month_range()
    full_start, full_end = _full_month_range()
    prior_start, prior_end = _prior_month_range()

    # MTD bookings
    cur.execute(
        """SELECT id, booking_date, hours, rate, gross, fees, net, source, status
           FROM studio_bookings
           WHERE studio_id = %s AND booking_date >= %s AND booking_date <= %s
           ORDER BY booking_date DESC""",
        (studio_id, mtd_start, mtd_end),
    )
    mtd_rows = cur.fetchall()

    # Full-month bookings
    cur.execute(
        """SELECT id, booking_date, hours, rate, gross, fees, net, source, status
           FROM studio_bookings
           WHERE studio_id = %s AND booking_date >= %s AND booking_date <= %s
           ORDER BY booking_date DESC""",
        (studio_id, full_start, full_end),
    )
    full_rows = cur.fetchall()

    # Prior month bookings
    cur.execute(
        """SELECT id, booking_date, hours, rate, gross, fees, net, source, status
           FROM studio_bookings
           WHERE studio_id = %s AND booking_date >= %s AND booking_date <= %s
           ORDER BY booking_date DESC""",
        (studio_id, prior_start, prior_end),
    )
    prior_rows = cur.fetchall()

    # MTD aggregates
    mtd_hours = sum(r[2] for r in mtd_rows)
    mtd_gross = sum(r[4] for r in mtd_rows)
    mtd_fees = sum(r[5] for r in mtd_rows)
    mtd_net = sum(r[6] for r in mtd_rows)
    mtd_bookings = len(mtd_rows)
    mtd_collected = sum(1 for r in mtd_rows if r[8] == "collected")

    # Full-month aggregates
    full_hours = sum(r[2] for r in full_rows)
    full_gross = sum(r[4] for r in full_rows)
    full_fees = sum(r[5] for r in full_rows)
    full_net = sum(r[6] for r in full_rows)
    full_bookings = len(full_rows)
    full_pending = sum(r[4] for r in full_rows if r[8] == "expected")

    # Prior month
    prior_net = sum(r[6] for r in prior_rows)
    prior_change_pct = round(((mtd_net - prior_net) / prior_net * 100), 1) if prior_net > 0 else 0

    # Ring % (this studio's hours / total hours across all studios MTD)
    rate = STUDIO_RATES.get(studio_name, 100.0)
    ring_pct = 0.0  # computed later across all studios

    # Sources breakdown
    sources: Dict[str, int] = {}
    for r in mtd_rows:
        src = r[7] or "direct"
        sources[src] = sources.get(src, 0) + 1
    total_src = sum(sources.values()) or 1
    source_pcts = {k: round(v / total_src * 100) for k, v in sources.items()}

    # Status breakdown
    statuses: Dict[str, int] = {}
    for r in mtd_rows:
        st = r[8] or "expected"
        statuses[st] = statuses.get(st, 0) + 1

    # Performance metrics
    bookings_list = [
        {
            "id": r[0],
            "date": r[1],
            "hours": r[2],
            "rate": r[3],
            "gross": r[4],
            "fees": r[5],
            "net": r[6],
            "source": r[7],
            "status": r[8],
        }
        for r in mtd_rows
    ]

    return {
        "name": studio_name,
        "rate": rate,
        "ring_pct": ring_pct,
        "mtd": {
            "hours": round(mtd_hours, 1),
            "gross": round(mtd_gross, 2),
            "fees": round(mtd_fees, 2),
            "net": round(mtd_net, 2),
            "bookings": mtd_bookings,
            "collected": mtd_collected,
            "expected": mtd_bookings - mtd_collected,
        },
        "full_month": {
            "hours": round(full_hours, 1),
            "gross": round(full_gross, 2),
            "fees": round(full_fees, 2),
            "net": round(full_net, 2),
            "bookings": full_bookings,
            "pending_gross": round(full_pending, 2),
        },
        "prior_month": {
            "net": round(prior_net, 2),
            "change_pct": prior_change_pct,
        },
        "sources": source_pcts,
        "source_counts": sources,
        "statuses": statuses,
        "bookings": bookings_list,
        "fee_ratio": round(mtd_fees / mtd_gross * 100, 1) if mtd_gross > 0 else 0,
        "avg_net_per_booking": round(mtd_net / mtd_bookings, 2) if mtd_bookings > 0 else 0,
        "net_per_hour": round(mtd_net / mtd_hours, 2) if mtd_hours > 0 else 0,
    }


def get_studios_dashboard(cur) -> Dict[str, Any]:
    """Full studios page payload — all 3 cards + header + reconciliation."""
    cur.execute("SELECT id, name, rate FROM studios ORDER BY id")
    studio_rows = cur.fetchall()

    cards = []
    total_mtd_hours = 0.0
    for sid, name, rate in studio_rows:
        card = get_studio_card(cur, sid, name)
        cards.append(card)
        total_mtd_hours += card["mtd"]["hours"]

    # Compute ring percentages
    for card in cards:
        card["ring_pct"] = round(card["mtd"]["hours"] / total_mtd_hours * 100, 1) if total_mtd_hours > 0 else 0

    # Header aggregates
    active_count = sum(1 for c in cards if c["mtd"]["bookings"] > 0)
    total_gross = sum(c["mtd"]["gross"] for c in cards)
    total_net = sum(c["mtd"]["net"] for c in cards)
    total_bookings = sum(c["mtd"]["bookings"] for c in cards)

    # Reconciliation checks
    checks = []
    for card in cards:
        m = card["mtd"]
        fm = card["full_month"]
        name = card["name"]
        rate = card["rate"]

        # S1: HRS × RATE = GROSS (MTD)
        s1_ok = abs(m["hours"] * rate - m["gross"]) < 0.02
        checks.append({"studio": name, "check": "S1: HRS×RATE=GROSS (MTD)", "pass": s1_ok,
                       "expected": round(m["hours"] * rate, 2), "observed": m["gross"]})

        # S2: GROSS − FEES = NET (MTD)
        s2_ok = abs(m["gross"] - m["fees"] - m["net"]) < 0.02
        checks.append({"studio": name, "check": "S2: GROSS−FEES=NET (MTD)", "pass": s2_ok,
                       "expected": round(m["gross"] - m["fees"], 2), "observed": m["net"]})

        # S3: Full-month HRS × RATE = GROSS
        s3_ok = abs(fm["hours"] * rate - fm["gross"]) < 0.02
        checks.append({"studio": name, "check": "S3: HRS×RATE=GROSS (Full)", "pass": s3_ok,
                       "expected": round(fm["hours"] * rate, 2), "observed": fm["gross"]})

        # S4: Full-month ≥ MTD
        s4_ok = fm["gross"] >= m["gross"] and fm["hours"] >= m["hours"] and fm["bookings"] >= m["bookings"]
        checks.append({"studio": name, "check": "S4: Full-month ≥ MTD", "pass": s4_ok})

    return {
        "header": {
            "active": active_count,
            "total": len(cards),
            "month_label": date.today().strftime("%B").upper(),
            "total_gross_mtd": round(total_gross, 2),
            "total_net_mtd": round(total_net, 2),
            "total_bookings_mtd": total_bookings,
            "total_hours_mtd": round(total_mtd_hours, 1),
        },
        "studios": cards,
        "reconciliation": checks,
        "all_checks_pass": all(c["pass"] for c in checks),
    }


def add_booking(cur, studio_name: str, booking_date: str, hours: float,
                rate: Optional[float] = None, fees: float = 0.0,
                source: str = "direct", status: str = "expected",
                guest_name: Optional[str] = None, notes: Optional[str] = None) -> Dict[str, Any]:
    """Add a booking with automatic reconciliation."""
    cur.execute("SELECT id, rate FROM studios WHERE name = %s", (studio_name,))
    row = cur.fetchone()
    if not row:
        raise ValueError(f"Studio not found: {studio_name}")
    studio_id, default_rate = row
    r = rate if rate is not None else default_rate
    gross, net = reconcile_booking(hours, r, fees)
    now = int(datetime.now().timestamp())
    cur.execute(
        """INSERT INTO studio_bookings
           (studio_id, booking_date, hours, rate, gross, fees, net, source, status, guest_name, notes, created_at, updated_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           RETURNING id""",
        (studio_id, booking_date, hours, r, gross, fees, net, source, status, guest_name, notes, now, now),
    )
    bid = cur.fetchone()[0]
    return {"ok": True, "id": bid, "gross": gross, "net": net, "fees": fees}
