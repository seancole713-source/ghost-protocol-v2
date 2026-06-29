"""Squeeze daily log — persist predictions and resolve vs session OHLC at EOD.

Records Telegram squeeze alerts (and optional first candidate snapshot per symbol/day),
then after cash close compares Ghost buy/sell/stop to realized open/high/low/close.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

LOGGER = logging.getLogger("ghost.squeeze_outcomes")


def squeeze_log_enabled() -> bool:
    return os.getenv("SQUEEZE_DAILY_LOG", "1").strip().lower() in ("1", "true", "yes", "on")


def _ct_date(ts: Optional[int] = None) -> str:
    ts = int(ts or time.time())
    try:
        import pytz

        tz = pytz.timezone(os.getenv("GHOST_TZ", "America/Chicago"))
        return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")
    except Exception:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _parse_bar_date(ts: str) -> str:
    if not ts:
        return ""
    s = str(ts).strip()
    try:
        from zoneinfo import ZoneInfo

        if "T" in s:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        pass
    return s.split("T")[0] if "T" in s else s[:10]


def _coerce_json(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return v


def ensure_squeeze_outcomes_table(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_squeeze_outcomes (
            id SERIAL PRIMARY KEY,
            session_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            kind TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'telegram',
            alerted_at BIGINT NOT NULL,
            buy FLOAT,
            sell FLOAT,
            stop FLOAT,
            squeeze_score INT,
            setup_score INT,
            trigger_score INT,
            confirm_score INT,
            p_continue_3pct_60m FLOAT,
            confidence_pct FLOAT,
            rvol FLOAT,
            peak_move_pct FLOAT,
            above_vwap BOOLEAN,
            payload JSONB,
            outcome TEXT,
            session_open FLOAT,
            session_high FLOAT,
            session_low FLOAT,
            session_close FLOAT,
            hit_target BOOLEAN,
            hit_stop BOOLEAN,
            hit_3pct BOOLEAN,
            close_pnl_pct FLOAT,
            target_gap_pct FLOAT,
            precision_score FLOAT,
            precision_grade VARCHAR(4),
            mistake_type VARCHAR(64),
            precision_json JSONB,
            resolved_at BIGINT,
            created_at BIGINT NOT NULL
        )
        """
    )
    # Existing production tables need additive migrations because CREATE TABLE
    # IF NOT EXISTS will not add new PR #102 precision columns.
    for sql in (
        "ALTER TABLE ghost_squeeze_outcomes ADD COLUMN IF NOT EXISTS precision_score FLOAT",
        "ALTER TABLE ghost_squeeze_outcomes ADD COLUMN IF NOT EXISTS precision_grade VARCHAR(4)",
        "ALTER TABLE ghost_squeeze_outcomes ADD COLUMN IF NOT EXISTS mistake_type VARCHAR(64)",
        "ALTER TABLE ghost_squeeze_outcomes ADD COLUMN IF NOT EXISTS precision_json JSONB",
    ):
        cur.execute(sql)
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_squeeze_outcomes_session
        ON ghost_squeeze_outcomes (session_date DESC, alerted_at DESC)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_squeeze_outcomes_pending
        ON ghost_squeeze_outcomes (session_date)
        WHERE outcome IS NULL
        """
    )


def _pick_fields(pick: Dict[str, Any]) -> Dict[str, Any]:
    probs = pick.get("probabilities") or {}
    return {
        "symbol": (pick.get("symbol") or "").upper(),
        "kind": pick.get("kind") or "squeeze_forming",
        "buy": pick.get("buy"),
        "sell": pick.get("sell"),
        "stop": pick.get("stop"),
        "squeeze_score": pick.get("squeeze_score"),
        "setup_score": pick.get("setup_score"),
        "trigger_score": pick.get("trigger_score"),
        "confirm_score": pick.get("confirm_score"),
        "p_continue_3pct_60m": probs.get("p_continue_3pct_60m"),
        "confidence_pct": pick.get("confidence_pct"),
        "rvol": pick.get("rvol"),
        "peak_move_pct": pick.get("peak_move_pct"),
        "above_vwap": pick.get("above_vwap"),
        "payload": pick,
    }


def record_squeeze_prediction(
    pick: Dict[str, Any],
    *,
    source: str = "telegram",
    alerted_at: Optional[int] = None,
) -> Optional[int]:
    """Insert one squeeze prediction row (Telegram alert or first candidate snapshot)."""
    if not squeeze_log_enabled():
        return None
    sym = (pick.get("symbol") or "").upper()
    if not sym:
        return None
    ts = int(alerted_at or pick.get("alerted_at") or time.time())
    session_date = _ct_date(ts)
    fields = _pick_fields(pick)
    try:
        from core.db import db_conn

        with db_conn() as conn:
            cur = conn.cursor()
            ensure_squeeze_outcomes_table(cur)
            if source == "candidate":
                cur.execute(
                    """
                    SELECT id FROM ghost_squeeze_outcomes
                    WHERE session_date = %s AND symbol = %s AND source = 'candidate'
                    LIMIT 1
                    """,
                    (session_date, sym),
                )
                if cur.fetchone():
                    return None
            cur.execute(
                """
                INSERT INTO ghost_squeeze_outcomes (
                    session_date, symbol, kind, source, alerted_at,
                    buy, sell, stop, squeeze_score, setup_score, trigger_score,
                    confirm_score, p_continue_3pct_60m, confidence_pct, rvol,
                    peak_move_pct, above_vwap, payload, created_at
                ) VALUES (
                    %s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,
                    %s,%s,%s::jsonb,%s
                )
                RETURNING id
                """,
                (
                    session_date,
                    sym,
                    fields["kind"],
                    source,
                    ts,
                    fields["buy"],
                    fields["sell"],
                    fields["stop"],
                    fields["squeeze_score"],
                    fields["setup_score"],
                    fields["trigger_score"],
                    fields["confirm_score"],
                    fields["p_continue_3pct_60m"],
                    fields["confidence_pct"],
                    fields["rvol"],
                    fields["peak_move_pct"],
                    fields["above_vwap"],
                    json.dumps(fields["payload"], default=str),
                    int(time.time()),
                ),
            )
            row = cur.fetchone()
            return int(row[0]) if row else None
    except Exception as exc:
        LOGGER.warning("record_squeeze_prediction %s: %s", sym, str(exc)[:120])
        return None


def _session_ohlc(symbol: str, session_date: str) -> Optional[Dict[str, float]]:
    """RTH daily bar OHLC for an exchange session date (YYYY-MM-DD)."""
    try:
        from core.signal_engine import _fetch_ohlcv

        bars = _fetch_ohlcv(symbol.upper(), "stock", period="3m") or []
        for bar in reversed(bars):
            if _parse_bar_date(str(bar.get("ts") or "")) != session_date:
                continue
            o, h, l, c = bar.get("open"), bar.get("high"), bar.get("low"), bar.get("close")
            if None in (o, h, l, c):
                continue
            if float(o) <= 0 or float(h) <= 0:
                continue
            return {
                "open": round(float(o), 4),
                "high": round(float(h), 4),
                "low": round(float(l), 4),
                "close": round(float(c), 4),
            }
    except Exception as exc:
        LOGGER.debug("session_ohlc %s %s: %s", symbol, session_date, str(exc)[:80])
    return None


def _resolve_row(
    buy: float,
    sell: float,
    stop: Optional[float],
    ohlc: Dict[str, float],
) -> Dict[str, Any]:
    o = ohlc["open"]
    h = ohlc["high"]
    l = ohlc["low"]
    c = ohlc["close"]
    hit_target = h >= float(sell) if sell else False
    hit_stop = stop is not None and l <= float(stop)
    hit_3pct = h >= float(buy) * 1.03 if buy else False
    if hit_target and not hit_stop:
        outcome = "WIN"
    elif hit_stop and not hit_target:
        outcome = "LOSS"
    elif hit_target and hit_stop:
        outcome = "MIXED"
    else:
        outcome = "NEUTRAL"
    close_pnl = ((c - float(buy)) / float(buy) * 100.0) if buy else None
    target_gap = ((c - float(sell)) / float(sell) * 100.0) if sell else None
    try:
        from core.ghost_precision import score_trade_precision

        precision = score_trade_precision(
            direction="UP",
            entry=buy,
            target=sell,
            stop=stop,
            live_open=o,
            live_low=l,
            live_high=h,
            live_close=c,
        )
    except Exception:
        precision = {}
    return {
        "outcome": outcome,
        "session_open": o,
        "session_high": h,
        "session_low": l,
        "session_close": c,
        "hit_target": hit_target,
        "hit_stop": hit_stop,
        "hit_3pct": hit_3pct,
        "close_pnl_pct": round(close_pnl, 3) if close_pnl is not None else None,
        "target_gap_pct": round(target_gap, 3) if target_gap is not None else None,
        "precision_score": precision.get("precision_score"),
        "precision_grade": precision.get("precision_grade"),
        "mistake_type": precision.get("mistake_type"),
        "precision": precision,
    }


def resolve_squeeze_outcomes(session_date: Optional[str] = None) -> int:
    """Resolve pending rows for a CT session date (default: today if after cash close)."""
    if not squeeze_log_enabled():
        return 0
    target = session_date or _ct_date()
    resolved = 0
    try:
        from core.db import db_conn

        with db_conn() as conn:
            cur = conn.cursor()
            ensure_squeeze_outcomes_table(cur)
            cur.execute(
                """
                SELECT id, symbol, buy, sell, stop
                FROM ghost_squeeze_outcomes
                WHERE session_date = %s AND outcome IS NULL
                ORDER BY alerted_at ASC
                """,
                (target,),
            )
            rows = cur.fetchall()
            now = int(time.time())
            for rid, sym, buy, sell, stop in rows:
                if buy is None or sell is None:
                    continue
                ohlc = _session_ohlc(str(sym), target)
                if not ohlc:
                    continue
                meta = _resolve_row(float(buy), float(sell), stop, ohlc)
                cur.execute(
                    """
                    UPDATE ghost_squeeze_outcomes SET
                        outcome = %s,
                        session_open = %s,
                        session_high = %s,
                        session_low = %s,
                        session_close = %s,
                        hit_target = %s,
                        hit_stop = %s,
                        hit_3pct = %s,
                        close_pnl_pct = %s,
                        target_gap_pct = %s,
                        precision_score = %s,
                        precision_grade = %s,
                        mistake_type = %s,
                        precision_json = %s::jsonb,
                        resolved_at = %s
                    WHERE id = %s
                    """,
                    (
                        meta["outcome"],
                        meta["session_open"],
                        meta["session_high"],
                        meta["session_low"],
                        meta["session_close"],
                        meta["hit_target"],
                        meta["hit_stop"],
                        meta["hit_3pct"],
                        meta["close_pnl_pct"],
                        meta["target_gap_pct"],
                        meta.get("precision_score"),
                        meta.get("precision_grade"),
                        meta.get("mistake_type"),
                        json.dumps(meta.get("precision") or {}, default=str),
                        now,
                        rid,
                    ),
                )
                resolved += 1
    except Exception as exc:
        LOGGER.warning("resolve_squeeze_outcomes: %s", str(exc)[:160])
    if resolved:
        LOGGER.info("[SqueezeOutcomes] resolved %s rows for %s", resolved, target)
    return resolved


def resolve_pending_squeeze_days(max_days: int = 7) -> int:
    """Resolve any pending squeeze rows for recent session dates."""
    total = 0
    try:
        from core.db import db_conn

        with db_conn() as conn:
            cur = conn.cursor()
            ensure_squeeze_outcomes_table(cur)
            cur.execute(
                """
                SELECT DISTINCT session_date FROM ghost_squeeze_outcomes
                WHERE outcome IS NULL
                ORDER BY session_date DESC
                LIMIT %s
                """,
                (max(1, max_days),),
            )
            dates = [r[0] for r in cur.fetchall()]
        for d in dates:
            total += resolve_squeeze_outcomes(d)
    except Exception as exc:
        LOGGER.warning("resolve_pending_squeeze_days: %s", str(exc)[:120])
    return total


def squeeze_daily_log(
    *,
    session_date: Optional[str] = None,
    days: int = 14,
) -> Dict[str, Any]:
    """API payload: predictions vs realized session OHLC for one day or recent history."""
    if not squeeze_log_enabled():
        return {"ok": True, "enabled": False, "rows": [], "days": []}
    days = max(1, min(90, int(days)))
    try:
        from core.db import db_conn

        with db_conn() as conn:
            cur = conn.cursor()
            ensure_squeeze_outcomes_table(cur)
            if session_date:
                cur.execute(
                    """
                    SELECT id, session_date, symbol, kind, source, alerted_at,
                           buy, sell, stop, squeeze_score, p_continue_3pct_60m,
                           confidence_pct, rvol, peak_move_pct,
                           outcome, session_open, session_high, session_low, session_close,
                           hit_target, hit_stop, hit_3pct, close_pnl_pct, target_gap_pct,
                           precision_score, precision_grade, mistake_type, precision_json,
                           resolved_at
                    FROM ghost_squeeze_outcomes
                    WHERE session_date = %s
                    ORDER BY alerted_at ASC
                    """,
                    (session_date,),
                )
            else:
                try:
                    import pytz
                    from datetime import timedelta

                    tz = pytz.timezone(os.getenv("GHOST_TZ", "America/Chicago"))
                    cutoff = (datetime.now(tz) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
                except Exception:
                    cutoff = _ct_date()
                cur.execute(
                    """
                    SELECT id, session_date, symbol, kind, source, alerted_at,
                           buy, sell, stop, squeeze_score, p_continue_3pct_60m,
                           confidence_pct, rvol, peak_move_pct,
                           outcome, session_open, session_high, session_low, session_close,
                           hit_target, hit_stop, hit_3pct, close_pnl_pct, target_gap_pct,
                           precision_score, precision_grade, mistake_type, precision_json,
                           resolved_at
                    FROM ghost_squeeze_outcomes
                    WHERE session_date >= %s
                    ORDER BY session_date DESC, alerted_at ASC
                    LIMIT 500
                    """,
                    (cutoff,),
                )
            raw = cur.fetchall()
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160], "rows": []}

    rows: List[Dict[str, Any]] = []
    for r in raw:
        rows.append({
            "id": r[0],
            "session_date": r[1],
            "symbol": r[2],
            "kind": r[3],
            "source": r[4],
            "alerted_at": r[5],
            "buy": r[6],
            "sell": r[7],
            "stop": r[8],
            "squeeze_score": r[9],
            "p_continue_3pct_60m": r[10],
            "confidence_pct": r[11],
            "rvol": r[12],
            "peak_move_pct": r[13],
            "outcome": r[14],
            "session_open": r[15],
            "session_high": r[16],
            "session_low": r[17],
            "session_close": r[18],
            "hit_target": r[19],
            "hit_stop": r[20],
            "hit_3pct": r[21],
            "close_pnl_pct": r[22],
            "target_gap_pct": r[23],
            "precision_score": r[24],
            "precision_grade": r[25],
            "mistake_type": r[26],
            "precision": _coerce_json(r[27]) if r[27] is not None else None,
            "resolved_at": r[28],
        })

    # Backfill API payloads for older rows that predate PR #102 without writing
    # to the DB. This keeps the UI honest immediately after deploy.
    try:
        from core.ghost_precision import score_trade_precision

        for row in rows:
            if row.get("precision") or row.get("session_open") is None:
                continue
            precision = score_trade_precision(
                direction="UP",
                entry=row.get("buy"),
                target=row.get("sell"),
                stop=row.get("stop"),
                live_open=row.get("session_open"),
                live_low=row.get("session_low"),
                live_high=row.get("session_high"),
                live_close=row.get("session_close"),
            )
            row["precision"] = precision
            row["precision_score"] = precision.get("precision_score")
            row["precision_grade"] = precision.get("precision_grade")
            row["mistake_type"] = precision.get("mistake_type")
    except Exception as exc:
        LOGGER.debug("squeeze precision backfill: %s", str(exc)[:80])

    try:
        from core.squeeze_live_drift import enrich_daily_log_rows

        rows = enrich_daily_log_rows(rows)
    except Exception as exc:
        LOGGER.debug("enrich_daily_log_rows: %s", str(exc)[:80])

    by_day: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        by_day.setdefault(row["session_date"], []).append(row)

    summaries = []
    for day, day_rows in sorted(by_day.items(), reverse=True):
        resolved = [x for x in day_rows if x.get("outcome")]
        wins = sum(1 for x in resolved if x.get("outcome") == "WIN")
        losses = sum(1 for x in resolved if x.get("outcome") == "LOSS")
        pending = sum(1 for x in day_rows if not x.get("outcome"))
        summaries.append({
            "session_date": day,
            "count": len(day_rows),
            "resolved": len(resolved),
            "pending": pending,
            "wins": wins,
            "losses": losses,
            "telegram": sum(1 for x in day_rows if x.get("source") == "telegram"),
        })

    today = _ct_date()
    focus = session_date or today
    focus_rows = by_day.get(focus, rows if session_date else [])

    live_drift: List[Dict[str, Any]] = []
    seen_syms = set()
    for r in sorted(rows, key=lambda x: int(x.get("alerted_at") or 0)):
        if r.get("outcome") or r.get("source") != "telegram":
            continue
        sym = (r.get("symbol") or "").upper()
        if not sym or sym in seen_syms or r.get("gap_pct") is None:
            continue
        seen_syms.add(sym)
        live_drift.append({
            "symbol": sym,
            "kind": r.get("kind"),
            "alert_buy": r.get("alert_buy") or r.get("buy"),
            "live_price": r.get("live_price"),
            "gap_pct": r.get("gap_pct"),
            "gap_label": r.get("gap_label"),
            "drift_status": r.get("drift_status"),
        })
    live_drift.sort(key=lambda x: x.get("gap_pct") or 0)

    return {
        "ok": True,
        "enabled": True,
        "session_date": focus,
        "today_ct": today,
        "rows": focus_rows if session_date else rows[:200],
        "days": summaries,
        "live_drift": live_drift,
        "note": (
            "Ghost squeeze buy/sell/stop at alert time vs cash-session OHLC. "
            "WIN = session high reached sell target; LOSS = session low hit stop. "
            "Live drift = first Telegram alert buy vs quote now (intraday)."
        ),
    }


def run_squeeze_eod_job() -> Dict[str, Any]:
    """Scheduler hook: resolve today's squeeze log after cash close."""
    if not squeeze_log_enabled():
        return {"ok": True, "skipped": True}
    hour = int(os.getenv("SQUEEZE_EOD_HOUR", os.getenv("DAILY_SUMMARY_HOUR", "16")))
    try:
        import pytz

        now_ct = datetime.now(pytz.timezone(os.getenv("GHOST_TZ", "America/Chicago")))
        if now_ct.hour < hour:
            return {"ok": True, "skipped": True, "reason": "before_eod_hour"}
    except Exception:
        pass
    session_date = _ct_date()
    n = resolve_squeeze_outcomes(session_date)
    n += resolve_pending_squeeze_days(5)
    return {"ok": True, "resolved": n, "session_date": session_date}
