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
    """Create the Hunter evaluation + resolution tables. Idempotent."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_squeeze_hunter_evaluations (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            scoring_version VARCHAR(16) NOT NULL,
            issued_ts BIGINT NOT NULL,
            feature_available_ts BIGINT,
            reference_price FLOAT,
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
            created_at BIGINT NOT NULL,
            UNIQUE (symbol, scoring_version, issued_ts)
        )
        """
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
            hit_minus_20 BOOLEAN,
            reason VARCHAR(200),
            created_at BIGINT NOT NULL
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_hunter_res_eval "
        "ON ghost_squeeze_hunter_resolutions (evaluation_id)"
    )


def log_hunter_evaluation(
    *,
    symbol: str,
    report: Dict[str, Any],
    short_ctx: Optional[Dict[str, Any]] = None,
    trigger_ctx: Optional[Dict[str, Any]] = None,
    confirm_ctx: Optional[Dict[str, Any]] = None,
    reference_price: Optional[float] = None,
    issued_ts: Optional[int] = None,
    feature_available_ts: Optional[int] = None,
    cur=None,
) -> Optional[int]:
    """Append one immutable Hunter evaluation. Returns row id or None.

    Idempotent by (symbol, scoring_version, issued_ts). Never raises into the
    caller — a persistence failure must not break the read-only report.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    ts = int(issued_ts or _now())
    fav = int(feature_available_ts) if feature_available_ts else ts

    def _impl(c) -> Optional[int]:
        c.execute(
            """
            INSERT INTO ghost_squeeze_hunter_evaluations
                (symbol, scoring_version, issued_ts, feature_available_ts,
                 reference_price,
                 fuel_score, trigger_score, confirmation_score,
                 squeeze_pressure_score, pressure_band, stage, explosion_score,
                 short_ctx, trigger_ctx, confirm_ctx, factors, projection, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s)
            ON CONFLICT (symbol, scoring_version, issued_ts) DO NOTHING
            RETURNING id
            """,
            (
                sym, HUNTER_SCORING_VERSION, ts, fav,
                reference_price,
                report.get("fuel_score"), report.get("trigger_score"),
                report.get("confirmation_score"),
                report.get("squeeze_pressure_score"),
                report.get("pressure_band"), report.get("stage"),
                report.get("explosion_score"),
                _jsonb(short_ctx), _jsonb(trigger_ctx), _jsonb(confirm_ctx),
                _jsonb(report.get("factors")), _jsonb(report.get("projection")),
                _now(),
            ),
        )
        row = c.fetchone()
        return int(row[0]) if row else None

    try:
        if cur is not None:
            return _impl(cur)
        from core.db import db_conn
        with db_conn() as conn:
            c = conn.cursor()
            ensure_hunter_tables(c)
            rid = _impl(c)
            conn.commit()
            return rid
    except Exception as exc:
        LOGGER.warning("log_hunter_evaluation %s: %s", sym, str(exc)[:160])
        return None


def resolve_hunter_evaluation(
    *,
    evaluation_id: int,
    return_1d_pct: Optional[float] = None,
    return_5d_pct: Optional[float] = None,
    return_14d_pct: Optional[float] = None,
    hit_plus_20: Optional[bool] = None,
    hit_minus_20: Optional[bool] = None,
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
                 hit_plus_20, hit_minus_20, reason, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (evaluation_id) DO NOTHING
            """,
            (
                evaluation_id, rts, eats,
                return_1d_pct, return_5d_pct, return_14d_pct,
                hit_plus_20, hit_minus_20, reason, now,
            ),
        )
        return c.rowcount > 0

    try:
        if cur is not None:
            return _impl(cur)
        from core.db import db_conn
        with db_conn() as conn:
            c = conn.cursor()
            ensure_hunter_tables(c)
            inserted = _impl(c)
            conn.commit()
            return inserted
    except Exception as exc:
        LOGGER.warning("resolve_hunter_evaluation %s: %s", evaluation_id, str(exc)[:160])
        return False


def recent_evaluations(symbol: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
    """Read recent Hunter evaluations (with resolutions) for inspection."""
    lim = max(1, min(200, int(limit)))
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_hunter_tables(cur)
            if symbol:
                cur.execute(
                    """
                    SELECT e.id, e.symbol, e.scoring_version, e.issued_ts,
                           e.fuel_score, e.trigger_score, e.confirmation_score,
                           e.squeeze_pressure_score, e.pressure_band, e.stage,
                           e.explosion_score, r.return_14d_pct, r.hit_plus_20, r.hit_minus_20
                    FROM ghost_squeeze_hunter_evaluations e
                    LEFT JOIN ghost_squeeze_hunter_resolutions r ON r.evaluation_id = e.id
                    WHERE e.symbol = %s
                    ORDER BY e.issued_ts DESC LIMIT %s
                    """,
                    (symbol.upper(), lim),
                )
            else:
                cur.execute(
                    """
                    SELECT e.id, e.symbol, e.scoring_version, e.issued_ts,
                           e.fuel_score, e.trigger_score, e.confirmation_score,
                           e.squeeze_pressure_score, e.pressure_band, e.stage,
                           e.explosion_score, r.return_14d_pct, r.hit_plus_20, r.hit_minus_20
                    FROM ghost_squeeze_hunter_evaluations e
                    LEFT JOIN ghost_squeeze_hunter_resolutions r ON r.evaluation_id = e.id
                    ORDER BY e.issued_ts DESC LIMIT %s
                    """,
                    (lim,),
                )
            rows = cur.fetchall()
        keys = ("id", "symbol", "scoring_version", "issued_ts",
                "fuel_score", "trigger_score", "confirmation_score",
                "squeeze_pressure_score", "pressure_band", "stage",
                "explosion_score", "return_14d_pct", "hit_plus_20", "hit_minus_20")
        return {"ok": True, "rows": [dict(zip(keys, r)) for r in rows]}
    except Exception as exc:
        LOGGER.warning("recent_evaluations: %s", str(exc)[:160])
        return {"ok": False, "error": str(exc)[:160], "rows": []}


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


def _bars_after(series: list, issued_ts: int) -> list:
    """Trading-day bars strictly after the evaluation timestamp, in order."""
    out = []
    for b in series:
        e = _bar_epoch(b)
        if e is None:
            continue
        if e > issued_ts + 6 * 3600:
            out.append(b)
    return out


def _resolve_one(eval_id: int, symbol: str, issued_ts: int, ref: Optional[float],
                 series: list, now: int) -> Optional[Dict[str, Any]]:
    """Compute 1/5/14-day returns + hit_plus_20/hit_minus_20 for one evaluation."""
    if ref is None or ref <= 0:
        return None
    fwd = _bars_after(series, issued_ts)
    if not fwd:
        return None

    def _ret_at(idx: int) -> Optional[float]:
        if len(fwd) > idx:
            px = fwd[idx].get("close")
            if px is not None:
                return round((float(px) - ref) / ref * 100.0, 3)
        return None

    r1 = _ret_at(0)
    r5 = _ret_at(4)
    r14 = _ret_at(13)

    # hit_plus_20 / hit_minus_20: did the price reach +20% / -20% from ref at
    # any point within the 14-day window (using high/low)?
    hit_plus_20 = None
    hit_minus_20 = None
    window = fwd[:14]
    if window:
        highs = [float(b["high"]) for b in window if b.get("high") is not None]
        lows = [float(b["low"]) for b in window if b.get("low") is not None]
        if highs:
            hit_plus_20 = max(highs) >= ref * 1.20
        if lows:
            hit_minus_20 = min(lows) <= ref * 0.80

    # Only resolve a horizon once enough bars exist; otherwise leave it null so
    # a later run can fill it in.
    return {
        "evaluation_id": eval_id,
        "return_1d_pct": r1,
        "return_5d_pct": r5,
        "return_14d_pct": r14,
        "hit_plus_20": hit_plus_20,
        "hit_minus_20": hit_minus_20,
        "resolved_ts": now,
        "evidence_available_ts": now,
    }


def resolve_hunter_predictions(*, limit: int = 200, now: Optional[int] = None) -> Dict[str, Any]:
    """Resolve unresolved Hunter evaluations against realized prices.

    Mirrors super_ghost_ledger.resolve_predictions: groups by symbol, fetches
    each price series once, and writes resolutions idempotently. Returns a
    summary. This is the scheduler hook for the Hunter's learning loop.
    """
    now = int(now or _now())
    resolved = 0
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_hunter_tables(cur)
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
                if not series:
                    continue
                for rec in recs:
                    upd = _resolve_one(rec["id"], sym, rec["issued_ts"],
                                       rec["reference_price"], series, now)
                    if not upd:
                        continue
                    inserted = resolve_hunter_evaluation(
                        evaluation_id=upd["evaluation_id"],
                        return_1d_pct=upd["return_1d_pct"],
                        return_5d_pct=upd["return_5d_pct"],
                        return_14d_pct=upd["return_14d_pct"],
                        hit_plus_20=upd["hit_plus_20"],
                        hit_minus_20=upd["hit_minus_20"],
                        resolved_ts=upd["resolved_ts"],
                        evidence_available_ts=upd["evidence_available_ts"],
                        cur=cur,
                    )
                    if inserted:
                        resolved += 1
            conn.commit()
    except Exception as exc:
        LOGGER.warning("resolve_hunter_predictions: %s", str(exc)[:160])
        return {"ok": False, "error": str(exc)[:160], "resolved": 0}
    return {"ok": True, "resolved": resolved}

