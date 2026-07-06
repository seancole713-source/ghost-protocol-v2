"""core/paper_wallet.py — fake-money paper wallet (PR #138).

A Cash-App-style wallet the operator can watch: starts at a configurable fake
balance (default $10k), mirrors Ghost's signals as paper trades filled at live
quotes, and answers the only question that matters at week's end: "if I had
followed every Ghost trade with real money, where would I be?"

Two books, never mixed:
  gated  — mirrors REAL fired picks (predictions table). This is "following
           Ghost". Currently silent while the gates hold at 0 fireable.
  shadow — mirrors the ungated virtual evaluations (ghost_shadow_outcomes)
           above a probability floor, small fixed slices. Research evidence
           only; it exists to accumulate fill-level data fast.

Fill realism (quote-level, honestly labeled — NOT broker microstructure):
  entry  — live quote at cycle time (not the eval-time price)
  target — fills AT the target (a resting limit fills at limit or better;
           we take the conservative side)
  stop   — fills at min(stop, current) — a gap through the stop fills at the
           gapped price, which is exactly the slippage bar-sims hide
  expiry — market close-out at current quote

HARD GUARDRAILS: fake money only. This module never talks to a broker, never
places real orders, and is long-only (UP signals; DOWN stays shadow-brains-only
per the governance plan). Changing the balance resets the wallet.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from core.quiet import note_suppressed

LOGGER = logging.getLogger("ghost.paper_wallet")

_CONFIG_KEY = "paper_wallet_config"


def _slice_usd() -> float:
    return max(50.0, float(os.getenv("PAPER_TRADE_SLICE_USD", "500")))


def _shadow_min_prob() -> float:
    return float(os.getenv("PAPER_SHADOW_MIN_PROB", "0.50"))


def _max_open() -> int:
    return max(1, int(os.getenv("PAPER_MAX_OPEN", "15")))


def wallet_enabled() -> bool:
    return (os.getenv("PAPER_WALLET_ENABLED", "1") or "1").strip().lower() in ("1", "on", "true", "yes")


def ensure_paper_tables(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_paper_trades (
            id SERIAL PRIMARY KEY,
            book TEXT NOT NULL,
            symbol VARCHAR(20) NOT NULL,
            direction VARCHAR(6) NOT NULL DEFAULT 'UP',
            qty FLOAT NOT NULL,
            entry_price FLOAT NOT NULL,
            entry_ts BIGINT NOT NULL,
            target_price FLOAT,
            stop_price FLOAT,
            expires_at BIGINT,
            status TEXT NOT NULL DEFAULT 'open',
            exit_price FLOAT,
            exit_ts BIGINT,
            exit_reason TEXT,
            pnl FLOAT,
            pnl_pct FLOAT,
            source TEXT UNIQUE,
            created_at BIGINT NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_paper_open ON ghost_paper_trades(status, symbol)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_paper_daily (
            trade_date TEXT PRIMARY KEY,
            equity FLOAT NOT NULL,
            cash FLOAT NOT NULL,
            pnl FLOAT NOT NULL,
            ts BIGINT NOT NULL
        )
    """)


def get_config(cur) -> Dict[str, Any]:
    from core.db import ensure_ghost_state
    ensure_ghost_state(cur)
    cur.execute("SELECT val FROM ghost_state WHERE key=%s", (_CONFIG_KEY,))
    row = cur.fetchone()
    if row and row[0]:
        try:
            return json.loads(row[0])
        except Exception:
            note_suppressed()
    cfg = {"starting_balance": 10000.0, "reset_ts": int(time.time())}
    cur.execute(
        "INSERT INTO ghost_state(key,val) VALUES(%s,%s) "
        "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
        (_CONFIG_KEY, json.dumps(cfg)),
    )
    return cfg


def reset_wallet(starting_balance: float) -> Dict[str, Any]:
    """Set a new balance and wipe the book history — a fresh experiment."""
    bal = max(100.0, min(10_000_000.0, float(starting_balance)))
    from core.db import db_conn, ensure_ghost_state
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_paper_tables(cur)
        ensure_ghost_state(cur)
        cur.execute("DELETE FROM ghost_paper_trades")
        cur.execute("DELETE FROM ghost_paper_daily")
        cfg = {"starting_balance": bal, "reset_ts": int(time.time())}
        cur.execute(
            "INSERT INTO ghost_state(key,val) VALUES(%s,%s) "
            "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
            (_CONFIG_KEY, json.dumps(cfg)),
        )
        conn.commit()
    LOGGER.info("[paper_wallet] reset to $%.2f", bal)
    return {"ok": True, "starting_balance": bal}


def _cash(cur, cfg: Dict[str, Any]) -> float:
    cur.execute("SELECT COALESCE(SUM(pnl),0) FROM ghost_paper_trades WHERE status='closed'")
    realized = float(cur.fetchone()[0] or 0)
    cur.execute("SELECT COALESCE(SUM(qty*entry_price),0) FROM ghost_paper_trades WHERE status='open'")
    deployed = float(cur.fetchone()[0] or 0)
    return float(cfg["starting_balance"]) + realized - deployed


def _live_prices(symbols: List[str]) -> Dict[str, Optional[float]]:
    if not symbols:
        return {}
    try:
        from core.market_sessions import get_market_sessions
        out = get_market_sessions(symbols, max_fresh=6)
        return {s: r.get("price") for s, r in out["sessions"].items()}
    except Exception as exc:
        LOGGER.warning("paper wallet prices: %s", str(exc)[:100])
        return {}


def exit_fill(price: float, target, stop, expires_at, now: int):
    """Pure fill rules (long-only). Returns (exit_price, reason) or None.

    stop:   fills at min(stop, price) — a gap through the stop fills at the
            gapped price (the slippage bar-sims hide)
    target: fills AT the target (resting limit fills at limit or better;
            we book the conservative side)
    expiry: market close-out at current price
    """
    if stop and price <= stop:
        return round(min(float(stop), price), 4), "stop"
    if target and price >= target:
        return round(float(target), 4), "target"
    if expires_at and now >= int(expires_at):
        return round(price, 4), "expiry"
    return None


def _enter(cur, *, book: str, symbol: str, source: str, entry: float,
           target: Optional[float], stop: Optional[float],
           expires_at: Optional[int]) -> bool:
    qty = round(_slice_usd() / entry, 4)
    now = int(time.time())
    cur.execute(
        """INSERT INTO ghost_paper_trades
           (book, symbol, direction, qty, entry_price, entry_ts, target_price,
            stop_price, expires_at, status, source, created_at)
           VALUES (%s,%s,'UP',%s,%s,%s,%s,%s,%s,'open',%s,%s)
           ON CONFLICT (source) DO NOTHING""",
        (book, symbol.upper(), qty, round(entry, 4), now, target, stop,
         expires_at, source, now),
    )
    return cur.rowcount > 0


def run_wallet_cycle() -> Dict[str, Any]:
    """One engine pass: mirror new signals, check exits, snapshot the day."""
    if not wallet_enabled():
        return {"ok": True, "skipped": "PAPER_WALLET_ENABLED=0"}
    from core.db import db_conn
    entered = closed = 0
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_paper_tables(cur)
            cfg = get_config(cur)

            cur.execute("SELECT COUNT(*) FROM ghost_paper_trades WHERE status='open'")
            open_count = int(cur.fetchone()[0])
            cash = _cash(cur, cfg)

            # ── candidate signals ────────────────────────────────────────
            now_ts = int(time.time())
            # Mirror any still-live signal (unresolved + unexpired). No
            # reset_ts filter: entries fill at the CURRENT quote, so mirroring
            # an hours-old signal is honest — we buy now at now's price.
            cur.execute(
                """SELECT id, symbol, entry_price, target_price, stop_price, expires_at
                   FROM predictions
                   WHERE outcome IS NULL AND direction IN ('UP','BUY')
                     AND expires_at > %s
                     AND entry_price > 0
                   ORDER BY predicted_at DESC LIMIT 20""", (now_ts,))
            gated_rows = [("gated", f"pick:{r[0]}", r[1], r[2], r[3], r[4], r[5])
                          for r in cur.fetchall()]
            cur.execute(
                """SELECT id, symbol, entry_price, target_price, stop_price, expires_at
                   FROM ghost_shadow_outcomes
                   WHERE outcome IS NULL AND expires_at > %s AND up_prob >= %s
                   ORDER BY eval_ts DESC LIMIT 20""",
                (now_ts, _shadow_min_prob()))
            shadow_rows = [("shadow", f"shadow:{r[0]}", r[1], r[2], r[3], r[4], r[5])
                           for r in cur.fetchall()]

            candidates = gated_rows + shadow_rows
            need_prices = sorted({c[2].upper() for c in candidates})
            cur.execute("SELECT DISTINCT symbol FROM ghost_paper_trades WHERE status='open'")
            open_syms = sorted({r[0] for r in cur.fetchall()})
            prices = _live_prices(sorted(set(need_prices + open_syms)))

            for book, source, sym, tgt, stp, exp in [
                    (c[0], c[1], c[2], c[3], c[4], c[5]) for c in candidates]:
                if open_count >= _max_open() or cash < _slice_usd():
                    break
                entry = prices.get(sym.upper())
                if not entry or entry <= 0:
                    continue
                # Never enter a trade whose exit is already true: a stale
                # signal with a blown stop books an instant fake loss, and one
                # past its target books an instant fake win. Skip both.
                if (stp and entry <= stp) or (tgt and entry >= tgt):
                    continue
                if _enter(cur, book=book, symbol=sym, source=source, entry=entry,
                          target=tgt, stop=stp, expires_at=exp):
                    entered += 1
                    open_count += 1
                    cash -= _slice_usd()

            # ── exits ────────────────────────────────────────────────────
            now = int(time.time())
            cur.execute(
                """SELECT id, symbol, qty, entry_price, target_price, stop_price, expires_at
                   FROM ghost_paper_trades WHERE status='open'""")
            for tid, sym, qty, entry, tgt, stp, exp in cur.fetchall():
                p = prices.get(sym.upper())
                if not p or p <= 0:
                    continue
                fill = exit_fill(p, tgt, stp, exp, now)
                if fill is None:
                    continue
                exit_price, reason = fill
                pnl = round((exit_price - entry) * qty, 4)
                pnl_pct = round((exit_price / entry - 1) * 100, 3) if entry else None
                cur.execute(
                    """UPDATE ghost_paper_trades
                       SET status='closed', exit_price=%s, exit_ts=%s,
                           exit_reason=%s, pnl=%s, pnl_pct=%s
                       WHERE id=%s AND status='open'""",
                    (exit_price, now, reason, pnl, pnl_pct, tid))
                closed += cur.rowcount

            # ── daily snapshot ───────────────────────────────────────────
            cash = _cash(cur, cfg)
            cur.execute(
                """SELECT symbol, SUM(qty), SUM(qty*entry_price)
                   FROM ghost_paper_trades WHERE status='open' GROUP BY symbol""")
            mkt = 0.0
            for sym, tqty, cost in cur.fetchall():
                p = prices.get(sym.upper())
                mkt += (float(tqty) * float(p)) if p else float(cost)
            equity = round(cash + mkt, 2)
            import datetime as _dt
            today = _dt.date.today().isoformat()
            cur.execute("SELECT equity FROM ghost_paper_daily WHERE trade_date < %s "
                        "ORDER BY trade_date DESC LIMIT 1", (today,))
            prev = cur.fetchone()
            prev_eq = float(prev[0]) if prev else float(cfg["starting_balance"])
            cur.execute(
                """INSERT INTO ghost_paper_daily (trade_date, equity, cash, pnl, ts)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (trade_date) DO UPDATE
                   SET equity=EXCLUDED.equity, cash=EXCLUDED.cash,
                       pnl=EXCLUDED.pnl, ts=EXCLUDED.ts""",
                (today, equity, round(cash, 2), round(equity - prev_eq, 2), now))
            conn.commit()
        out = {"ok": True, "entered": entered, "closed": closed, "equity": equity}
        if entered or closed:
            LOGGER.info("[paper_wallet] %s", out)
        return out
    except Exception as exc:
        LOGGER.warning("paper wallet cycle failed: %s", str(exc)[:140])
        return {"ok": False, "error": str(exc)[:140]}


def wallet_summary() -> Dict[str, Any]:
    """Everything the Wallet tab renders. Read-only; never raises."""
    from core.db import db_conn
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_paper_tables(cur)
            cfg = get_config(cur)
            cash = _cash(cur, cfg)
            cur.execute(
                """SELECT id, book, symbol, qty, entry_price, entry_ts,
                          target_price, stop_price, expires_at
                   FROM ghost_paper_trades WHERE status='open'
                   ORDER BY entry_ts DESC""")
            open_rows = cur.fetchall()
            open_syms = sorted({r[2].upper() for r in open_rows})
            prices = _live_prices(open_syms)
            positions = []
            invested = mkt_value = 0.0
            for tid, book, sym, qty, entry, ets, tgt, stp, exp in open_rows:
                cur_p = prices.get(sym.upper()) or entry
                val = round(qty * cur_p, 2)
                cost = qty * entry
                invested += cost
                mkt_value += val
                positions.append({
                    "id": tid, "book": book, "symbol": sym, "qty": qty,
                    "entry_price": entry, "entry_ts": ets, "current_price": cur_p,
                    "value": val, "pnl": round(val - cost, 2),
                    "pnl_pct": round((cur_p / entry - 1) * 100, 2) if entry else None,
                    "target_price": tgt, "stop_price": stp, "expires_at": exp,
                })
            cur.execute(
                """SELECT id, book, symbol, qty, entry_price, entry_ts, exit_price,
                          exit_ts, exit_reason, pnl, pnl_pct
                   FROM ghost_paper_trades WHERE status='closed'
                   ORDER BY exit_ts DESC LIMIT 60""")
            history = [dict(zip(("id", "book", "symbol", "qty", "entry_price",
                                 "entry_ts", "exit_price", "exit_ts", "exit_reason",
                                 "pnl", "pnl_pct"), r)) for r in cur.fetchall()]
            cur.execute("SELECT COUNT(*), COALESCE(SUM(pnl),0),"
                        " SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END)"
                        " FROM ghost_paper_trades WHERE status='closed'")
            n_closed, realized, wins = cur.fetchone()
            cur.execute("SELECT trade_date, equity, pnl FROM ghost_paper_daily "
                        "ORDER BY trade_date DESC LIMIT 14")
            daily = [{"date": r[0], "equity": float(r[1]), "pnl": float(r[2])}
                     for r in cur.fetchall()]
            equity = round(cash + mkt_value, 2)
            start = float(cfg["starting_balance"])
            today_pnl = daily[0]["pnl"] if daily else 0.0
            return {
                "ok": True,
                "paper": True,
                "note": ("FAKE MONEY — paper wallet with quote-level fills. gated book = real "
                         "fired picks; shadow book = ungated research signals, small slices."),
                "starting_balance": start,
                "reset_ts": cfg.get("reset_ts"),
                "cash": round(cash, 2),
                "invested": round(invested, 2),
                "market_value": round(mkt_value, 2),
                "total_value": equity,
                "total_pnl": round(equity - start, 2),
                "total_pnl_pct": round((equity / start - 1) * 100, 2) if start else 0.0,
                "today_pnl": today_pnl,
                "realized_pnl": round(float(realized), 2),
                "closed_trades": int(n_closed or 0),
                "closed_wins": int(wins or 0),
                "open_positions": positions,
                "history": history,
                "daily": daily,
                "trade_slice_usd": _slice_usd(),
                "shadow_min_prob": _shadow_min_prob(),
            }
    except Exception as exc:
        LOGGER.warning("wallet summary failed: %s", str(exc)[:140])
        return {"ok": False, "error": str(exc)[:140]}
