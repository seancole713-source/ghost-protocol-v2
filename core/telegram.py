import os, logging, requests

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
            ok = r.ok
            if r.ok: LOGGER.info("Telegram OK")
            else: LOGGER.error("Telegram fail: " + r.text[:80])
        except Exception as e:
            LOGGER.error("Telegram error: " + str(e))
            ok = False
    if DISCORD_URL:
        try: requests.post(DISCORD_URL, json={"content": text}, timeout=10)
        except: pass
    return ok

def _fmt(v):
    if v is None: return "$0"
    return "$" + str(round(v, 2)) if v >= 1 else "$" + str(round(v, 6))

def send_morning_card(picks, week_stats=None, is_update=False):
    from datetime import datetime, timezone
    import pytz
    tz = pytz.timezone(os.getenv("GHOST_TZ", "America/Chicago"))
    day = datetime.now(tz).strftime("%A %b %d")
    label = "OPEN POSITIONS" if is_update else "PICKS"
    parts = ["<b>Ghost " + label + " -- " + day + "</b>"]
    if not picks:
        parts.append("No picks today.")
    else:
        for i, p in enumerate(picks[:10], 1):
            sym = p.get("symbol", "")
            direction = p.get("direction", "UP")
            entry = p.get("entry_price") or 0
            target = p.get("target_price") or 0
            stop = p.get("stop_price") or 0
            conf = int((p.get("confidence") or 0) * 100)
            pct = (target - entry) / entry * 100 if entry and direction == "UP" else (entry - target) / entry * 100 if entry else 0
            usd_out = round(100 * (1 + pct / 100), 2)
            exp_ts = p.get("expires_at") or 0
            if exp_ts:
                exp_str = datetime.fromtimestamp(float(exp_ts), tz=timezone.utc).astimezone(tz).strftime("%a %b %d")
            else:
                exp_str = "48hrs"
            lbl = "BUY" if direction == "UP" else "SELL"
            parts.append("")
            parts.append(str(i) + ". <b>" + sym + " -- " + lbl + "</b> (" + str(conf) + "% confident)")
            parts.append("   Get in at:   " + _fmt(entry))
            parts.append("   Get out at:  " + _fmt(target) + " (+" + str(round(pct, 1)) + "%)")
            parts.append("   Run away at: " + _fmt(stop))
            parts.append("   Done by:     " + exp_str)
            parts.append("   $100 in -- $" + str(usd_out) + " out")
            pos = p.get("pos_size_pct", 2.0)
            parts.append("   Risk: " + str(pos) + "% of your capital (e.g. $" + str(round(pos/100*1000,0)) + " of $1K)")
    if week_stats:
        w = week_stats.get("wins", 0)
        l = week_stats.get("losses", 0)
        pnl = week_stats.get("pnl_usd", 0)
        if w + l > 0:
            parts.append("")
            parts.append("Last 7 days: " + str(w) + "W/" + str(l) + "L | $" + str(round(pnl, 2)) + " if followed")
        parts.append("All-time: " + str(week_stats.get("alltime_wr", 0)) + "% accuracy")
    return _send(NL.join(parts))

def send_position_alert(symbol, direction, outcome, entry, exit_price, pnl_pct, usd_out):
    label = "TARGET HIT" if outcome == "WIN" else "STOPPED OUT"
    sign = "+" if pnl_pct >= 0 else ""
    parts = [
        "<b>" + symbol + " " + label + " -- " + outcome + "</b>",
        direction + " | " + _fmt(entry) + " to " + _fmt(exit_price),
        sign + str(round(pnl_pct, 2)) + "% | $100 to $" + str(round(usd_out, 2))
    ]
    return _send(NL.join(parts))

def send_news_alert(symbol, headline, sentiment, action=""):
    icon = "WARNING" if sentiment == "BEARISH" else "UPDATE"
    parts = ["<b>" + icon + " -- " + symbol + "</b>", headline[:150], "Sentiment: " + sentiment]
    if action: parts.append(action)
    return _send(NL.join(parts))

def send_weekly_summary(wins_or_stats, losses=None, wr=None, avg_win=None, avg_loss=None):
    # Accept either dict or positional args
    if isinstance(wins_or_stats, dict):
        stats = wins_or_stats
        wins = stats.get("wins", 0)
        losses = stats.get("losses", 0)
        total = wins + losses
        wr = round(wins / total * 100, 1) if total else 0
        avg_win = stats.get("avg_win", 0)
        avg_loss = stats.get("avg_loss", 0)
        alltime_wr = stats.get("alltime_wr", wr)
        retrain_days = stats.get("retrain_in_days", 14)
    else:
        stats = {}  # prevent NameError on stats.get() below
        wins = wins_or_stats or 0
        losses = losses or 0
        total = wins + losses
        wr = wr or 0
        avg_win = avg_win or 0
        avg_loss = avg_loss or 0
        alltime_wr = wr
        retrain_days = 14
    # P&L simulation: avg win/loss * $100 per trade
    pnl = (wins * (avg_win or 0) + losses * (avg_loss or 0)) if total else 0
    sign = "+" if pnl >= 0 else ""
    parts = [
        "<b>Ghost WEEKLY SUMMARY</b>",
        "",
        "If you followed every pick:",
        str(wins) + "W / " + str(losses) + "L -- " + str(wr) + "%",
        sign + "$" + str(round(pnl, 2)) + " on $1,000 deployed",
    ]
    if stats.get("best_pick"): parts.append("Best: " + stats["best_pick"])
    if stats.get("worst_pick"): parts.append("Worst: " + stats["worst_pick"])
    parts.extend([
        "",
        "All-time: " + str(stats.get("alltime_wr", wr)) + "% accuracy",
        "Model retrains in: " + str(stats.get("retrain_in_days", 14)) + " days",
        "Next card: Monday 8 AM CT",
    ])
    return _send(NL.join(parts))

def send_health_alert(issue):
    return _send(NL.join(["<b>GHOST HEALTH ALERT</b>", issue]))

def send_test():
    return _send("Ghost Protocol v2 -- Telegram connected OK")