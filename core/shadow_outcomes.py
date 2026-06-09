"""Shadow scoring — resolve every silenced model evaluation against real prices.

Ghost evaluates the full watchlist every scan cycle but historically only
learned from the rare pick that cleared the gates. This module turns the
silenced evaluations (ghost_perf_symbol_evals) into virtual picks: one per
symbol per CT trading day, resolved with the exact same TP/SL bar-path rules
as live picks (core.tp_sl_resolve + core.vol_targets). The result is a
per-symbol live hit-rate scoreboard that accrues ~44 resolved virtual trades
per trading day without risking a dollar.

Shadow rows live in their own table (ghost_shadow_outcomes) so perf-log
pruning never erases the scoreboard history.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("ghost.shadow")

# Probability floor used for scoreboard bucketing (matches the live v3 BUY
# floor on current models). Bucket edges only — resolution does not gate.
PROB_FLOOR = 0.55


def shadow_enabled() -> bool:
    return (os.getenv("GHOST_SHADOW_SCORING", "on") or "on").strip().lower() not in (
        "0", "off", "false", "no",
    )


def ensure_shadow_table(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_shadow_outcomes (
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            eval_ts BIGINT NOT NULL,
            up_prob FLOAT,
            confidence FLOAT,
            skip_code TEXT,
            fired BOOLEAN NOT NULL DEFAULT FALSE,
            entry_price FLOAT NOT NULL,
            target_price FLOAT NOT NULL,
            stop_price FLOAT NOT NULL,
            expires_at BIGINT NOT NULL,
            outcome TEXT,
            exit_price FLOAT,
            pnl_pct FLOAT,
            resolved_at BIGINT,
            created_at BIGINT NOT NULL,
            UNIQUE (symbol, trade_date)
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_shadow_pending "
        "ON ghost_shadow_outcomes (symbol) WHERE outcome IS NULL"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_shadow_eval_ts "
        "ON ghost_shadow_outcomes (eval_ts DESC)"
    )


def _ct_date(ts: int) -> str:
    try:
        import pytz

        tz = pytz.timezone(os.getenv("GHOST_TZ", "America/Chicago"))
        return datetime.fromtimestamp(int(ts), tz).strftime("%Y-%m-%d")
    except Exception:
        return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d")


def pick_daily_first(evals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One eval per (symbol, CT trade date) — the earliest of the day.

    The models are daily; intraday repeats of the same symbol are
    pseudo-replicates that would inflate sample counts.
    """
    chosen: Dict[tuple, Dict[str, Any]] = {}
    for ev in evals:
        ts = ev.get("eval_ts")
        sym = str(ev.get("symbol") or "").upper()
        if not sym or not ts:
            continue
        key = (sym, _ct_date(int(ts)))
        prev = chosen.get(key)
        if prev is None or int(ts) < int(prev["eval_ts"]):
            chosen[key] = ev
    return list(chosen.values())


def _eval_entry_price(ev: Dict[str, Any]) -> Optional[float]:
    """Entry for the virtual trade: real entry when fired, else scan price."""
    try:
        p = float(ev.get("entry_price") or 0)
        if p > 0:
            return p
    except Exception:
        pass
    scores = ev.get("scores")
    if isinstance(scores, str):
        try:
            scores = json.loads(scores)
        except Exception:
            scores = None
    if isinstance(scores, dict):
        try:
            p = float(scores.get("price") or 0)
            if p > 0:
                return p
        except Exception:
            pass
    return None


def seed_shadow_rows(days_back: int = 3) -> int:
    """Insert pending shadow rows from recent symbol evals (idempotent)."""
    from core.db import db_conn

    cutoff = int(time.time()) - max(1, int(days_back)) * 86400
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_shadow_table(cur)
        try:
            cur.execute(
                "SELECT symbol, eval_ts, up_prob, confidence, skip_code, fired, "
                "entry_price, target_price, stop_price, scores "
                "FROM ghost_perf_symbol_evals "
                "WHERE eval_ts >= %s AND up_prob IS NOT NULL",
                (cutoff,),
            )
            rows = cur.fetchall()
        except Exception as e:
            LOGGER.debug("shadow seed: eval read failed: %s", str(e)[:80])
            return 0

        evals = [
            {
                "symbol": r[0], "eval_ts": r[1], "up_prob": r[2], "confidence": r[3],
                "skip_code": r[4], "fired": bool(r[5]), "entry_price": r[6],
                "target_price": r[7], "stop_price": r[8], "scores": r[9],
            }
            for r in rows
        ]
        from core.tp_sl_resolve import expires_at_nth_trading_close, label_hold_bars, tp_sl_prices_from_vol
        from core.vol_targets import base_vol_pct

        hold = label_hold_bars()
        now = int(time.time())
        inserted = 0
        # Drop evals without a resolvable entry first (rows logged before the
        # scan price was captured) so they can't shadow out priced ones as the
        # "earliest of the day".
        priced = [ev for ev in evals if _eval_entry_price(ev) is not None]
        for ev in pick_daily_first(priced):
            entry = _eval_entry_price(ev)
            if entry is None:
                continue
            sym = str(ev["symbol"]).upper()
            if ev.get("fired") and ev.get("target_price") and ev.get("stop_price"):
                target, stop = float(ev["target_price"]), float(ev["stop_price"])
            else:
                target, stop = tp_sl_prices_from_vol(entry, base_vol_pct(sym, "stock"), "UP")
            if target <= 0 or stop <= 0:
                continue
            eval_ts = int(ev["eval_ts"])
            cur.execute(
                """
                INSERT INTO ghost_shadow_outcomes
                    (symbol, trade_date, eval_ts, up_prob, confidence, skip_code, fired,
                     entry_price, target_price, stop_price, expires_at, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol, trade_date) DO NOTHING
                """,
                (
                    sym, _ct_date(eval_ts), eval_ts,
                    ev.get("up_prob"), ev.get("confidence"), ev.get("skip_code"),
                    bool(ev.get("fired")),
                    round(entry, 6), round(target, 6), round(stop, 6),
                    expires_at_nth_trading_close(eval_ts, hold), now,
                ),
            )
            inserted += cur.rowcount or 0
    if inserted:
        LOGGER.info("Shadow seed: %d new virtual picks", inserted)
    return inserted


def resolve_shadow_rows(max_symbols: int = 60) -> int:
    """Resolve pending shadow rows with the same bar-path rules as live picks."""
    from core.db import db_conn
    from core.pnl import resolution_exit
    from core.tp_sl_resolve import label_hold_bars, resolve_open_prediction

    now = int(time.time())
    hold = label_hold_bars()
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_shadow_table(cur)
        cur.execute(
            "SELECT id, symbol, eval_ts, entry_price, target_price, stop_price, expires_at "
            "FROM ghost_shadow_outcomes WHERE outcome IS NULL ORDER BY symbol, eval_ts"
        )
        pending = cur.fetchall()
    if not pending:
        return 0

    by_symbol: Dict[str, List[tuple]] = {}
    for row in pending:
        by_symbol.setdefault(str(row[1]).upper(), []).append(row)

    resolved = 0
    for i, (sym, rows) in enumerate(sorted(by_symbol.items())):
        if i >= max_symbols:
            break
        bars = None
        try:
            from core.signal_engine import _fetch_ohlcv

            bars = _fetch_ohlcv(sym, "stock", period="3m")
        except Exception as e:
            LOGGER.debug("shadow bars %s: %s", sym, str(e)[:80])
        if not bars:
            continue
        last_close = float(bars[-1].get("close") or 0)
        for (sid, _sym, eval_ts, entry, target, stop, expires_at) in rows:
            outcome = resolve_open_prediction(
                direction="UP",
                target=float(target),
                stop=float(stop),
                predicted_at=int(eval_ts),
                hold_bars=hold,
                daily_bars=bars,
                snapshot_price=None,
                now=now,
                expires_at=int(expires_at) if expires_at else None,
            )
            if not outcome:
                continue
            exit_price, pnl = resolution_exit(
                outcome, "UP", float(entry), float(target), float(stop),
                last_close if last_close > 0 else float(entry),
            )
            with db_conn() as conn:
                conn.cursor().execute(
                    "UPDATE ghost_shadow_outcomes "
                    "SET outcome=%s, exit_price=%s, pnl_pct=%s, resolved_at=%s WHERE id=%s",
                    (outcome, exit_price, pnl, now, sid),
                )
            resolved += 1
    if resolved:
        LOGGER.info("Shadow resolve: %d virtual picks resolved", resolved)
    return resolved


def _bucket_for(up_prob: Optional[float]) -> str:
    if up_prob is None:
        return "unknown"
    p = float(up_prob)
    if p >= PROB_FLOOR:
        return "fireable"      # >= 0.55 — would pass the prob gate
    if p >= 0.50:
        return "near"          # 0.50–0.55 — model leaning up, below floor
    return "weak"              # < 0.50 — model leaning down/flat


def aggregate_shadow_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pure aggregation: per-symbol records + prob-bucket calibration."""
    symbols: Dict[str, Dict[str, Any]] = {}
    buckets: Dict[str, Dict[str, Any]] = {}
    pending = 0
    for r in rows:
        outcome = r.get("outcome")
        if outcome is None:
            pending += 1
            continue
        sym = str(r.get("symbol") or "").upper()
        s = symbols.setdefault(sym, {
            "symbol": sym, "n": 0, "wins": 0, "losses": 0, "expired": 0,
            "pnl_pct_sum": 0.0, "last_outcome": None, "last_eval_ts": 0,
        })
        s["n"] += 1
        if outcome == "WIN":
            s["wins"] += 1
        elif outcome == "LOSS":
            s["losses"] += 1
        else:
            s["expired"] += 1
        s["pnl_pct_sum"] += float(r.get("pnl_pct") or 0)
        if int(r.get("eval_ts") or 0) >= s["last_eval_ts"]:
            s["last_eval_ts"] = int(r.get("eval_ts") or 0)
            s["last_outcome"] = outcome

        b = buckets.setdefault(_bucket_for(r.get("up_prob")), {
            "n": 0, "wins": 0, "losses": 0, "expired": 0,
        })
        b["n"] += 1
        if outcome == "WIN":
            b["wins"] += 1
        elif outcome == "LOSS":
            b["losses"] += 1
        else:
            b["expired"] += 1

    def _wr(wins: int, losses: int) -> Optional[float]:
        tot = wins + losses
        return round(wins / tot * 100.0, 1) if tot else None

    sym_out = []
    for s in symbols.values():
        sym_out.append({
            "symbol": s["symbol"],
            "n": s["n"],
            "wins": s["wins"],
            "losses": s["losses"],
            "expired": s["expired"],
            "tp_rate_pct": _wr(s["wins"], s["losses"]),
            "avg_pnl_pct": round(s["pnl_pct_sum"] / s["n"], 3) if s["n"] else None,
            "last_outcome": s["last_outcome"],
        })
    sym_out.sort(key=lambda x: (-(x["tp_rate_pct"] or -1), -x["n"]))

    for b in buckets.values():
        b["tp_rate_pct"] = _wr(b["wins"], b["losses"])

    total_resolved = sum(s["n"] for s in symbols.values())
    return {
        "resolved": total_resolved,
        "pending": pending,
        "buckets": buckets,
        "symbols": sym_out,
    }


def shadow_stats(days: int = 30) -> Dict[str, Any]:
    """Scoreboard payload for /api/shadow-stats and the MCP tool."""
    from core.db import db_conn

    days = max(1, min(365, int(days)))
    cutoff = int(time.time()) - days * 86400
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_shadow_table(cur)
        cur.execute(
            "SELECT symbol, eval_ts, up_prob, outcome, pnl_pct "
            "FROM ghost_shadow_outcomes WHERE eval_ts >= %s",
            (cutoff,),
        )
        rows = [
            {"symbol": r[0], "eval_ts": r[1], "up_prob": r[2], "outcome": r[3], "pnl_pct": r[4]}
            for r in cur.fetchall()
        ]
    out = aggregate_shadow_stats(rows)
    out.update({
        "ok": True,
        "days": days,
        "prob_floor": PROB_FLOOR,
        "enabled": shadow_enabled(),
        "note": (
            "Virtual picks: every scanned symbol's daily model evaluation resolved "
            "with live TP/SL bar-path rules — gates ignored. 'fireable' bucket = "
            "up_prob >= prob floor (what the engine would have fired without "
            "regime/confidence gates)."
        ),
    })
    return out


def run_shadow_cycle() -> Dict[str, int]:
    """Scheduler hook: seed new rows, then resolve what price has decided."""
    if not shadow_enabled():
        return {"seeded": 0, "resolved": 0}
    try:
        seeded = seed_shadow_rows()
    except Exception as e:
        LOGGER.warning("shadow seed failed: %s", str(e)[:100])
        seeded = 0
    try:
        resolved = resolve_shadow_rows()
    except Exception as e:
        LOGGER.warning("shadow resolve failed: %s", str(e)[:100])
        resolved = 0
    return {"seeded": seeded, "resolved": resolved}
