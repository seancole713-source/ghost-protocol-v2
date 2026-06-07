"""
core/watchdog.py - Real-time position hit alerts.
Runs every 5 minutes. When a pick hits target or stop,
marks it resolved and sends immediate Telegram alert.
Separate from reconcile_outcomes() which runs every 15 min
and does NOT send alerts.
"""
import time, logging, os
from core.db import db_conn
from core.prices import get_price
from core.tp_sl_resolve import label_hold_bars, resolve_open_prediction

LOGGER = logging.getLogger("ghost.watchdog")

def run_watchdog():
    """Check all open v2 picks. Bar-path TP/SL first (v3.2 label parity)."""
    alerted = 0
    now = int(time.time())
    hold_bars = label_hold_bars()
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id,symbol,direction,entry_price,target_price,stop_price,asset_type,confidence,"
                " predicted_at,expires_at"
                " FROM predictions WHERE outcome IS NULL AND predicted_at IS NOT NULL"
                " AND entry_price > 0.52 AND target_price IS NOT NULL AND stop_price IS NOT NULL"
                " LIMIT 60"
            )
            open_picks = cur.fetchall()
        for pred_id, symbol, direction, entry, target, stop, asset_type, conf, predicted_at, expires_at in open_picks:
            daily_bars = None
            try:
                from core.signal_engine import _fetch_ohlcv
                daily_bars = _fetch_ohlcv(symbol, asset_type or "stock", period="3m")
            except Exception:
                pass
            price = get_price(symbol)
            hit = resolve_open_prediction(
                direction=direction,
                target=float(target),
                stop=float(stop),
                predicted_at=int(predicted_at or 0),
                hold_bars=hold_bars,
                daily_bars=daily_bars,
                snapshot_price=float(price) if price else None,
                now=now,
                expires_at=int(expires_at) if expires_at else None,
            )
            if hit not in ("WIN", "LOSS"):
                continue
            from core.pnl import resolution_exit
            exit_price, pnl = resolution_exit(hit, direction, entry, target, stop, price if price else entry)
            with db_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE predictions SET outcome=%s,exit_price=%s,pnl_pct=%s,resolved_at=%s WHERE id=%s AND outcome IS NULL",
                    (hit, exit_price, pnl, now, pred_id))
                updated = cur.rowcount
            if not updated: continue  # Already resolved by reconciler
            try:
                from core.telegram import send_position_alert
                usd_out = round(100 * (1 + pnl / 100), 2)
                send_position_alert(symbol, direction, hit, entry, exit_price, pnl, usd_out)
                alerted += 1
                LOGGER.info("WATCHDOG HIT: "+symbol+" "+direction+" "+hit+" "+str(round(pnl,2))+"%")
            except Exception as e:
                LOGGER.error("watchdog alert "+symbol+": "+str(e))
    except Exception as e:
        LOGGER.error("watchdog error: "+str(e))
    return alerted
