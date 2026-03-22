import os, time, logging, requests
from typing import Optional

LOGGER = logging.getLogger("ghost.telegram")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DISCORD_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
ALERTS_ENABLED = os.getenv("ALERTS_ENABLED", "1") == "1"

def _send(text: str) -> bool:
    if not ALERTS_ENABLED:
        LOGGER.info("Alerts disabled")
        return True
    ok = True
    if BOT_TOKEN and CHAT_ID:
        try:
            url = "https://api.telegram.org/bot" + BOT_TOKEN + "/sendMessage"
            r = requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
            if not r.ok:
                LOGGER.error("Telegram failed: " + r.text[:100])
                ok = False
            else:
                LOGGER.info("Telegram sent OK")
        except Exception as e:
            LOGGER.error("Telegram error: " + str(e))
            ok = False
    if DISCORD_URL:
        try:
            requests.post(DISCORD_URL, json={"content": text}, timeout=10)
        except Exception as e:
            LOGGER.warning("Discord error: " + str(e))
    return ok

def send_morning_card(picks: list, week_stats: dict = None) -> bool:
    from datetime import datetime, timezone
    import pytz
    tz = pytz.timezone(os.getenv("GHOST_TZ", "America/Chicago"))
    now = datetime.now(tz)
    day = now.strftime("%A %b %d")
    lines = ["<b>Ghost PICKS -- " + day + "</b>"]
    if not picks:
        lines.append("No picks today -- market conditions not ideal.")
    else:
        for i, p in enumerate(picks[:10], 1):
            sym = p["symbol"]
            direction = p["direction"]
            arrow = "BUY" if direction == "UP" else "SELL"
            entry = p.get("entry_price", 0)
            target = p.get("target_price", 0)
            stop = p.get("stop_price", 0)
            conf = int(p.get("confidence", 0) * 100)
            if direction == "UP":
                pct = (target - entry) / entry * 100 if entry else 0
            else:
                pct = (entry - target) / entry * 100 if entry else 0
            usd_out = round(100 * (1 + pct/100), 2)
            if entry >= 1:
                ep = "$" + str(round(entry, 2))
                tp = "$" + str(round(target, 2))
                sp = "$" + str(round(stop, 2))
            else:
                ep = "$" + str(round(entry, 6))
                tp = "$" + str(round(target, 6))
                sp = "$" + str(round(stop, 6))
            exp_ts = p.get("expires_at", 0)
            if exp_ts:
                exp = datetime.fromtimestamp(float(exp_ts), tz=timezone.utc)
                exp_str = exp.astimezone(tz).strftime("%a %b %d")
            else:
                exp_str = "48hrs"
            lines.append("")
            lines.append(str(i) + ". <b>" + sym + " -- " + arrow + "</b> (" + str(conf) + "% confident)")
            lines.append("   Get in at:   " + ep)
            lines.append("   Get out at:  " + tp + " (+" + str(round(pct,1)) + "%)")
            lines.append("   Run away at: " + sp)
            lines.append("   Done by:     " + exp_str)
            lines.append("   $100 in -- $" + str(usd_out) + " out")
    if week_stats:
        w = week_stats.get("wins", 0)
        l = week_stats.get("losses", 0)
        pnl = week_stats.get("pnl_usd", 0)
        if w + l > 0:
            lines.append("")
            lines.append("Last 7 days: " + str(w) + "W/" + str(l) + "L | $" + str(round(pnl,2)) + " if followed")
        wr = week_stats.get("alltime_wr", 0)
        lines.append("All-time: " + str(wr) + "% accuracy")
    return _send("
".join(lines))

def send_position_alert(symbol: str, direction: str, outcome: str, entry: float, exit_price: float, pnl_pct: float, usd_out: float) -> bool:
    label = "TARGET HIT" if outcome == "WIN" else "STOPPED OUT"
    status = "WIN" if outcome == "WIN" else "LOSS"
    if entry >= 1:
        entry_str = "$" + str(round(entry,2))
        exit_str = "$" + str(round(exit_price,2))
    else:
        entry_str = "$" + str(round(entry,6))
        exit_str = "$" + str(round(exit_price,6))
    sign = "+" if pnl_pct >= 0 else ""
    msg = "<b>" + symbol + " " + label + " -- " + status + "</b>
"
    msg += direction + " | " + entry_str + " to " + exit_str + "
"
    msg += sign + str(round(pnl_pct,2)) + "% | $100 to $" + str(round(usd_out,2))
    return _send(msg)

def send_news_alert(symbol: str, headline: str, sentiment: str, action: str = "") -> bool:
    icon = "WARNING" if sentiment == "BEARISH" else "UPDATE"
    msg = "<b>" + icon + " -- " + symbol + "</b>
"
    msg += headline[:150] + "
"
    msg += "Sentiment: " + sentiment
    if action:
        msg += "
" + action
    return _send(msg)

def send_weekly_summary(stats: dict) -> bool:
    wins = stats.get("wins", 0)
    losses = stats.get("losses", 0)
    total = wins + losses
    wr = round(wins/total*100, 1) if total else 0
    pnl = stats.get("pnl_usd", 0)
    best = stats.get("best_pick", "")
    worst = stats.get("worst_pick", "")
    alltime_wr = stats.get("alltime_wr", 0)
    retrain_days = stats.get("retrain_in_days", 14)
    msg = "<b>Ghost WEEKLY SUMMARY</b>

"
    msg += "If you followed every pick:
"
    msg += str(wins) + "W / " + str(losses) + "L -- " + str(wr) + "%
"
    sign = "+" if pnl >= 0 else ""
    msg += sign + "$" + str(round(pnl,2)) + " on $1,000 deployed
"
    if best: msg += "Best: " + best + "
"
    if worst: msg += "Worst: " + worst + "
"
    msg += "
All-time: " + str(alltime_wr) + "% accuracy"
    msg += "
Model retrains in: " + str(retrain_days) + " days"
    msg += "
Next card: Monday 8 AM CT"
    return _send(msg)

def send_health_alert(issue: str) -> bool:
    return _send("<b>GHOST HEALTH ALERT</b>
" + issue)

def send_test() -> bool:
    msg = "Ghost Protocol v2 -- Telegram connected OK"
    return _send(msg)