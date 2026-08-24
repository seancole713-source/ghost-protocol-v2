"""core/explosion_benchmark.py — preregistered explosion-event detection benchmark.

Measures Ghost's *detection recall* independently of its *trading precision*.
An "explosion event" is defined BEFORE testing (no hindsight redefinition):

    +20% within 1 trading day
    +30% within 5 trading days
    +50% within 10 trading days
    +100% within 20 trading days

For every event we record, point-in-time:
  - when Ghost first observed the symbol (first WATCH / candidate / alert),
  - the price at first observation,
  - the price when promoted to a trade candidate,
  - the maximum move captured,
  - whether the alert arrived before +10% / +20%,
  - the false-alert burden (alerts that never became events).

This is a *measurement* ledger, not a trading gate. It never blocks a pick; it
only reports how well the detection tier is doing against a fixed target.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

LOGGER = logging.getLogger("ghost.explosion_benchmark")

# Preregistered event thresholds: (label, move_pct, window_trading_days).
# Ordered smallest→largest so a symbol can satisfy multiple tiers.
EXPLOSION_TIERS: Tuple[Tuple[str, float, int], ...] = (
    ("+20%_1d", 20.0, 1),
    ("+30%_5d", 30.0, 5),
    ("+50%_10d", 50.0, 10),
    ("+100%_20d", 100.0, 20),
)

# Alert-arrival checkpoints: did Ghost alert before the move crossed this %?
ALERT_BEFORE_CHECKPOINTS = (10.0, 20.0)

BENCHMARK_VERSION = "1"


def _now() -> int:
    return int(time.time())


def _jsonb(v: Any) -> Optional[str]:
    if v is None:
        return None
    try:
        return json.dumps(v, default=str)
    except Exception:
        return None


def ensure_benchmark_tables(cur) -> None:
    """Create the explosion-event + observation tables. Idempotent."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_explosion_events (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            tier TEXT NOT NULL,
            window_days INT NOT NULL,
            move_pct FLOAT NOT NULL,
            start_price FLOAT NOT NULL,
            peak_price FLOAT NOT NULL,
            start_ts BIGINT NOT NULL,
            peak_ts BIGINT NOT NULL,
            first_observed_ts BIGINT,
            first_observed_price FLOAT,
            promoted_ts BIGINT,
            promoted_price FLOAT,
            max_move_captured_pct FLOAT,
            alerted_before_10pct BOOLEAN,
            alerted_before_20pct BOOLEAN,
            benchmark_version TEXT NOT NULL,
            created_at BIGINT NOT NULL,
            UNIQUE (symbol, tier, start_ts)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_explosion_observations (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            observed_at BIGINT NOT NULL,
            price FLOAT,
            kind TEXT NOT NULL,
            confidence_pct FLOAT,
            created_at BIGINT NOT NULL
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_explosion_events_sym_ts "
        "ON ghost_explosion_events (symbol, start_ts DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_explosion_obs_sym_ts "
        "ON ghost_explosion_observations (symbol, observed_at DESC)"
    )


def record_observation(
    symbol: str,
    *,
    price: Optional[float],
    kind: str,
    confidence_pct: Optional[float] = None,
    observed_at: Optional[int] = None,
    cur=None,
) -> None:
    """Record one detection observation (WATCH / candidate / alert) point-in-time.

    This is the raw "when did Ghost first see it" evidence the benchmark needs.
    Best-effort: a failure to record must never break the detection path.
    """
    sym = (symbol or "").upper()
    if not sym:
        return
    ts = int(observed_at or _now())
    try:
        if cur is not None:
            ensure_benchmark_tables(cur)
            cur.execute(
                """INSERT INTO ghost_explosion_observations
                   (symbol, observed_at, price, kind, confidence_pct, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (sym, ts, price, kind, confidence_pct, _now()),
            )
        else:
            from core.db import db_conn
            with db_conn() as conn:
                c = conn.cursor()
                ensure_benchmark_tables(c)
                c.execute(
                    """INSERT INTO ghost_explosion_observations
                       (symbol, observed_at, price, kind, confidence_pct, created_at)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (sym, ts, price, kind, confidence_pct, _now()),
                )
    except Exception as exc:
        LOGGER.debug("record_observation(%s): %s", sym, str(exc)[:80])


def detect_explosion_events(
    bars: List[Dict[str, Any]],
    *,
    symbol: str,
) -> List[Dict[str, Any]]:
    """Detect preregistered explosion events in a daily-bar series.

    `bars` is a chronological list of {ts, close} (or {ts, open/high/low/close}).
    Returns a list of event dicts (one per tier satisfied), each with the
    start/peak prices and timestamps. Pure function — no I/O.
    """
    sym = (symbol or "").upper()
    if not bars:
        return []
    closes = []
    for b in bars:
        try:
            c = float(b.get("close") or b.get("c") or 0)
            ts = int(b.get("ts") or b.get("t") or 0)
        except (TypeError, ValueError):
            continue
        if c > 0 and ts > 0:
            closes.append((ts, c))
    if len(closes) < 2:
        return []
    events: List[Dict[str, Any]] = []
    for label, move_pct, window in EXPLOSION_TIERS:
        # Sliding window: for each start index, look ahead up to `window` bars.
        for i in range(len(closes) - 1):
            start_ts, start_px = closes[i]
            peak_px = start_px
            peak_ts = start_ts
            for j in range(i + 1, min(i + 1 + window, len(closes))):
                ts, px = closes[j]
                if px > peak_px:
                    peak_px = px
                    peak_ts = ts
            gain = (peak_px - start_px) / start_px * 100.0
            if gain >= move_pct:
                events.append({
                    "symbol": sym,
                    "tier": label,
                    "window_days": window,
                    "move_pct": round(gain, 2),
                    "start_price": round(start_px, 4),
                    "peak_price": round(peak_px, 4),
                    "start_ts": start_ts,
                    "peak_ts": peak_ts,
                })
                # A symbol can satisfy the same tier from multiple start points;
                # keep the earliest start for a given tier to avoid double-count.
                break
    return events


def _first_observation(cur, symbol: str, start_ts: int, peak_ts: int) -> Optional[Dict[str, Any]]:
    """Earliest observation in [start_ts, peak_ts] for a symbol."""
    cur.execute(
        """SELECT observed_at, price, kind, confidence_pct
           FROM ghost_explosion_observations
           WHERE symbol=%s AND observed_at >= %s AND observed_at <= %s
           ORDER BY observed_at ASC LIMIT 1""",
        (symbol.upper(), start_ts, peak_ts),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {"observed_at": row[0], "price": row[1], "kind": row[2], "confidence_pct": row[3]}


def _first_promotion(cur, symbol: str, start_ts: int, peak_ts: int) -> Optional[Dict[str, Any]]:
    """Earliest trade-candidate/alert observation (promotion) in the window."""
    cur.execute(
        """SELECT observed_at, price, kind, confidence_pct
           FROM ghost_explosion_observations
           WHERE symbol=%s AND observed_at >= %s AND observed_at <= %s
             AND kind IN ('candidate','telegram','alert')
           ORDER BY observed_at ASC LIMIT 1""",
        (symbol.upper(), start_ts, peak_ts),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {"observed_at": row[0], "price": row[1], "kind": row[2], "confidence_pct": row[3]}


def record_event_with_observations(
    event: Dict[str, Any],
    *,
    cur=None,
) -> Optional[int]:
    """Persist one detected event, enriched with Ghost's first observation /
    promotion and the alert-arrival checkpoints. Idempotent by (symbol, tier, start_ts)."""
    sym = event.get("symbol", "").upper()
    if not sym:
        return None
    start_ts = int(event.get("start_ts") or 0)
    peak_ts = int(event.get("peak_ts") or 0)
    start_px = float(event.get("start_price") or 0)
    if start_ts <= 0 or start_px <= 0:
        return None

    first = None
    promoted = None
    try:
        if cur is not None:
            first = _first_observation(cur, sym, start_ts, peak_ts)
            promoted = _first_promotion(cur, sym, start_ts, peak_ts)
        else:
            from core.db import db_conn
            with db_conn() as conn:
                c = conn.cursor()
                ensure_benchmark_tables(c)
                first = _first_observation(c, sym, start_ts, peak_ts)
                promoted = _first_promotion(c, sym, start_ts, peak_ts)
    except Exception as exc:
        LOGGER.debug("record_event_with_observations(%s): %s", sym, str(exc)[:80])

    first_obs_ts = first.get("observed_at") if first else None
    first_obs_px = first.get("price") if first else None
    promoted_ts = promoted.get("observed_at") if promoted else None
    promoted_px = promoted.get("price") if promoted else None

    # Max move captured: from first observation price to peak, if observed.
    max_captured = None
    if first_obs_px and first_obs_px > 0:
        max_captured = round((float(event["peak_price"]) - float(first_obs_px)) / float(first_obs_px) * 100.0, 2)

    # Alert-arrival checkpoints: did Ghost observe (any kind) before the move
    # crossed +10% / +20% from the start price?
    alerted_before_10 = False
    alerted_before_20 = False
    if first_obs_ts is not None:
        # The observation price relative to start tells us if it was early.
        if first_obs_px and first_obs_px > 0:
            obs_gain = (float(first_obs_px) - start_px) / start_px * 100.0
            alerted_before_10 = obs_gain < 10.0
            alerted_before_20 = obs_gain < 20.0

    try:
        if cur is not None:
            ensure_benchmark_tables(cur)
            cur.execute(
                """INSERT INTO ghost_explosion_events
                   (symbol, tier, window_days, move_pct, start_price, peak_price,
                    start_ts, peak_ts, first_observed_ts, first_observed_price,
                    promoted_ts, promoted_price, max_move_captured_pct,
                    alerted_before_10pct, alerted_before_20pct,
                    benchmark_version, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (symbol, tier, start_ts) DO NOTHING RETURNING id""",
                (sym, event["tier"], event["window_days"], event["move_pct"],
                 event["start_price"], event["peak_price"], start_ts, peak_ts,
                 first_obs_ts, first_obs_px, promoted_ts, promoted_px,
                 max_captured, alerted_before_10, alerted_before_20,
                 BENCHMARK_VERSION, _now()),
            )
            row = cur.fetchone()
            return int(row[0]) if row else None
        else:
            from core.db import db_conn
            with db_conn() as conn:
                c = conn.cursor()
                ensure_benchmark_tables(c)
                c.execute(
                    """INSERT INTO ghost_explosion_events
                       (symbol, tier, window_days, move_pct, start_price, peak_price,
                        start_ts, peak_ts, first_observed_ts, first_observed_price,
                        promoted_ts, promoted_price, max_move_captured_pct,
                        alerted_before_10pct, alerted_before_20pct,
                        benchmark_version, created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (symbol, tier, start_ts) DO NOTHING RETURNING id""",
                    (sym, event["tier"], event["window_days"], event["move_pct"],
                     event["start_price"], event["peak_price"], start_ts, peak_ts,
                     first_obs_ts, first_obs_px, promoted_ts, promoted_px,
                     max_captured, alerted_before_10, alerted_before_20,
                     BENCHMARK_VERSION, _now()),
                )
                row = c.fetchone()
                return int(row[0]) if row else None
    except Exception as exc:
        LOGGER.warning("record_event_with_observations(%s): %s", sym, str(exc)[:120])
        return None


def benchmark_summary(cur=None) -> Dict[str, Any]:
    """Aggregate recall metrics over all recorded events.

    Returns per-tier recall (fraction of events Ghost observed before peak) and
    the alert-arrival rates. Read-only; never blocks a pick.
    """
    try:
        if cur is not None:
            cur.execute(
                """SELECT tier, COUNT(*),
                          COUNT(first_observed_ts),
                          COUNT(alerted_before_10pct) FILTER (WHERE alerted_before_10pct),
                          COUNT(alerted_before_20pct) FILTER (WHERE alerted_before_20pct)
                   FROM ghost_explosion_events GROUP BY tier ORDER BY tier"""
            )
            rows = cur.fetchall()
        else:
            from core.db import db_conn
            with db_conn() as conn:
                c = conn.cursor()
                ensure_benchmark_tables(c)
                c.execute(
                    """SELECT tier, COUNT(*),
                              COUNT(first_observed_ts),
                              COUNT(alerted_before_10pct) FILTER (WHERE alerted_before_10pct),
                              COUNT(alerted_before_20pct) FILTER (WHERE alerted_before_20pct)
                       FROM ghost_explosion_events GROUP BY tier ORDER BY tier"""
                )
                rows = c.fetchall()
    except Exception as exc:
        LOGGER.warning("benchmark_summary: %s", str(exc)[:120])
        return {"ok": False, "error": str(exc)[:120]}

    tiers = []
    total_events = 0
    total_observed = 0
    for tier, n, observed, before10, before20 in rows:
        n = int(n or 0)
        observed = int(observed or 0)
        before10 = int(before10 or 0)
        before20 = int(before20 or 0)
        total_events += n
        total_observed += observed
        tiers.append({
            "tier": tier,
            "events": n,
            "observed": observed,
            "recall_pct": round(observed / n * 100, 1) if n else 0.0,
            "alerted_before_10pct": before10,
            "alerted_before_20pct": before20,
        })
    return {
        "ok": True,
        "benchmark_version": BENCHMARK_VERSION,
        "total_events": total_events,
        "total_observed": total_observed,
        "overall_recall_pct": round(total_observed / total_events * 100, 1) if total_events else 0.0,
        "tiers": tiers,
        "note": "Detection recall is measured separately from trading precision. "
                "A low recall here means the detection tier missed moves; it does "
                "not imply the trade gate should be loosened.",
    }
