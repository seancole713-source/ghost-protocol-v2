"""
core/telegram.py - Ghost Protocol v2 notification system.
Sends morning card, position alerts, news warnings, weekly summary.
Plain language. A 5-year-old should understand every message.
"""
import os, time, logging, requests
from typing import Optional

LOGGER = logging.getLogger("ghost.telegram")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DISCORD_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
ALERTS_ENABLED = os.getenv("ALERTS_ENABLED", "1") == "1"

def _send(text: str) -> bool:
    """Send message to Telegram and Discord."""
    if not ALERTS_ENABLED:
        LOGGER.info("Alerts disabled - would send: " + text[:50])
        return True
    ok = True
    # Telegram
    if BOT_TOKEN and CHAT_ID:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=10
            )
            if not r.ok:
                LOGGER.error("Telegram failed: " + r.text[:100])
                ok = False
        except Exception as e:
            LOGGER.error("Telegram error: " + str(e))
            ok = False
    # Discord
    if DISCORD_URL:
        try:
            requests.post(DISCORD_URL, json={"content": text}, timeout=10)
        except Exception as e:
            LOGGER.warning("Discord error: " + str(e))
    return ok

def send_morning_card(picks: list, week_stats: dict = None) -> bool:
    """
    Send the daily TOP picks card.
    picks: list of prediction dicts from prediction.py
    """
    from datetime import datetime
    import pytz
    tz = pytz.timezone(os.getenv("GHOST_TZ", "America/Chicago"))
    now = datetime.now(tz)
    day = now.strftime("%A %b %d")
    lines = ["<b>👻 GHOST PICKS — " + day + "</b>"]
    if not picks:
        lines.append("No picks today — market conditions not ideal.")
    else:
        for i, p in enumerate(picks[:10], 1):
            sym = p["symbol"]
            direction = p["direction"]
            arrow = "🟢" if direction == "UP" else "🔴"
            entry = p["entry_price"]
            target = p["target_price"]
            stop = p["stop_price"]
            conf = int(p["confidence"] * 100)
            # Calculate $100 in -> $X out
            if direction == "UP":
                pct = (target - entry) / entry * 100
            else:
                pct = (entry - target) / entry * 100
            usd_out = round(100 * (1 + pct/100), 2)
            # Format price nicely
            if entry >= 1:
                ep = "$" + str(round(entry, 2))
                tp = "$" + str(round(target, 2))
                sp = "$" + str(round(stop, 2))
            else:
                ep = "$" + str(round(entry, 6))
                tp = "$" + str(round(target, 6))
                sp = "$" + str(round(stop, 6))
            from datetime import datetime, timezone
            exp = datetime.fromtimestamp(p["expires_at"], tz=timezone.utc)
            exp_str = exp.astimezone(tz).strftime("%a %b %d ~%-I%p").upper()
            lines.append("")
            lines.append(arrow + " <b>" + sym + " going " + direction + "</b> (" + str(conf) + "% confident)")
            lines.append("   Get in at    " + ep)
            lines.append("   Get out at   " + tp + " (+" + str(round(pct,1)) + "%)")
            lines.append("   Run away at  " + sp)
            lines.append("   Done by      " + exp_str)
            lines.append("   $100 in → $" + str(usd_out) + " out")
    if week_stats and week_stats.get("total", 0) > 0:
        lines.append("")
        w = week_stats["wins"]
        l = week_stats["losses"]
        pnl = week_stats.get("pnl_usd", 0)
        lines.append("📊 Last 7 days: " + str(w) + "W/" + str(l) + "L | $" + str(round(pnl,2)) + " if followed")
    lines.append("")
    lines.append("🎯 All-time: " + str(week_stats.get("alltime_wr",0)) + "% accuracy")
    return _send("
".join(lines))

def send_position_alert(symbol: str, direction: str, outcome: str, entry: float, exit_price: float, pnl_pct: float, usd_out: float) -> bool:
    """Send alert when a position hits target or stop."""
    arrow = "✅" if outcome == "WIN" else "❌"
    label = "TARGET HIT" if outcome == "WIN" else "STOPPED OUT"
    msg = arrow + " <b>" + symbol + " " + label + "</b>
"
    msg += direction + " position
"
    if entry >= 1:
        msg += "Entry: $" + str(round(entry,2)) + " → Exit: $" + str(round(exit_price,2)) + "
"
    else:
        msg += "Entry: $" + str(round(entry,6)) + " → Exit: $" + str(round(exit_price,6)) + "
"
    sign = "+" if pnl_pct >= 0 else ""
    msg += "P&L: " + sign + str(round(pnl_pct,2)) + "% | $100 → $" + str(round(usd_out,2))
    return _send(msg)

def send_news_alert(symbol: str, headline: str, sentiment: str, action: str = "") -> bool:
    """Send alert when breaking news affects an open position."""
    icon = "🔴" if sentiment == "BEARISH" else "🟠" if sentiment == "NEUTRAL" else "🟢"
    msg = icon + " <b>NEWS ALERT — " + symbol + "</b>
"
    msg += headline[:150] + "
"
    msg += "Sentiment: " + sentiment
    if action:
        msg += "
⚠️ " + action
    return _send(msg)

def send_weekly_summary(stats: dict) -> bool:
    """Send Friday weekly performance summary."""
    wins = stats.get("wins", 0)
    losses = stats.get("losses", 0)
    total = wins + losses
    wr = round(wins/total*100, 1) if total else 0
    pnl = stats.get("pnl_usd", 0)
    best = stats.get("best_pick", "")
    worst = stats.get("worst_pick", "")
    alltime_wr = stats.get("alltime_wr", 0)
    retrain_days = stats.get("retrain_in_days", 14)
    lines = [
        "<b>👻 GHOST WEEKLY SUMMARY</b>",
        "",
        "If you followed every pick this week:",
        str(wins) + " wins / " + str(losses) + " losses — " + str(wr) + "%",
    ]
    if pnl >= 0:
        lines.append("+$" + str(round(pnl,2)) + " on $1,000 deployed")
    else:
        lines.append("-$" + str(abs(round(pnl,2))) + " on $1,000 deployed")
    if best: lines.append("Best: " + best)
    if worst: lines.append("Worst: " + worst)
    lines.append("")
    lines.append("All-time accuracy: " + str(alltime_wr) + "%")
    lines.append("Model retrains in: " + str(retrain_days) + " days")
    lines.append("Next card: Monday 8 AM CT")
    return _send("
".join(lines))

def send_health_alert(issue: str) -> bool:
    """Send alert when system health degrades."""
    return _send("⚠️ <b>GHOST HEALTH ALERT</b>
" + issue)

def send_test() -> bool:
    """Test alert — call /api/test-alert to verify Telegram works."""
    return _send("👻 Ghost Protocol v2 — Telegram connected ✅")