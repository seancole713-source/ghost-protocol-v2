"""core/paper_wallet.py — fake-money paper wallet (PR #138).

A Cash-App-style wallet the operator can watch: starts at a configurable fake
balance (default $10k), mirrors Ghost's signals as paper trades filled at live
quotes, and answers the only question that matters at week's end: "if I had
followed every Ghost trade with real money, where would I be?"

Two books, never mixed:
  gated  — mirrors REAL fired picks (predictions table). This is "following
           Ghost". Currently silent while the gates hold at 0 fireable.
  shadow — mirrors the ungated virtual evaluations (ghost_shadow_outcomes)
           above a probability floor *and* a symbol-level proven-skill floor,
           small fixed slices. Research evidence only; it exists to accumulate
           fill-level data without buying every coin-flip symbol.

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
    # PR #151: Shadow wallet should not buy below Ghost's own shadow/fireable
    # probability floor by default. Keep env override for controlled experiments.
    return float(os.getenv("PAPER_SHADOW_MIN_PROB", "0.55"))


def _shadow_skill_min_tp_rate() -> float:
    # Symbol-level historical TP filter for fake-money shadow entries.
    # 0.55 = better than coin-flip after ignoring still-pending/expired rows.
    return float(os.getenv("PAPER_SHADOW_SKILL_MIN_TP_RATE", "0.55"))


def _shadow_skill_min_resolved() -> int:
    return max(1, int(os.getenv("PAPER_SHADOW_SKILL_MIN_RESOLVED", "10")))


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
    # Each finished month's result vs the goal — the honest track record of
    # "can Ghost 2x fake money in a month?" that survives the monthly reset.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_paper_monthly (
            month TEXT PRIMARY KEY,
            start_balance FLOAT NOT NULL,
            goal FLOAT NOT NULL,
            final_equity FLOAT NOT NULL,
            hit_goal BOOLEAN NOT NULL,
            return_pct FLOAT,
            closed_at BIGINT NOT NULL
        )
    """)


def _month_key() -> str:
    import datetime as _dt
    return _dt.date.today().strftime("%Y-%m")


def _default_goal() -> float:
    return float(os.getenv("PAPER_MONTHLY_GOAL", "20000"))


def _write_config(cur, cfg: Dict[str, Any]) -> None:
    cur.execute(
        "INSERT INTO ghost_state(key,val) VALUES(%s,%s) "
        "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
        (_CONFIG_KEY, json.dumps(cfg)),
    )


def get_config(cur) -> Dict[str, Any]:
    from core.db import ensure_ghost_state
    ensure_ghost_state(cur)
    cur.execute("SELECT val FROM ghost_state WHERE key=%s", (_CONFIG_KEY,))
    row = cur.fetchone()
    cfg = None
    if row and row[0]:
        try:
            cfg = json.loads(row[0])
        except Exception:
            note_suppressed()
    if cfg is None:
        cfg = {"starting_balance": 10000.0, "reset_ts": int(time.time())}
    # Backfill goal fields for wallets created before the monthly-goal feature.
    changed = False
    if "monthly_goal" not in cfg:
        cfg["monthly_goal"] = _default_goal(); changed = True
    if "goal_month" not in cfg:
        cfg["goal_month"] = _month_key(); changed = True
    if changed or not (row and row[0]):
        _write_config(cur, cfg)
    return cfg


def reset_wallet(starting_balance: float,
                 monthly_goal: float | None = None) -> Dict[str, Any]:
    """Set a new balance/goal and wipe the book history — a fresh experiment."""
    bal = max(100.0, min(10_000_000.0, float(starting_balance)))
    from core.db import db_conn, ensure_ghost_state
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_paper_tables(cur)
        ensure_ghost_state(cur)
        prev = get_config(cur)
        goal = float(monthly_goal) if monthly_goal is not None else float(prev.get("monthly_goal") or _default_goal())
        goal = max(bal, min(10_000_000.0, goal))  # goal can't be below the start
        cur.execute("DELETE FROM ghost_paper_trades")
        cur.execute("DELETE FROM ghost_paper_daily")
        cfg = {"starting_balance": bal, "monthly_goal": goal,
               "goal_month": _month_key(), "reset_ts": int(time.time())}
        _write_config(cur, cfg)
        conn.commit()
    LOGGER.info("[paper_wallet] reset to $%.2f (monthly goal $%.2f)", bal, goal)
    return {"ok": True, "starting_balance": bal, "monthly_goal": goal}


def _maybe_roll_month(cur, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """On calendar-month rollover: record the finished month's result vs goal,
    then wipe the books and start the new month fresh at starting_balance.

    This is what gives the wallet its recurring purpose — every month it starts
    over at $10k and tries again to reach the goal, and every attempt is kept
    in ghost_paper_monthly as honest history."""
    this_month = _month_key()
    prev_month = cfg.get("goal_month")
    if prev_month == this_month:
        return cfg
    start = float(cfg.get("starting_balance") or 10000.0)
    goal = float(cfg.get("monthly_goal") or _default_goal())
    # Final equity = the last daily snapshot of the closing month (already
    # mark-to-market); fall back to cost-based equity if no snapshot exists.
    cur.execute("SELECT equity FROM ghost_paper_daily ORDER BY trade_date DESC LIMIT 1")
    r = cur.fetchone()
    equity_now = float(r[0]) if r and r[0] is not None else _cash(cur, cfg)
    ret = round((equity_now / start - 1) * 100, 2) if start else 0.0
    cur.execute(
        """INSERT INTO ghost_paper_monthly
           (month, start_balance, goal, final_equity, hit_goal, return_pct, closed_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (month) DO NOTHING""",
        (prev_month or "unknown", start, goal, round(equity_now, 2),
         bool(equity_now >= goal), ret, int(time.time())),
    )
    cur.execute("DELETE FROM ghost_paper_trades")
    cur.execute("DELETE FROM ghost_paper_daily")
    cfg = {**cfg, "goal_month": this_month, "reset_ts": int(time.time())}
    _write_config(cur, cfg)
    LOGGER.info("[paper_wallet] month %s closed at $%.2f (goal $%.2f); reset for %s",
                prev_month, equity_now, goal, this_month)
    return cfg


def _cash(cur, cfg: Dict[str, Any]) -> float:
    cur.execute("SELECT COALESCE(SUM(pnl),0) FROM ghost_paper_trades WHERE status='closed'")
    realized = float(cur.fetchone()[0] or 0)
    cur.execute("SELECT COALESCE(SUM(qty*entry_price),0) FROM ghost_paper_trades WHERE status='open'")
    deployed = float(cur.fetchone()[0] or 0)
    return float(cfg["starting_balance"]) + realized - deployed


def _live_prices(symbols: List[str]) -> Dict[str, Optional[float]]:
    """Prices for the wallet's candidate/open symbols.

    The batch endpoint is cache-first with a small fresh budget (anti-breaker),
    which starved the wallet — it needs a real price for every symbol it might
    trade, not just the 6 the batch refreshes. So: batch first, then a bounded
    get_price() fallback (the same 5-tier spot chain the single-symbol endpoint
    uses, breaker-protected) for any symbol the batch left null. (PR #143)
    """
    if not symbols:
        return {}
    out: Dict[str, Optional[float]] = {}
    try:
        from core.market_sessions import get_market_sessions
        sess = get_market_sessions(symbols, max_fresh=len(symbols))
        out = {s: r.get("price") for s, r in sess["sessions"].items()}
    except Exception as exc:
        LOGGER.warning("paper wallet batch prices: %s", str(exc)[:100])
    missing = [s for s in symbols if not out.get(s.upper()) and not out.get(s)]
    if missing:
        try:
            from core.prices import get_price
            for s in missing[:40]:
                p = get_price(s)
                if p and p > 0:
                    out[s.upper()] = round(float(p), 4)
        except Exception as exc:
            LOGGER.warning("paper wallet spot fallback: %s", str(exc)[:100])
    return out


def fresh_bands(symbol: str, entry: float, asset_type: str = "stock",
                now: int | None = None):
    """Target/stop/expiry bracketing the CURRENT entry, using Ghost's own
    vol geometry (base_vol_pct + stop_pct_from_vol). (PR #145, Option B)

    Mirroring at the current quote with the signal's STALE morning bands was
    incoherent — on a down day the live price is already below the morning
    stop, so every long was refused and the wallet never traded. Recomputing
    the bands from the buy-now price means each entry is bracketed correctly
    and the wallet takes positions daily (wins AND losses — the unbiased
    evidence the whole exercise exists to gather). Same geometry the engine
    uses, applied at the fill price.
    """
    from core.vol_targets import base_vol_pct, stop_pct_from_vol
    now = int(now or time.time())
    vol = base_vol_pct(symbol, asset_type)
    stop_pct = stop_pct_from_vol(vol)
    target = round(entry * (1 + vol), 4)
    stop = round(entry * (1 - stop_pct), 4)
    expires_at = now + int(os.getenv("PAPER_HOLD_BARS", "3")) * 86400
    return target, stop, expires_at


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
            cfg = _maybe_roll_month(cur, cfg)  # new month → record + reset

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
            # Research-wallet shadow entries must be both confident enough and
            # historically proven for the *symbol*. PR #151: up_prob >= 0.50 was
            # admitting coin-flip/negative-P&L symbols (e.g. 33-41% TP buckets)
            # even though the public gate remained closed. This is still fake
            # money only, but the evidence wallet should prefer symbols whose
            # shadow track record clears a basic skill floor.
            cur.execute(
                """
                WITH skill AS (
                    SELECT symbol,
                           SUM(CASE WHEN outcome IN ('WIN','LOSS') THEN 1 ELSE 0 END) AS resolved,
                           SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) AS wins
                    FROM ghost_shadow_outcomes
                    WHERE outcome IS NOT NULL
                    GROUP BY symbol
                )
                SELECT o.id, o.symbol, o.entry_price, o.target_price, o.stop_price, o.expires_at,
                       COALESCE(s.resolved, 0) AS resolved,
                       COALESCE(s.wins, 0) AS wins
                FROM ghost_shadow_outcomes o
                LEFT JOIN skill s ON s.symbol = o.symbol
                WHERE o.outcome IS NULL
                  AND o.expires_at > %s
                  AND o.up_prob >= %s
                  AND COALESCE(s.resolved, 0) >= %s
                  AND (COALESCE(s.wins, 0)::float / NULLIF(s.resolved, 0)) >= %s
                ORDER BY o.eval_ts DESC LIMIT 20
                """,
                (now_ts, _shadow_min_prob(), _shadow_skill_min_resolved(), _shadow_skill_min_tp_rate()))
            shadow_rows = [("shadow", f"shadow:{r[0]}", r[1], r[2], r[3], r[4], r[5])
                           for r in cur.fetchall()]

            candidates = gated_rows + shadow_rows
            need_prices = sorted({c[2].upper() for c in candidates})
            cur.execute("SELECT DISTINCT symbol FROM ghost_paper_trades WHERE status='open'")
            open_syms = sorted({r[0] for r in cur.fetchall()})
            prices = _live_prices(sorted(set(need_prices + open_syms)))

            # Observability (PR #143): why did candidates not become entries?
            diag = {"gated_candidates": len(gated_rows),
                    "shadow_candidates": len(shadow_rows),
                    "skip_no_price": 0, "skip_capacity": 0, "skip_dupe": 0}
            for book, source, sym in [(c[0], c[1], c[2]) for c in candidates]:
                if open_count >= _max_open() or cash < _slice_usd():
                    diag["skip_capacity"] += 1
                    continue
                entry = prices.get(sym.upper())
                if not entry or entry <= 0:
                    diag["skip_no_price"] += 1
                    continue
                # Option B (PR #145): bracket the buy-now price with fresh bands
                # from Ghost's vol geometry — no stale-band pre-crossing.
                tgt, stp, exp = fresh_bands(sym, entry)
                if _enter(cur, book=book, symbol=sym, source=source, entry=entry,
                          target=tgt, stop=stp, expires_at=exp):
                    entered += 1
                    open_count += 1
                    cash -= _slice_usd()
                else:
                    diag["skip_dupe"] += 1

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
        out = {"ok": True, "entered": entered, "closed": closed, "equity": equity,
               "diag": diag}
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

            # ── Monthly goal progress (the wallet's recurring purpose) ──
            import datetime as _dt
            goal = float(cfg.get("monthly_goal") or _default_goal())
            todd = _dt.date.today()
            if todd.month == 12:
                days_in_month = 31
            else:
                days_in_month = (_dt.date(todd.year, todd.month + 1, 1) - _dt.date(todd.year, todd.month, 1)).days
            day_of_month = todd.day
            days_left = max(0, days_in_month - day_of_month)
            gained = equity - start
            needed = goal - start
            progress_pct = round(gained / needed * 100, 1) if needed > 0 else 0.0
            remaining = round(goal - equity, 2)
            need_per_day = round(remaining / days_left, 2) if days_left > 0 else remaining
            cur.execute("SELECT month, start_balance, goal, final_equity, hit_goal, return_pct "
                        "FROM ghost_paper_monthly ORDER BY month DESC LIMIT 12")
            months = [dict(zip(("month", "start_balance", "goal", "final_equity",
                                "hit_goal", "return_pct"), r)) for r in cur.fetchall()]
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
                "shadow_skill_min_tp_rate": _shadow_skill_min_tp_rate(),
                "shadow_skill_min_resolved": _shadow_skill_min_resolved(),
                "goal": {
                    "target": goal,
                    "month": cfg.get("goal_month"),
                    "progress_pct": progress_pct,
                    # progress_pct = (equity-start)/(goal-start): goes negative
                    # when underwater (honest but confusing next to a bar). Also
                    # expose pct_of_goal = equity/goal, always positive, so the
                    # bar and the number agree. (PR #150 audit)
                    "pct_of_goal": round(equity / goal * 100, 1) if goal > 0 else 0.0,
                    "reached": bool(equity >= goal),
                    "remaining": remaining,
                    "day_of_month": day_of_month,
                    "days_in_month": days_in_month,
                    "days_left": days_left,
                    "need_per_day": need_per_day,
                    "history": months,
                    "note": ("Aspirational stretch target. The wallet resets to the starting "
                             "balance on the 1st of each month and tries again — every month's "
                             "real result is kept below, so this shows what's actually achievable."),
                },
            }
    except Exception as exc:
        LOGGER.warning("wallet summary failed: %s", str(exc)[:140])
        return {"ok": False, "error": str(exc)[:140]}
