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
from core.quiet import note_suppressed

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from core.db import ensure_ghost_state

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
    # Additive migration: persist the market regime AT ISSUANCE on the outcome
    # row itself. The 70+ slice search conditions the win test on regime, and
    # ghost_perf_symbol_evals (the join source) is pruned after ~90 days
    # (GHOST_PERF_RETENTION_DAYS) while shadow outcomes are not — without a
    # durable column the conditioning signal would decay out from under a
    # forward proof. CREATE TABLE IF NOT EXISTS never adds columns to an
    # existing prod table, so this ALTER is required.
    cur.execute(
        "ALTER TABLE ghost_shadow_outcomes ADD COLUMN IF NOT EXISTS regime_label TEXT"
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
    # Deterministic (symbol, date) order: concurrent seeders acquire row locks
    # in the same order, which removes the lock-order deadlock between the
    # hourly job and the market-scan seed.
    return [chosen[k] for k in sorted(chosen)]


def _eval_entry_price(ev: Dict[str, Any]) -> Optional[float]:
    """Entry for the virtual trade: real entry when fired, else scan price."""
    try:
        p = float(ev.get("entry_price") or 0)
        if p > 0:
            return p
    except Exception:
        note_suppressed()
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
            note_suppressed()
    return None


# Advisory-lock key for the shadow seeder — arbitrary constant, unique app-wide.
_SEED_ADVISORY_LOCK_KEY = 749_301_552


def seed_shadow_rows(days_back: int = 3) -> int:
    """Insert pending shadow rows from recent symbol evals (idempotent)."""
    from core.db import db_conn

    cutoff = int(time.time()) - max(1, int(days_back)) * 86400
    with db_conn() as conn:
        cur = conn.cursor()
        # Single-seeder guard: the hourly job and the market-scan seed can run
        # concurrently; seeding is idempotent, so if another transaction holds
        # the lock just skip — the next run catches anything missed. This (plus
        # deterministic insert order in pick_daily_first) removes the
        # "deadlock detected" failures seen in production.
        cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (_SEED_ADVISORY_LOCK_KEY,))
        row = cur.fetchone()
        if not (row and row[0]):
            LOGGER.debug("shadow seed: another seeder holds the lock, skipping")
            return 0
        ensure_shadow_table(cur)
        try:
            cur.execute(
                "SELECT symbol, eval_ts, up_prob, confidence, skip_code, fired, "
                "entry_price, target_price, stop_price, scores, regime_label "
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
                "regime_label": r[10],
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
                     entry_price, target_price, stop_price, expires_at, created_at,
                     regime_label)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol, trade_date) DO NOTHING
                """,
                (
                    sym, _ct_date(eval_ts), eval_ts,
                    ev.get("up_prob"), ev.get("confidence"), ev.get("skip_code"),
                    bool(ev.get("fired")),
                    round(entry, 6), round(target, 6), round(stop, 6),
                    expires_at_nth_trading_close(eval_ts, hold), now,
                    (str(ev.get("regime_label")) if ev.get("regime_label") is not None else None),
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
            # PR #151: if a virtual pick is already past its hold window but
            # no OHLCV bars are available, close it as EXPIRED at entry (0%
            # P&L) instead of leaving it pending forever. This is conservative:
            # no WIN/LOSS is credited without a bar path; it simply honors the
            # documented hold expiry.
            for (sid, _sym, eval_ts, entry, target, stop, expires_at) in rows:
                if expires_at and now > int(expires_at):
                    with db_conn() as conn:
                        conn.cursor().execute(
                            "UPDATE ghost_shadow_outcomes "
                            "SET outcome=%s, exit_price=%s, pnl_pct=%s, resolved_at=%s WHERE id=%s",
                            ("EXPIRED", float(entry), 0.0, now, sid),
                        )
                    resolved += 1
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
    elif pending:
        LOGGER.debug("Shadow resolve: 0/%d pending — hold window still open or path undecided", len(pending))
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


def shadow_diagnostics() -> Dict[str, Any]:
    """Ops payload: explain pending vs resolved (hold window timing)."""
    from core.db import db_conn
    from core.tp_sl_resolve import label_hold_bars

    hold = label_hold_bars()
    now = int(time.time())
    out: Dict[str, Any] = {
        "hold_bars": hold,
        "now_ts": now,
        "pending": 0,
        "resolved_total": 0,
    }
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_shadow_table(cur)
            cur.execute("SELECT COUNT(*) FROM ghost_shadow_outcomes WHERE outcome IS NULL")
            out["pending"] = int(cur.fetchone()[0] or 0)
            cur.execute("SELECT COUNT(*) FROM ghost_shadow_outcomes WHERE outcome IS NOT NULL")
            out["resolved_total"] = int(cur.fetchone()[0] or 0)
            cur.execute(
                "SELECT MIN(eval_ts), MAX(eval_ts), MIN(expires_at), MIN(trade_date), MAX(trade_date) "
                "FROM ghost_shadow_outcomes WHERE outcome IS NULL"
            )
            row = cur.fetchone()
            if row and row[0]:
                out["oldest_pending_eval_ts"] = int(row[0])
                out["newest_pending_eval_ts"] = int(row[1])
                out["earliest_expires_at"] = int(row[2]) if row[2] else None
                out["pending_trade_dates"] = {"oldest": row[3], "newest": row[4]}
    except Exception as e:
        out["error"] = str(e)[:120]
        return out

    exp = out.get("earliest_expires_at")
    if out["pending"] and exp:
        if now >= exp:
            out["resolution_status"] = "due — pending rows past expiry; resolver should close on next hourly job"
        else:
            out["resolution_status"] = "waiting — hold window open; first batch closes after earliest expires_at"
        try:
            import pytz

            tz = pytz.timezone(os.getenv("GHOST_TZ", "America/Chicago"))
            out["earliest_expires_at_ct"] = datetime.fromtimestamp(exp, tz).strftime("%Y-%m-%d %H:%M %Z")
        except Exception:
            note_suppressed()
    elif out["pending"]:
        out["resolution_status"] = "waiting — no expiry metadata on pending rows"
    else:
        out["resolution_status"] = "idle — no pending virtual picks"
    return out


def shadow_stats(days: int = 30) -> Dict[str, Any]:
    """Scoreboard payload for /api/shadow-stats and the MCP tool."""
    from core.db import db_conn
    from core.tp_sl_resolve import label_hold_bars

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
    diag = shadow_diagnostics()
    try:
        import json as _j
        from core.db import db_conn

        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT val FROM ghost_state WHERE key='last_shadow_cycle'")
            row = cur.fetchone()
        if row and row[0]:
            diag["last_cycle"] = _j.loads(row[0])
    except Exception:
        note_suppressed()
    out.update({
        "ok": True,
        "days": days,
        "prob_floor": PROB_FLOOR,
        "enabled": shadow_enabled(),
        "diagnostics": diag,
        "note": (
            "Virtual picks: every scanned symbol's daily model evaluation resolved "
            "with live TP/SL bar-path rules — gates ignored. 'fireable' bucket = "
            "up_prob >= prob floor. Pending rows stay open until TP/SL hit or "
            f"{label_hold_bars()}-bar hold expires (same as live picks)."
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
    result = {"seeded": seeded, "resolved": resolved}
    try:
        import json as _j
        from core.db import db_conn

        with db_conn() as conn:
            cur = conn.cursor()
            ensure_ghost_state(cur)
            cur.execute(
                "INSERT INTO ghost_state(key,val) VALUES('last_shadow_cycle', %s) "
                "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                (_j.dumps({**result, "ts": int(time.time())}),),
            )
    except Exception:
        note_suppressed()
    return result
