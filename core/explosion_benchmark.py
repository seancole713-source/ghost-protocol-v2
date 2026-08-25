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

import datetime as dt
import json
import logging
import math
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

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

BENCHMARK_VERSION = "2"
BENCHMARK_HISTORY_DAYS = 450
_RUN_STALE_S = 3 * 3600
_CT = ZoneInfo("America/Chicago")


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
            crossed_10pct_ts BIGINT,
            crossed_20pct_ts BIGINT,
            first_alert_ts BIGINT,
            benchmark_version TEXT NOT NULL,
            created_at BIGINT NOT NULL,
            UNIQUE (symbol, tier, start_ts)
        )
        """
    )
    cur.execute("ALTER TABLE ghost_explosion_events ADD COLUMN IF NOT EXISTS crossed_10pct_ts BIGINT")
    cur.execute("ALTER TABLE ghost_explosion_events ADD COLUMN IF NOT EXISTS crossed_20pct_ts BIGINT")
    cur.execute("ALTER TABLE ghost_explosion_events ADD COLUMN IF NOT EXISTS first_alert_ts BIGINT")
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
    cur.execute(
        """DELETE FROM ghost_explosion_observations newer
           USING ghost_explosion_observations older
           WHERE newer.id > older.id
             AND newer.symbol=older.symbol
             AND newer.kind=older.kind
             AND newer.kind IN ('watch','candidate')
             AND (newer.observed_at / 300)=(older.observed_at / 300)"""
    )
    cur.execute("DROP INDEX IF EXISTS idx_explosion_obs_5m_once")
    cur.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_explosion_obs_detection_5m_once
           ON ghost_explosion_observations (symbol, kind, ((observed_at / 300)))
           WHERE kind IN ('watch','candidate')"""
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_explosion_benchmark_runs (
            id SERIAL PRIMARY KEY,
            benchmark_version TEXT NOT NULL,
            session_date DATE NOT NULL,
            status TEXT NOT NULL,
            started_at BIGINT NOT NULL,
            completed_at BIGINT,
            symbols_requested INT NOT NULL DEFAULT 0,
            symbols_completed INT NOT NULL DEFAULT 0,
            symbols_failed INT NOT NULL DEFAULT 0,
            events_inserted INT NOT NULL DEFAULT 0,
            outcomes_resolved INT NOT NULL DEFAULT 0,
            last_error TEXT,
            UNIQUE (benchmark_version, session_date)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_explosion_benchmark_symbol_runs (
            id SERIAL PRIMARY KEY,
            benchmark_version TEXT NOT NULL,
            session_date DATE NOT NULL,
            symbol VARCHAR(20) NOT NULL,
            status TEXT NOT NULL,
            events_inserted INT NOT NULL DEFAULT 0,
            outcomes_resolved INT NOT NULL DEFAULT 0,
            completed_at BIGINT,
            last_error TEXT,
            UNIQUE (benchmark_version, session_date, symbol)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_explosion_alert_outcomes (
            id SERIAL PRIMARY KEY,
            benchmark_version TEXT NOT NULL,
            observation_id INT NOT NULL,
            symbol VARCHAR(20) NOT NULL,
            alerted_at BIGINT NOT NULL,
            alert_price FLOAT NOT NULL,
            maturity_ts BIGINT NOT NULL,
            resolved_at BIGINT NOT NULL,
            peak_move_pct FLOAT NOT NULL,
            is_false_alert BOOLEAN NOT NULL,
            matched_tier TEXT,
            UNIQUE (benchmark_version, observation_id)
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_explosion_outcomes_symbol "
        "ON ghost_explosion_alert_outcomes (symbol, alerted_at DESC)"
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
    """Record point-in-time detection evidence without hot-path DDL.

    Repeated WATCH/candidate scans are coalesced into five-minute buckets by a
    partial database index. Delivered alerts are never coalesced here, so
    repeated operator notifications remain visible as real burden.
    """
    sym = (symbol or "").upper()
    normalized_kind = (kind or "").strip().lower()
    if not sym or normalized_kind not in {"watch", "candidate", "alert"}:
        return
    ts = int(observed_at or _now())
    sql = """INSERT INTO ghost_explosion_observations
             (symbol, observed_at, price, kind, confidence_pct, created_at)
             VALUES (%s,%s,%s,%s,%s,%s)
             ON CONFLICT DO NOTHING"""
    args = (sym, ts, price, normalized_kind, confidence_pct, _now())
    try:
        if cur is not None:
            cur.execute(sql, args)
        else:
            from core.db import db_conn
            with db_conn() as conn:
                conn.cursor().execute(sql, args)
    except Exception as exc:
        # Detection cannot depend on measurement persistence.
        LOGGER.debug("record_observation(%s): %s", sym, str(exc)[:80])


def _epoch_day(value: Any) -> Optional[int]:
    """Normalize a provider daily-bar label to an exchange session epoch."""
    try:
        if isinstance(value, dt.datetime):
            parsed = value
        elif isinstance(value, dt.date):
            parsed = dt.datetime.combine(value, dt.time(), tzinfo=_CT)
        elif isinstance(value, (int, float)):
            raw = float(value)
            if raw > 1e15:
                raw /= 1e9
            elif raw > 1e12:
                raw /= 1e3
            day = dt.datetime.fromtimestamp(raw, dt.timezone.utc).date()
            return int(dt.datetime.combine(day, dt.time(), tzinfo=dt.timezone.utc).timestamp())
        else:
            text = str(value or "").strip()
            if not text:
                return None
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(_CT)
        day = parsed.date()
        return int(dt.datetime.combine(day, dt.time(), tzinfo=dt.timezone.utc).timestamp())
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _normalized_bars(bars: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_day: Dict[int, Dict[str, Any]] = {}
    for bar in bars or []:
        raw_ts = bar.get("ts")
        if raw_ts is None:
            raw_ts = bar.get("t")
        if raw_ts is None:
            raw_ts = bar.get("date")
        day_ts = _epoch_day(raw_ts)
        try:
            close = float(bar.get("close") or bar.get("c") or 0)
            high = float(bar.get("high") or bar.get("h") or close)
        except (TypeError, ValueError):
            continue
        if day_ts is not None and close > 0 and high > 0 and math.isfinite(close) and math.isfinite(high):
            by_day[day_ts] = {"ts": day_ts, "close": close, "high": max(high, close)}
    return [by_day[k] for k in sorted(by_day)]


def detect_explosion_events(
    bars: List[Dict[str, Any]],
    *,
    symbol: str,
) -> List[Dict[str, Any]]:
    """Detect every fully matured preregistered explosion episode.

    The start convention is the completed session close. Threshold crossings
    use later session highs, which captures an intraday explosion without
    pretending Ghost could have known it before that session. Overlapping start
    points for the same tier are collapsed into one episode until its peak.
    """
    sym = (symbol or "").upper()
    rows = _normalized_bars(bars)
    if len(rows) < 2:
        return []
    events: List[Dict[str, Any]] = []
    for label, required_move_pct, window in EXPLOSION_TIERS:
        next_eligible_index = 0
        for i in range(0, len(rows) - window):
            if i < next_eligible_index:
                continue
            start = rows[i]
            forward = rows[i + 1:i + 1 + window]
            start_px = float(start["close"])
            peak = max(forward, key=lambda row: float(row["high"]))
            gain = (float(peak["high"]) - start_px) / start_px * 100.0
            if gain < required_move_pct:
                continue
            crossed: Dict[float, Optional[int]] = {checkpoint: None for checkpoint in ALERT_BEFORE_CHECKPOINTS}
            for row in forward:
                move = (float(row["high"]) - start_px) / start_px * 100.0
                for checkpoint in ALERT_BEFORE_CHECKPOINTS:
                    if crossed[checkpoint] is None and move >= checkpoint:
                        crossed[checkpoint] = int(row["ts"])
            events.append({
                "symbol": sym,
                "tier": label,
                "window_days": window,
                "move_pct": round(gain, 2),
                "start_price": round(start_px, 4),
                "peak_price": round(float(peak["high"]), 4),
                "start_ts": int(start["ts"]),
                "peak_ts": int(peak["ts"]),
                "crossed_10pct_ts": crossed[10.0],
                "crossed_20pct_ts": crossed[20.0],
            })
            # A continuing climb is one episode, not one event per baseline.
            peak_index = rows.index(peak)
            next_eligible_index = max(i + 1, peak_index + 1)
    return events


def _session_end_ts(day_ts: int) -> int:
    return int(day_ts) + 86400 - 1


def _first_observation(cur, symbol: str, start_ts: int, peak_ts: int) -> Optional[Dict[str, Any]]:
    """Earliest observation from the start session through peak session."""
    cur.execute(
        """SELECT observed_at, price, kind, confidence_pct
           FROM ghost_explosion_observations
           WHERE symbol=%s AND observed_at >= %s AND observed_at <= %s
           ORDER BY observed_at ASC LIMIT 1""",
        (symbol.upper(), start_ts, _session_end_ts(peak_ts)),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {"observed_at": row[0], "price": row[1], "kind": row[2], "confidence_pct": row[3]}


def _first_promotion(cur, symbol: str, start_ts: int, peak_ts: int) -> Optional[Dict[str, Any]]:
    """Earliest candidate observation; delivered alerts are tracked separately."""
    cur.execute(
        """SELECT observed_at, price, kind, confidence_pct
           FROM ghost_explosion_observations
           WHERE symbol=%s AND observed_at >= %s AND observed_at <= %s
             AND kind='candidate'
           ORDER BY observed_at ASC LIMIT 1""",
        (symbol.upper(), start_ts, _session_end_ts(peak_ts)),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {"observed_at": row[0], "price": row[1], "kind": row[2], "confidence_pct": row[3]}


def _first_alert(cur, symbol: str, start_ts: int, peak_ts: int) -> Optional[Dict[str, Any]]:
    cur.execute(
        """SELECT observed_at, price, kind, confidence_pct
           FROM ghost_explosion_observations
           WHERE symbol=%s AND observed_at >= %s AND observed_at <= %s
             AND kind='alert'
           ORDER BY observed_at ASC LIMIT 1""",
        (symbol.upper(), start_ts, _session_end_ts(peak_ts)),
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

    first = _first_observation(cur, sym, start_ts, peak_ts) if cur is not None else None
    promoted = _first_promotion(cur, sym, start_ts, peak_ts) if cur is not None else None
    first_alert = _first_alert(cur, sym, start_ts, peak_ts) if cur is not None else None
    if cur is None:
        from core.db import db_conn
        with db_conn() as conn:
            return record_event_with_observations(event, cur=conn.cursor())

    first_obs_ts = first.get("observed_at") if first else None
    first_obs_px = first.get("price") if first else None
    promoted_ts = promoted.get("observed_at") if promoted else None
    promoted_px = promoted.get("price") if promoted else None
    first_alert_ts = first_alert.get("observed_at") if first_alert else None

    max_captured = None
    if first_obs_px and first_obs_px > 0:
        max_captured = round((float(event["peak_price"]) - float(first_obs_px)) / float(first_obs_px) * 100.0, 2)

    crossed_10 = event.get("crossed_10pct_ts")
    crossed_20 = event.get("crossed_20pct_ts")
    alerted_before_10 = bool(first_alert_ts and crossed_10 and first_alert_ts < int(crossed_10))
    alerted_before_20 = bool(first_alert_ts and crossed_20 and first_alert_ts < int(crossed_20))

    cur.execute(
        """INSERT INTO ghost_explosion_events
           (symbol, tier, window_days, move_pct, start_price, peak_price,
            start_ts, peak_ts, first_observed_ts, first_observed_price,
            promoted_ts, promoted_price, max_move_captured_pct,
            alerted_before_10pct, alerted_before_20pct, crossed_10pct_ts,
            crossed_20pct_ts, first_alert_ts, benchmark_version, created_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (symbol, tier, start_ts) DO UPDATE SET
             move_pct=EXCLUDED.move_pct, peak_price=EXCLUDED.peak_price,
             peak_ts=EXCLUDED.peak_ts, first_observed_ts=EXCLUDED.first_observed_ts,
             first_observed_price=EXCLUDED.first_observed_price,
             promoted_ts=EXCLUDED.promoted_ts, promoted_price=EXCLUDED.promoted_price,
             max_move_captured_pct=EXCLUDED.max_move_captured_pct,
             alerted_before_10pct=EXCLUDED.alerted_before_10pct,
             alerted_before_20pct=EXCLUDED.alerted_before_20pct,
             crossed_10pct_ts=EXCLUDED.crossed_10pct_ts,
             crossed_20pct_ts=EXCLUDED.crossed_20pct_ts,
             first_alert_ts=EXCLUDED.first_alert_ts,
             benchmark_version=EXCLUDED.benchmark_version
           RETURNING id""",
        (sym, event["tier"], event["window_days"], event["move_pct"],
         event["start_price"], event["peak_price"], start_ts, peak_ts,
         first_obs_ts, first_obs_px, promoted_ts, promoted_px, max_captured,
         alerted_before_10, alerted_before_20, crossed_10, crossed_20,
         first_alert_ts, BENCHMARK_VERSION, _now()),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def _latest_completed_session(now: Optional[dt.datetime] = None) -> dt.date:
    """Latest NYSE session whose daily bar is past the settlement buffer."""
    from core.market_hours import _rth_close_for, is_market_holiday

    current = now or dt.datetime.now(_CT)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_CT)
    else:
        current = current.astimezone(_CT)
    day = current.date()
    close_min = _rth_close_for(current)
    if is_market_holiday(day) or current.hour * 60 + current.minute < close_min + 35:
        day -= dt.timedelta(days=1)
    while is_market_holiday(day):
        day -= dt.timedelta(days=1)
    return day


def _day_epoch(day: dt.date) -> int:
    return int(dt.datetime.combine(day, dt.time(), tzinfo=dt.timezone.utc).timestamp())


def _claim_run(session_date: dt.date, symbols_requested: int) -> bool:
    from core.db import db_conn

    now = _now()
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO ghost_explosion_benchmark_runs
               (benchmark_version, session_date, status, started_at, symbols_requested)
               VALUES (%s,%s,'running',%s,%s)
               ON CONFLICT (benchmark_version, session_date) DO UPDATE SET
                 status='running', started_at=EXCLUDED.started_at,
                 symbols_requested=EXCLUDED.symbols_requested, last_error=NULL
               WHERE ghost_explosion_benchmark_runs.status IN ('partial','failed')
                  OR (ghost_explosion_benchmark_runs.status='running'
                      AND ghost_explosion_benchmark_runs.started_at < %s)
               RETURNING id""",
            (BENCHMARK_VERSION, session_date, now, symbols_requested, now - _RUN_STALE_S),
        )
        return cur.fetchone() is not None


def _resolve_alert_outcomes(cur, symbol: str, rows: Sequence[Dict[str, Any]]) -> int:
    """Resolve delivered alerts only after 20 completed forward sessions."""
    cur.execute(
        """SELECT o.id, o.observed_at, o.price
           FROM ghost_explosion_observations o
           LEFT JOIN ghost_explosion_alert_outcomes r
             ON r.benchmark_version=%s AND r.observation_id=o.id
           WHERE o.symbol=%s AND o.kind='alert' AND o.price > 0 AND r.id IS NULL
           ORDER BY o.observed_at""",
        (BENCHMARK_VERSION, symbol),
    )
    alerts = cur.fetchall()
    resolved = 0
    day_values = [int(row["ts"]) for row in rows]
    for observation_id, alerted_at, alert_price in alerts:
        alert_day_date = dt.datetime.fromtimestamp(int(alerted_at), dt.timezone.utc).astimezone(_CT).date()
        alert_day = _day_epoch(alert_day_date)
        next_indexes = [i for i, day_ts in enumerate(day_values) if day_ts > alert_day]
        if not next_indexes:
            continue
        start_index = next_indexes[0]
        forward = list(rows[start_index:start_index + 20])
        if len(forward) < 20:
            continue
        price = float(alert_price)
        peak_move = max((float(row["high"]) - price) / price * 100.0 for row in forward)
        matched_tier = None
        for label, threshold, window in EXPLOSION_TIERS:
            window_rows = forward[:window]
            if window_rows and max((float(row["high"]) - price) / price * 100.0 for row in window_rows) >= threshold:
                matched_tier = label
                break
        cur.execute(
            """INSERT INTO ghost_explosion_alert_outcomes
               (benchmark_version, observation_id, symbol, alerted_at, alert_price,
                maturity_ts, resolved_at, peak_move_pct, is_false_alert, matched_tier)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (benchmark_version, observation_id) DO NOTHING""",
            (BENCHMARK_VERSION, observation_id, symbol, alerted_at, price,
             int(forward[-1]["ts"]), _now(), round(peak_move, 2),
             matched_tier is None, matched_tier),
        )
        resolved += int(cur.rowcount or 0)
    return resolved


def run_daily_benchmark_job(
    *,
    symbols: Optional[Sequence[str]] = None,
    now: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    """Resumable leader-scheduled benchmark over completed daily bars."""
    from config.symbols import OFFICIAL_WATCHLIST
    from core.db import db_conn
    from core.market_history import get_daily_history

    universe = tuple(dict.fromkeys((s or "").strip().upper() for s in (symbols or OFFICIAL_WATCHLIST) if (s or "").strip()))
    session_date = _latest_completed_session(now)
    if not _claim_run(session_date, len(universe)):
        return {"ok": True, "skipped": "already claimed", "session_date": session_date.isoformat()}

    completed = failed = events_written = outcomes_resolved = 0
    errors: List[str] = []
    cutoff = _day_epoch(session_date)
    for symbol in universe:
        try:
            with db_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    """SELECT status FROM ghost_explosion_benchmark_symbol_runs
                       WHERE benchmark_version=%s AND session_date=%s AND symbol=%s""",
                    (BENCHMARK_VERSION, session_date, symbol),
                )
                existing = cur.fetchone()
            if existing and existing[0] == "done":
                completed += 1
                continue
            raw = get_daily_history(symbol, BENCHMARK_HISTORY_DAYS, force_refresh=True)
            rows = [row for row in _normalized_bars(raw) if int(row["ts"]) <= cutoff]
            if len(rows) < max(window for _, _, window in EXPLOSION_TIERS) + 1:
                raise RuntimeError("insufficient completed daily history")
            events = detect_explosion_events(rows, symbol=symbol)
            inserted = 0
            with db_conn() as conn:
                cur = conn.cursor()
                for event in events:
                    cur.execute("SAVEPOINT explosion_event")
                    try:
                        if record_event_with_observations(event, cur=cur):
                            inserted += 1
                        cur.execute("RELEASE SAVEPOINT explosion_event")
                    except Exception:
                        cur.execute("ROLLBACK TO SAVEPOINT explosion_event")
                        cur.execute("RELEASE SAVEPOINT explosion_event")
                        raise
                resolved = _resolve_alert_outcomes(cur, symbol, rows)
                cur.execute(
                    """INSERT INTO ghost_explosion_benchmark_symbol_runs
                       (benchmark_version, session_date, symbol, status, events_inserted,
                        outcomes_resolved, completed_at)
                       VALUES (%s,%s,%s,'done',%s,%s,%s)
                       ON CONFLICT (benchmark_version, session_date, symbol) DO UPDATE SET
                         status='done', events_inserted=EXCLUDED.events_inserted,
                         outcomes_resolved=EXCLUDED.outcomes_resolved,
                         completed_at=EXCLUDED.completed_at, last_error=NULL""",
                    (BENCHMARK_VERSION, session_date, symbol, inserted, resolved, _now()),
                )
            completed += 1
            events_written += inserted
            outcomes_resolved += resolved
        except Exception as exc:
            failed += 1
            errors.append(f"{symbol}: {str(exc)[:100]}")
            try:
                with db_conn() as conn:
                    conn.cursor().execute(
                        """INSERT INTO ghost_explosion_benchmark_symbol_runs
                           (benchmark_version, session_date, symbol, status, last_error)
                           VALUES (%s,%s,%s,'failed',%s)
                           ON CONFLICT (benchmark_version, session_date, symbol) DO UPDATE SET
                             status='failed', last_error=EXCLUDED.last_error""",
                        (BENCHMARK_VERSION, session_date, symbol, str(exc)[:200]),
                    )
            except Exception:
                LOGGER.exception("benchmark failed to record symbol failure %s", symbol)

    status = "done" if failed == 0 else "partial"
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """UPDATE ghost_explosion_benchmark_runs SET status=%s, completed_at=%s,
                 symbols_completed=%s, symbols_failed=%s, events_inserted=%s,
                 outcomes_resolved=%s, last_error=%s
               WHERE benchmark_version=%s AND session_date=%s""",
            (status, _now(), completed, failed, events_written, outcomes_resolved,
             "; ".join(errors)[:1000] or None, BENCHMARK_VERSION, session_date),
        )
        if status == "done":
            cur.execute("DELETE FROM ghost_explosion_observations WHERE kind <> 'alert' AND created_at < %s", (_now() - 120 * 86400,))
            cur.execute("DELETE FROM ghost_explosion_benchmark_symbol_runs WHERE session_date < %s", (session_date - dt.timedelta(days=90),))
            cur.execute("DELETE FROM ghost_explosion_benchmark_runs WHERE session_date < %s", (session_date - dt.timedelta(days=90),))
    return {
        "ok": failed == 0,
        "status": status,
        "session_date": session_date.isoformat(),
        "symbols_completed": completed,
        "symbols_failed": failed,
        "events_written": events_written,
        "outcomes_resolved": outcomes_resolved,
        "errors": errors[:10],
    }


def benchmark_summary(cur=None) -> Dict[str, Any]:
    """Aggregate recall metrics over all recorded events.

    Returns per-tier recall (fraction of events Ghost observed before peak) and
    the alert-arrival rates. Read-only; never blocks a pick.
    """
    try:
        owns_connection = cur is None
        if owns_connection:
            from core.db import db_conn
            with db_conn() as conn:
                return benchmark_summary(cur=conn.cursor())
        cur.execute(
            """SELECT tier, COUNT(*),
                      COUNT(first_observed_ts),
                      COUNT(*) FILTER (WHERE alerted_before_10pct),
                      COUNT(*) FILTER (WHERE alerted_before_20pct)
               FROM ghost_explosion_events
               WHERE benchmark_version=%s
               GROUP BY tier ORDER BY tier""",
            (BENCHMARK_VERSION,),
        )
        rows = cur.fetchall()
        cur.execute(
            """SELECT COUNT(*), COUNT(*) FILTER (WHERE NOT is_false_alert)
               FROM ghost_explosion_alert_outcomes
               WHERE benchmark_version=%s""",
            (BENCHMARK_VERSION,),
        )
        outcome_row = cur.fetchone() if hasattr(cur, "fetchone") else (0, 0)
        outcome_row = outcome_row or (0, 0)
        cur.execute(
            """SELECT session_date, status, symbols_completed, symbols_failed,
                      completed_at, last_error
               FROM ghost_explosion_benchmark_runs
               WHERE benchmark_version=%s
               ORDER BY session_date DESC LIMIT 1""",
            (BENCHMARK_VERSION,),
        )
        run_row = cur.fetchone() if hasattr(cur, "fetchone") else None
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
    resolved_alerts = int(outcome_row[0] or 0)
    true_alerts = int(outcome_row[1] or 0)
    false_alerts = max(0, resolved_alerts - true_alerts)
    last_run = None
    if run_row:
        last_run = {
            "session_date": run_row[0].isoformat() if hasattr(run_row[0], "isoformat") else str(run_row[0]),
            "status": run_row[1],
            "symbols_completed": int(run_row[2] or 0),
            "symbols_failed": int(run_row[3] or 0),
            "completed_at": run_row[4],
            "last_error": run_row[5],
        }
    return {
        "ok": True,
        "benchmark_version": BENCHMARK_VERSION,
        "total_events": total_events,
        "total_observed": total_observed,
        "overall_recall_pct": round(total_observed / total_events * 100, 1) if total_events else 0.0,
        "resolved_alerts": resolved_alerts,
        "true_alerts": true_alerts,
        "false_alerts": false_alerts,
        "alert_precision_pct": round(true_alerts / resolved_alerts * 100, 1) if resolved_alerts else None,
        "last_run": last_run,
        "tiers": tiers,
        "note": "Detection recall and delivered-alert precision are separate. "
                "Peer-derived evidence and benchmark outcomes never loosen trade gates.",
    }
