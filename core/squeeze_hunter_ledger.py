"""core/squeeze_hunter_ledger.py — point-in-time audit trail for the Squeeze Hunter.

The Squeeze Hunter is read-only intelligence, but to ever become *measurable*
(and therefore improvable) it must persist, for every evaluation:

  - the full input snapshot (short/trigger/confirmation contexts) as they were
    at evaluation time — so we can later reconstruct exactly what information
    was available (no hindsight bias);
  - source timestamps / freshness;
  - the scoring version;
  - the computed report (scores, stage, projection).

Resolutions are appended separately and idempotently, recording the realized
return at 1/5/14 trading days plus whether the +20% / -20% thresholds were hit.
This is the raw evidence a future calibration step needs (Wilson bounds, Brier
score) — it does NOT itself claim any accuracy.

Design mirrors core/research_ledger.py and core/super_ghost_ledger.py:
  - append-only truth rows (no updates);
  - idempotent by a stable key;
  - schema owned here, created at startup via core.db._migrate_schema().
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

LOGGER = logging.getLogger("ghost.squeeze_hunter_ledger")

# Bump when scoring logic changes so old rows are never silently re-read as
# if they came from the current model.
HUNTER_SCORING_VERSION = "1"

# Resolution horizons in trading days (mirrors super_ghost_ledger's 1/5/20,
# but tuned to the Hunter's 1-14 day window).
HUNTER_HORIZONS = (1, 5, 14)


def _now() -> int:
    return int(time.time())


def _jsonb(v: Any) -> Optional[str]:
    if v is None:
        return None
    try:
        return json.dumps(v, default=str)
    except Exception:
        return None


def ensure_hunter_tables(cur) -> None:
    """Create the Hunter evaluation + resolution tables. Idempotent.

    Includes ALTER TABLE migrations so a table created by an earlier commit
    (before reference_price / session_date / the extra resolution columns
    existed) is upgraded in place — CREATE TABLE IF NOT EXISTS alone does NOT
    add missing columns.
    """
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_squeeze_hunter_evaluations (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            scoring_version VARCHAR(16) NOT NULL,
            session_date VARCHAR(10) NOT NULL,
            issued_ts BIGINT NOT NULL,
            feature_available_ts BIGINT,
            reference_price FLOAT,
            reference_price_ts BIGINT,
            fuel_score FLOAT,
            trigger_score FLOAT,
            confirmation_score FLOAT,
            squeeze_pressure_score FLOAT,
            pressure_band VARCHAR(16),
            stage VARCHAR(24),
            explosion_score FLOAT,
            short_ctx JSONB,
            trigger_ctx JSONB,
            confirm_ctx JSONB,
            factors JSONB,
            projection JSONB,
            planning_levels JSONB,
            created_at BIGINT NOT NULL
        )
        """
    )
    # Migrations for columns added after the table first shipped (commit 60c38fd).
    cur.execute(
        "ALTER TABLE ghost_squeeze_hunter_evaluations "
        "ADD COLUMN IF NOT EXISTS reference_price FLOAT"
    )
    cur.execute(
        "ALTER TABLE ghost_squeeze_hunter_evaluations "
        "ADD COLUMN IF NOT EXISTS session_date VARCHAR(10)"
    )
    cur.execute(
        "ALTER TABLE ghost_squeeze_hunter_evaluations "
        "ADD COLUMN IF NOT EXISTS reference_price_ts BIGINT"
    )
    cur.execute(
        "ALTER TABLE ghost_squeeze_hunter_evaluations "
        "ADD COLUMN IF NOT EXISTS planning_levels JSONB"
    )
    # Idempotency key: one evaluation per symbol per scoring version per
    # exchange session date. The old (symbol, scoring_version, issued_ts)
    # unique constraint is dropped so the honest issued_ts (actual time) does
    # not collide with the stable session_date key.
    cur.execute(
        "ALTER TABLE ghost_squeeze_hunter_evaluations "
        "DROP CONSTRAINT IF EXISTS ghost_squeeze_hunter_evaluations_symbol_scoring_version_issued_ts_key"
    )
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_hunter_eval_symbol_session "
        "ON ghost_squeeze_hunter_evaluations (symbol, scoring_version, session_date)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_hunter_eval_symbol_time "
        "ON ghost_squeeze_hunter_evaluations (symbol, issued_ts DESC)"
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_squeeze_hunter_resolutions (
            id SERIAL PRIMARY KEY,
            evaluation_id BIGINT NOT NULL UNIQUE,
            resolved_ts BIGINT NOT NULL,
            evidence_available_ts BIGINT NOT NULL,
            return_1d_pct FLOAT,
            return_5d_pct FLOAT,
            return_14d_pct FLOAT,
            hit_plus_20 BOOLEAN,
            hit_plus_50 BOOLEAN,
            hit_plus_100 BOOLEAN,
            hit_minus_20 BOOLEAN,
            max_favorable_pct FLOAT,
            max_adverse_pct FLOAT,
            reason VARCHAR(200),
            created_at BIGINT NOT NULL
        )
        """
    )
    # P0 migration: add the extra forecast labels + excursion columns to a
    # resolution table created before they existed.
    for col, typ in (
        ("hit_plus_50", "BOOLEAN"),
        ("hit_plus_100", "BOOLEAN"),
        ("max_favorable_pct", "FLOAT"),
        ("max_adverse_pct", "FLOAT"),
    ):
        cur.execute(
            f"ALTER TABLE ghost_squeeze_hunter_resolutions "
            f"ADD COLUMN IF NOT EXISTS {col} {typ}"
        )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_hunter_res_eval "
        "ON ghost_squeeze_hunter_resolutions (evaluation_id)"
    )


def enforce_hunter_constraints(cur) -> None:
    """Apply strict invariants after invalid legacy rows have been purged."""
    cur.execute(
        "ALTER TABLE ghost_squeeze_hunter_evaluations "
        "ALTER COLUMN session_date SET NOT NULL"
    )


def purge_invalid_hunter_samples(cur) -> int:
    """One-time cleanup of structurally invalid pre-enforcement samples.

    Older issuance paths could persist rows without a valid reference price,
    provider observation timestamp, or exchange session date. Such rows are not
    prospective calibration samples and must be removed before the startup
    migration applies strict constraints. Idempotent once cleanup is complete.
    """
    invalid_where = (
        "reference_price IS NULL OR reference_price <= 0 "
        "OR reference_price_ts IS NULL OR session_date IS NULL"
    )
    cur.execute(
        f"""
        DELETE FROM ghost_squeeze_hunter_resolutions
        WHERE evaluation_id IN (
            SELECT id FROM ghost_squeeze_hunter_evaluations
            WHERE {invalid_where}
        )
        """
    )
    cur.execute(
        f"""
        DELETE FROM ghost_squeeze_hunter_evaluations
        WHERE {invalid_where}
        """
    )
    return cur.rowcount


def persist_hunter_evaluation(
    *,
    symbol: str,
    report: Dict[str, Any],
    short_ctx: Optional[Dict[str, Any]] = None,
    trigger_ctx: Optional[Dict[str, Any]] = None,
    confirm_ctx: Optional[Dict[str, Any]] = None,
    reference_price: Optional[float] = None,
    reference_price_ts: Optional[int] = None,
    session_date: Optional[str] = None,
    issued_ts: Optional[int] = None,
    feature_available_ts: Optional[int] = None,
    cur=None,
) -> Dict[str, Any]:
    """Persist one immutable sample with an explicit, non-ambiguous result."""
    sym = (symbol or "").strip().upper()
    if (
        not sym
        or reference_price is None
        or reference_price <= 0
        or reference_price_ts is None
        or not session_date
    ):
        return {"status": "invalid_reference", "evaluation_id": None}
    ts = int(issued_ts or _now())
    fav = int(feature_available_ts) if feature_available_ts is not None else int(reference_price_ts)
    rpts = int(reference_price_ts)

    def _impl(c) -> Dict[str, Any]:
        c.execute(
            """
            INSERT INTO ghost_squeeze_hunter_evaluations
                (symbol, scoring_version, session_date, issued_ts, feature_available_ts,
                 reference_price, reference_price_ts,
                 fuel_score, trigger_score, confirmation_score,
                 squeeze_pressure_score, pressure_band, stage, explosion_score,
                 short_ctx, trigger_ctx, confirm_ctx, factors, projection,
                 planning_levels, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s)
            ON CONFLICT (symbol, scoring_version, session_date) DO NOTHING
            RETURNING id
            """,
            (
                sym, HUNTER_SCORING_VERSION, session_date, ts, fav,
                reference_price, rpts,
                report.get("fuel_score"), report.get("trigger_score"),
                report.get("confirmation_score"),
                report.get("squeeze_pressure_score"),
                report.get("pressure_band"), report.get("stage"),
                report.get("explosion_score"),
                _jsonb(short_ctx), _jsonb(trigger_ctx), _jsonb(confirm_ctx),
                _jsonb(report.get("factors")), _jsonb(report.get("projection")),
                _jsonb(report.get("planning_levels")), _now(),
            ),
        )
        row = c.fetchone()
        if row:
            return {"status": "inserted", "evaluation_id": int(row[0])}
        return {"status": "duplicate", "evaluation_id": None}

    try:
        if cur is not None:
            return _impl(cur)
        from core.db import db_conn
        with db_conn() as conn:
            c = conn.cursor()
            result = _impl(c)
            conn.commit()
            return result
    except Exception as exc:
        LOGGER.warning("persist_hunter_evaluation %s: %s", sym, str(exc)[:160])
        return {"status": "database_unavailable", "evaluation_id": None}


def log_hunter_evaluation(
    *,
    symbol: str,
    report: Dict[str, Any],
    short_ctx: Optional[Dict[str, Any]] = None,
    trigger_ctx: Optional[Dict[str, Any]] = None,
    confirm_ctx: Optional[Dict[str, Any]] = None,
    reference_price: Optional[float] = None,
    reference_price_ts: Optional[int] = None,
    session_date: Optional[str] = None,
    issued_ts: Optional[int] = None,
    feature_available_ts: Optional[int] = None,
    cur=None,
) -> Optional[int]:
    """Compatibility wrapper returning an inserted row id or ``None``."""
    result = persist_hunter_evaluation(
        symbol=symbol,
        report=report,
        short_ctx=short_ctx,
        trigger_ctx=trigger_ctx,
        confirm_ctx=confirm_ctx,
        reference_price=reference_price,
        reference_price_ts=reference_price_ts,
        session_date=session_date,
        issued_ts=issued_ts,
        feature_available_ts=feature_available_ts,
        cur=cur,
    )
    return result.get("evaluation_id")


def resolve_hunter_evaluation(
    *,
    evaluation_id: int,
    return_1d_pct: Optional[float] = None,
    return_5d_pct: Optional[float] = None,
    return_14d_pct: Optional[float] = None,
    hit_plus_20: Optional[bool] = None,
    hit_plus_50: Optional[bool] = None,
    hit_plus_100: Optional[bool] = None,
    hit_minus_20: Optional[bool] = None,
    max_favorable_pct: Optional[float] = None,
    max_adverse_pct: Optional[float] = None,
    resolved_ts: Optional[int] = None,
    evidence_available_ts: Optional[int] = None,
    reason: str = "",
    cur=None,
) -> bool:
    """Append one resolution for a Hunter evaluation. Idempotent by evaluation_id.

    Returns True if a new resolution was inserted. The realized returns are the
    raw evidence a future calibration step consumes; this function does NOT
    compute any accuracy claim.
    """
    now = _now()
    rts = int(resolved_ts or now)
    eats = int(evidence_available_ts or now)

    def _impl(c) -> bool:
        c.execute(
            """
            INSERT INTO ghost_squeeze_hunter_resolutions
                (evaluation_id, resolved_ts, evidence_available_ts,
                 return_1d_pct, return_5d_pct, return_14d_pct,
                 hit_plus_20, hit_plus_50, hit_plus_100, hit_minus_20,
                 max_favorable_pct, max_adverse_pct, reason, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (evaluation_id) DO NOTHING
            """,
            (
                evaluation_id, rts, eats,
                return_1d_pct, return_5d_pct, return_14d_pct,
                hit_plus_20, hit_plus_50, hit_plus_100, hit_minus_20,
                max_favorable_pct, max_adverse_pct, reason, now,
            ),
        )
        return c.rowcount > 0

    try:
        if cur is not None:
            return _impl(cur)
        from core.db import db_conn
        with db_conn() as conn:
            c = conn.cursor()
            inserted = _impl(c)
            conn.commit()
            return inserted
    except Exception as exc:
        LOGGER.warning("resolve_hunter_evaluation %s: %s", evaluation_id, str(exc)[:160])
        return False


def recent_evaluations(symbol: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
    """Read recent Hunter evaluations (with full resolution evidence)."""
    lim = max(1, min(200, int(limit)))
    cols = (
        "e.id, e.symbol, e.scoring_version, e.session_date, e.issued_ts, "
        "e.feature_available_ts, e.reference_price, e.reference_price_ts, "
        "e.fuel_score, e.trigger_score, e.confirmation_score, "
        "e.squeeze_pressure_score, e.pressure_band, e.stage, e.explosion_score, "
        "e.planning_levels, "
        "r.return_1d_pct, r.return_5d_pct, r.return_14d_pct, "
        "r.hit_plus_20, r.hit_plus_50, r.hit_plus_100, r.hit_minus_20, "
        "r.max_favorable_pct, r.max_adverse_pct, r.reason"
    )
    keys = ("id", "symbol", "scoring_version", "session_date", "issued_ts",
            "feature_available_ts", "reference_price", "reference_price_ts",
            "fuel_score", "trigger_score", "confirmation_score",
            "squeeze_pressure_score", "pressure_band", "stage", "explosion_score",
            "planning_levels", "return_1d_pct", "return_5d_pct", "return_14d_pct",
            "hit_plus_20", "hit_plus_50", "hit_plus_100", "hit_minus_20",
            "max_favorable_pct", "max_adverse_pct", "reason")
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            if symbol:
                cur.execute(
                    f"""
                    SELECT {cols}
                    FROM ghost_squeeze_hunter_evaluations e
                    LEFT JOIN ghost_squeeze_hunter_resolutions r ON r.evaluation_id = e.id
                    WHERE e.symbol = %s
                    ORDER BY e.issued_ts DESC LIMIT %s
                    """,
                    (symbol.upper(), lim),
                )
            else:
                cur.execute(
                    f"""
                    SELECT {cols}
                    FROM ghost_squeeze_hunter_evaluations e
                    LEFT JOIN ghost_squeeze_hunter_resolutions r ON r.evaluation_id = e.id
                    ORDER BY e.issued_ts DESC LIMIT %s
                    """,
                    (lim,),
                )
            rows = cur.fetchall()
        return {"ok": True, "rows": [dict(zip(keys, r)) for r in rows]}
    except Exception as exc:
        LOGGER.warning("recent_evaluations: %s", str(exc)[:160])
        return {"ok": False, "error": "database_unavailable", "rows": []}


# ── Resolver job ───────────────────────────────────────────────────────────

def _ohlc_series(symbol: str, period: str = "3mo") -> list:
    """Realized daily OHLC bars, newest last. Best-effort via signal_engine."""
    try:
        from core.signal_engine import _fetch_ohlcv
        bars = _fetch_ohlcv(symbol.upper(), "stock", period=period) or []
        out = []
        for b in bars:
            o, h, lo, c = b.get("open"), b.get("high"), b.get("low"), b.get("close")
            ts = b.get("ts")
            if None in (o, h, lo, c):
                continue
            out.append({"ts": ts, "open": float(o), "high": float(h),
                        "low": float(lo), "close": float(c)})
        return out
    except Exception as exc:
        LOGGER.debug("_ohlc_series %s: %s", symbol, str(exc)[:80])
        return []


def _bar_epoch(bar: Dict[str, Any]) -> Optional[int]:
    ts = bar.get("ts")
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return int(ts)
    s = str(ts)
    try:
        from datetime import datetime, timezone
        if "T" in s:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(s[:10]).replace(tzinfo=timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def _session_date(ts: int) -> Optional[str]:
    """Exchange-session date (America/Chicago) for a timestamp.

    Used instead of a fixed 6-hour offset so day-zero/day-one alignment does
    not depend on the data provider's bar timestamp convention.
    """
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from core.market_hours import SESSION_TZ
        return datetime.fromtimestamp(int(ts), ZoneInfo(SESSION_TZ)).date().isoformat()
    except Exception:
        return None


def _bar_session_date(bar: Dict[str, Any]) -> Optional[str]:
    """The exchange session date for a daily bar, from its DATE LABEL.

    Daily bars from Stooq are normalized to midnight UTC ("YYYY-MM-DDT00:00:00Z"),
    which timezone-converts to the PREVIOUS day in America/Chicago. The date
    label is the canonical session date, so we read it directly instead of
    converting the midnight timestamp. Falls back to _session_date for bars
    that carry a real intraday timestamp.
    """
    ts = bar.get("ts")
    if isinstance(ts, str):
        # A date-only or midnight-UTC daily label: the YYYY-MM-DD prefix IS the
        # session date. Do not timezone-shift it.
        s = ts.strip()
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return s[:10]
    e = _bar_epoch(bar)
    if e is None:
        return None
    return _session_date(e)


def _bars_after(series: list, issued_ts: int) -> list:
    """Trading-day bars strictly after the evaluation's session date, in order.

    Day-zero = the session date the evaluation was issued on. Forward bars are
    bars whose session date (from the bar's date label) is strictly later.
    This is provider-independent: it keys on the exchange session date, not a
    raw timestamp offset or a timezone-shifted midnight label.
    """
    eval_date = _session_date(issued_ts)
    if eval_date is None:
        return []
    out = []
    for b in series:
        bd = _bar_session_date(b)
        if bd is None:
            continue
        if bd > eval_date:
            out.append(b)
    return out


def _resolve_one(eval_id: int, symbol: str, issued_ts: int, ref: Optional[float],
                 series: list, now: int) -> Optional[Dict[str, Any]]:
    """Compute a FULL 1/5/14-day resolution for one evaluation.

    Returns None (no resolution yet) unless all 14 forward bars are available,
    so a partial resolution is never written and later runs are not blocked by
    a prematurely-inserted row. hit_plus_20/50/100 and hit_minus_20 are only
    asserted once the full 14-day window has elapsed.
    """
    if ref is None or ref <= 0:
        return None
    fwd = _bars_after(series, issued_ts)
    if len(fwd) < 14:
        return None  # not enough forward bars yet — wait for a later run

    def _ret_at(idx: int) -> Optional[float]:
        px = fwd[idx].get("close")
        if px is not None:
            return round((float(px) - ref) / ref * 100.0, 3)
        return None

    r1 = _ret_at(0)
    r5 = _ret_at(4)
    r14 = _ret_at(13)

    window = fwd[:14]
    highs = [float(b["high"]) for b in window if b.get("high") is not None]
    lows = [float(b["low"]) for b in window if b.get("low") is not None]
    max_fav = round((max(highs) - ref) / ref * 100.0, 3) if highs else None
    max_adv = round((min(lows) - ref) / ref * 100.0, 3) if lows else None

    return {
        "evaluation_id": eval_id,
        "return_1d_pct": r1,
        "return_5d_pct": r5,
        "return_14d_pct": r14,
        "hit_plus_20": (max_fav is not None and max_fav >= 20.0),
        "hit_plus_50": (max_fav is not None and max_fav >= 50.0),
        "hit_plus_100": (max_fav is not None and max_fav >= 100.0),
        "hit_minus_20": (max_adv is not None and max_adv <= -20.0),
        "max_favorable_pct": max_fav,
        "max_adverse_pct": max_adv,
        "resolved_ts": now,
        "evidence_available_ts": now,
    }


def _resolve_one_row(cur, rec: Dict[str, Any], sym: str, series: list, now: int) -> str:
    """Resolve a single Hunter evaluation row. Returns 'resolved', 'terminal', or 'skip'."""
    # Terminal: no reference price → can never resolve.
    if rec["reference_price"] is None or rec["reference_price"] <= 0:
        resolve_hunter_evaluation(
            evaluation_id=rec["id"],
            reason="missing_reference_price",
            resolved_ts=now,
            evidence_available_ts=now,
            cur=cur,
        )
        return "terminal"
    # Transient vs permanent history failure. An empty series right now is
    # usually a provider outage / rate limit / breaker — NOT terminal. Only
    # mark terminal once the full 14-day horizon PLUS a grace period has
    # elapsed and we still have no bars (then it is genuinely unresolvable).
    if not series:
        horizon_elapsed = now - rec["issued_ts"] >= (14 + 5) * 86400
        if horizon_elapsed:
            resolve_hunter_evaluation(
                evaluation_id=rec["id"],
                reason="history_permanently_unavailable",
                resolved_ts=now,
                evidence_available_ts=now,
                cur=cur,
            )
            return "terminal"
        return "skip"  # transient — retry on a later run
    upd = _resolve_one(rec["id"], sym, rec["issued_ts"],
                       rec["reference_price"], series, now)
    if not upd:
        return "skip"  # not enough forward bars yet
    inserted = resolve_hunter_evaluation(
        evaluation_id=upd["evaluation_id"],
        return_1d_pct=upd["return_1d_pct"],
        return_5d_pct=upd["return_5d_pct"],
        return_14d_pct=upd["return_14d_pct"],
        hit_plus_20=upd["hit_plus_20"],
        hit_plus_50=upd["hit_plus_50"],
        hit_plus_100=upd["hit_plus_100"],
        hit_minus_20=upd["hit_minus_20"],
        max_favorable_pct=upd["max_favorable_pct"],
        max_adverse_pct=upd["max_adverse_pct"],
        resolved_ts=upd["resolved_ts"],
        evidence_available_ts=upd["evidence_available_ts"],
        cur=cur,
    )
    return "resolved" if inserted else "skip"


def resolve_hunter_predictions(*, limit: int = 200, now: Optional[int] = None) -> Dict[str, Any]:
    """Resolve unresolved Hunter evaluations against realized prices.

    Only writes a resolution once all 14 forward bars exist (no partial rows).
    Evaluations with a missing reference price or no historical data are marked
    terminal so they never block the queue. Groups by symbol (one price fetch
    per symbol). Returns a summary.
    """
    now = int(now or _now())
    resolved = 0
    terminal = 0
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT e.id, e.symbol, e.issued_ts, e.reference_price
                FROM ghost_squeeze_hunter_evaluations e
                LEFT JOIN ghost_squeeze_hunter_resolutions r ON r.evaluation_id = e.id
                WHERE r.id IS NULL
                ORDER BY e.issued_ts ASC
                LIMIT %s
                """,
                (max(1, min(1000, int(limit))),),
            )
            rows = cur.fetchall()
            by_symbol: Dict[str, list] = {}
            for r in rows:
                rec = {"id": r[0], "symbol": (r[1] or "").upper(),
                       "issued_ts": r[2], "reference_price": r[3]}
                by_symbol.setdefault(rec["symbol"], []).append(rec)

            for sym, recs in by_symbol.items():
                series = _ohlc_series(sym, period="3mo")
                for rec in recs:
                    # Per-row savepoint: one bad row must not roll back the
                    # whole batch (forensic SQ-2). Each resolution commits or
                    # rolls back independently.
                    cur.execute("SAVEPOINT hunter_resolve_row")
                    try:
                        result = _resolve_one_row(cur, rec, sym, series, now)
                        cur.execute("RELEASE SAVEPOINT hunter_resolve_row")
                        if result == "resolved":
                            resolved += 1
                        elif result == "terminal":
                            terminal += 1
                    except Exception as _re:
                        try:
                            cur.execute("ROLLBACK TO SAVEPOINT hunter_resolve_row")
                            cur.execute("RELEASE SAVEPOINT hunter_resolve_row")
                        except Exception:
                            pass
                        LOGGER.warning("hunter resolve row %s failed: %s", rec["id"], str(_re)[:120])
            conn.commit()
    except Exception as exc:
        LOGGER.warning("resolve_hunter_predictions: %s", str(exc)[:160])
        return {"ok": False, "error": "database_unavailable", "resolved": 0, "terminal": 0}
    return {"ok": True, "resolved": resolved, "terminal": terminal}


# ── Scheduled issuance (preregistered sampler) ─────────────────────────────

def _session_date_key(now_ts: Optional[int] = None) -> Optional[str]:
    """The exchange session date (America/Chicago) if it's a trading day.

    Returns None on weekends / market-closed so no samples are issued then.
    """
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from core.market_hours import SESSION_TZ, is_us_extended_hours
        ts = int(now_ts or _now())
        dt = datetime.fromtimestamp(ts, ZoneInfo(SESSION_TZ))
        if not is_us_extended_hours(dt):
            return None
        return dt.date().isoformat()
    except Exception:
        return None


# Fixed sampling window: issue only after the cash close (15:00 CT) and before
# 16:00 CT, so every day's sample is drawn from the same post-close population.
# This freezes the sampling time so a restart at 3 AM vs noon does not produce
# fundamentally different prediction populations under the same session_date.
_SAMPLE_WINDOW_START_MIN = 15 * 60 + 5   # 15:05 CT
_SAMPLE_WINDOW_END_MIN = 16 * 60         # 16:00 CT


def _in_sampling_window(now_ts: Optional[int] = None) -> bool:
    """True only during the frozen post-close sampling window (15:05–16:00 CT)."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from core.market_hours import SESSION_TZ
        ts = int(now_ts or _now())
        dt = datetime.fromtimestamp(ts, ZoneInfo(SESSION_TZ))
        hm = dt.hour * 60 + dt.minute
        return _SAMPLE_WINDOW_START_MIN <= hm < _SAMPLE_WINDOW_END_MIN
    except Exception:
        return False


def _existing_session_symbols(session_date: str) -> set[str]:
    """Read persisted keys before vendor work so retries only fetch missing rows."""
    try:
        from core.db import db_conn

        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT symbol FROM ghost_squeeze_hunter_evaluations "
                "WHERE scoring_version=%s AND session_date=%s",
                (HUNTER_SCORING_VERSION, session_date),
            )
            return {str(row[0]).upper() for row in (cur.fetchall() or []) if row and row[0]}
    except Exception as exc:
        LOGGER.debug("Hunter issuance preflight unavailable: %s", str(exc)[:120])
        return set()


def issue_hunter_samples(*, symbols: Optional[list] = None, now_ts: Optional[int] = None) -> Dict[str, Any]:
    """Preregistered sampler: write ONE evaluation per symbol per session date.

    This is the ONLY path that should persist Hunter evaluations. It issues
    only during the frozen post-close window (15:05–16:00 CT) so every day's
    sample is drawn from the same population. It uses the ACTUAL issuance time
    (honest `issued_ts`) and the stable `session_date` for the idempotency key.
    Samples with a missing reference price are NOT persisted (counted as
    `invalid_reference`, not `inserted`).
    """
    date_key = _session_date_key(now_ts)
    if date_key is None:
        return {"ok": True, "attempted": 0, "inserted": 0, "session_date": None,
                "note": "market closed or weekend"}
    if not _in_sampling_window(now_ts):
        return {"ok": True, "attempted": 0, "inserted": 0, "session_date": date_key,
                "note": "outside the frozen 15:05-16:00 CT sampling window"}
    if symbols is None:
        try:
            from config.symbols import watchlist_symbols
            symbols = sorted(watchlist_symbols())
        except Exception:
            symbols = []
    now = int(now_ts or _now())
    existing = _existing_session_symbols(date_key)

    attempted = 0
    inserted = 0
    duplicate = 0
    invalid_reference = 0
    persistence_failed = 0
    for sym in symbols:
        sym = str(sym).strip().upper()
        if sym in existing:
            duplicate += 1
            continue
        attempted += 1
        try:
            from core.squeeze_hunter import fetch_explosion_report
            rep = fetch_explosion_report(sym, persist=True, issued_ts=now)
            status = (rep.get("persistence") or {}).get("status")
            if status == "inserted":
                inserted += 1
            elif status == "duplicate":
                duplicate += 1
            elif status == "invalid_reference":
                invalid_reference += 1
            else:
                persistence_failed += 1
        except Exception:
            persistence_failed += 1
    return {
        "ok": persistence_failed == 0,
        "requested": len(symbols),
        "attempted": attempted,
        "inserted": inserted,
        "duplicate": duplicate,
        "invalid_reference": invalid_reference,
        "persistence_failed": persistence_failed,
        "session_date": date_key,
    }

