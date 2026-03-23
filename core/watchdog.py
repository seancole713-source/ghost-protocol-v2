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

LOGGER = logging.getLogger("ghost.watchdog")

def run_watchdog():
    """Check all open v2 picks against live prices. Alert on hits."""
    alerted = 0
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id,symbol,direction,entry_price,target_price,stop_price,asset_type,confidence"
                " FROM predictions WHERE outcome IS NULL AND predicted_at IS NOT NULL"
                " AND entry_price > 0.52 AND target_price IS NOT NULL AND stop_price IS NOT NULL"
                " LIMIT 60"
            )
            open_picks = cur.fetchall()
        for pred_id, symbol, direction, entry, target, stop, asset_type, conf in open_picks:
            price = get_price(symbol, asset_type or "crypto")
            if not price: continue
            hit = None
            if direction == "UP":
                if price >= target: hit = "WIN"
                elif price <= stop: hit = "LOSS"
            else:
                if price <= target: hit = "WIN"
                elif price >= stop: hit = "LOSS"
            if not hit: continue
            pnl = (price-entry)/entry*100 if direction=="UP" else (entry-price)/entry*100
            with db_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE predictions SET outcome=%s,exit_price=%s,pnl_pct=%s,resolved_at=%s WHERE id=%s AND outcome IS NULL",
                    (hit, price, round(pnl,3), int(time.time()), pred_id))
                updated = cur.rowcount
            if not updated: continue  # Already resolved by reconciler
            try:
                from core.telegram import send_position_alert
                send_position_alert(symbol, direction, entry, price, hit, round(pnl,2), conf or 0)
                alerted += 1
                LOGGER.info("WATCHDOG HIT: "+symbol+" "+direction+" "+hit+" "+str(round(pnl,2))+"%")
            except Exception as e:
                LOGGER.error("watchdog alert "+symbol+": "+str(e))
    except Exception as e:
        LOGGER.error("watchdog error: "+str(e))
    return alerted