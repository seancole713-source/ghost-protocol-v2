import os, time, logging, requests
from typing import Optional

LOGGER = logging.getLogger("ghost.telegram")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DISCORD_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
ALERTS_ENABLED = os.getenv("ALERTS_ENABLED", "1") == "1"
NL = chr(10)

def _send(text):
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

def send_morning_card(picks, week_stats=None):
    from datetime import datetime, timezone
    import pytz
    tz = pytz.timezone(os.getenv("GHOST_TZ", "America/Chicago"))
    now = datetime.now(tz)
    day = now.strftime("%A %b %d")
    parts = ["<b>Ghost PICKS -- " + day + "</b>"]
    if not picks:
        parts.append("No picks today -- conditions not ideal.")
    else:
        for i, p in enumerate(picks[:10], 1):
            sym = p["symbol"]
            direction = p["direction"]
            arrow = "BUY" if direction == "UP" else "SELL"
            entry = p.get("entry_price", 0) or 0
            target = p.get("target_price", 0) or 0
            stop = p.get("stop_price", 0) or 0
            conf = int((p.get("confidence", 0) or 0) * 100)
            pct = (target - entry) / entry * 100 if entry and direction == "UP" else (entry - target) / entry * 100 if entry else 0
            usd_out = round(100 * (1 + pct/100), 2)
            fmt = lambda v: "$" + str(round(v, 2)) if v >= 1 else "$" + str(round(v, 6))
            exp_ts = p.get("expires_at", 0)
            if exp_ts:
                exp = datetime.fromtimestamp(float(exp_ts), tz=timezone.utc)
                exp_str = exp.astimezone(tz).strftime("%a %b %d")
            else:
                exp_str = "48hrs"
            parts.append("")
            parts.append(str(i) + ". <b>" + sym + " -- " + arrow + "</b> (" + str(conf) + "% confident)")
            parts.append("   Get in at:   " + fmt(entry))
            parts.append("   Get out at:  " + fmt(target) + " (+" + str(round(pct,1)) + "%)")
            parts.append("   Run away at: " + fmt(stop))
            parts.append("   Done by:     " + exp_str)
            parts.append("   $100 in -- $" + str(usd_out) + " out")
    if week_stats:
        w = week_stats.get("wins", 0)
        l = week_stats.get("losses", 0)
        pnl = week_stats.get("pnl_usd", 0)
        if w + l > 0:
            parts.append("")
            parts.append("Last 7 days: " + str(w) + "W/" + str(l) + "L | $" + str(round(pnl,2)) + " if followed")
        wr = week_stats.get("alltime_wr", 0)
        parts.append("All-time: " + str(wr) + "% accuracy")
    return _send(NL.join(parts))

def send_position_alert(symbol, direction, outcome, entry, exit_price, pnl_pct, usd_out):
    label = "TARGET HIT" if outcome == "WIN" else "STOPPED OUT"
    status = "WIN" if outcome == "WIN" else "LOSS"
    fmt = lambda v: "$" + str(round(v, 2)) if v >= 1 else "$" + str(round(v, 6))
    sign = "+" if pnl_pct >= 0 else ""
    msg = "<b>" + symbol + " " + label + " -- " + status + "</b>" + NL
    msg += direction + " | " + fmt(entry) + " to " + fmt(exit_price) + NL
    msg += sign + str(round(pnl_pct,2)) + "% | $100 to $" + str(round(usd_out,2))
    return _send(msg)

def send_news_alert(symbol, headline, sentiment, action=""):
    icon = "WARNING" if sentiment == "BEARISH" else "UPDATE"
    msg = "<b>" + icon + " -- " + symbol + "</b>" + NL
    msg += headline[:150] + NL
    msg += "Sentiment: " + sentiment
    if action:
        msg += NL + action
    return _send(msg)

def send_weekly_summary(stats):
    wins = stats.get("wins", 0)
    losses = stats.get("losses", 0)
    total = wins + losses
    wr = round(wins/total*100, 1) if total else 0
    pnl = stats.get("pnl_usd", 0)
    sign = "+" if pnl >= 0 else ""
    msg = "<b>Ghost WEEKLY SUMMARY</b>" + NL + NL
    msg += "If you followed every pick:" + NL
    msg += str(wins) + "W / " + str(losses) + "L -- " + str(wr) + "%" + NL
    msg += sign + "$" + str(round(pnl,2)) + " on $1,000 deployed" + NL
    best = stats.get("best_pick", "")
    worst = stats.get("worst_pick", "")
    if best: msg += "Best: " + best + NL
    if worst: msg += "Worst: " + worst + NL
    msg += NL + "All-time: " + str(stats.get("alltime_wr",0)) + "% accuracy"
    msg += NL + "Model retrains in: " + str(stats.get("retrain_in_days",14)) + " days"
    msg += NL + "Next card: Monday 8 AM CT"
    return _send(msg)

def send_health_alert(issue):
    return _send("<b>GHOST HEALTH ALERT</b>" + NL + issue)

def send_test():
    return _send("Ghost Protocol v2 -- Telegram connected OK")
