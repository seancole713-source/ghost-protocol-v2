import os, logging, requests, time

LOGGER = logging.getLogger("ghost.telegram")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DISCORD_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
ALERTS_ENABLED = os.getenv("ALERTS_ENABLED", "1") == "1"
NL = chr(10)

# P1-2 (audit): retry + dead-letter queue for Telegram alerts
_TELEGRAM_RETRIES = max(1, int(os.getenv("TELEGRAM_RETRIES", "3")))
_TELEGRAM_RETRY_BACKOFF_S = [2.0, 4.0, 8.0]  # per-attempt backoff


def _send(text):
    """Send to Telegram + Discord with retry. On final failure, write to dead-letter queue."""
    if not ALERTS_ENABLED:
        LOGGER.info("Alerts disabled")
        return True
    ok = True
    if BOT_TOKEN and CHAT_ID:
        last_err = None
        for attempt in range(_TELEGRAM_RETRIES):
            try:
                url = "https://api.telegram.org/bot" + BOT_TOKEN + "/sendMessage"
                r = requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
                if r.ok:
                    LOGGER.info("Telegram OK (attempt %s)", attempt + 1)
                    break
                last_err = f"HTTP {r.status_code}: {r.text[:80]}"
                LOGGER.error("Telegram fail (attempt %s): %s", attempt + 1, last_err)
            except Exception as e:
                last_err = str(e)[:160]
                LOGGER.error("Telegram error (attempt %s): %s", attempt + 1, last_err)
            if attempt + 1 < _TELEGRAM_RETRIES:
                backoff = _TELEGRAM_RETRY_BACKOFF_S[min(attempt, len(_TELEGRAM_RETRY_BACKOFF_S) - 1)]
                time.sleep(backoff)
        else:
            # All retries exhausted — write to dead-letter queue
            ok = False
            LOGGER.error("Telegram dead-letter: all %s retries failed — %s", _TELEGRAM_RETRIES, last_err)
            try:
                _enqueue_dead_letter(text, last_err or "unknown")
            except Exception as _dle:
                LOGGER.error("Dead-letter write failed: %s", str(_dle)[:80])
    if DISCORD_URL:
        try:
            requests.post(DISCORD_URL, json={"content": text}, timeout=10)
        except Exception:
            pass
    return ok


def _enqueue_dead_letter(text: str, error: str) -> None:
    """Append a failed alert to ghost_state.telegram_dead_letter (last 50)."""
    import json as _j
    from core.db import db_conn
    entry = {"ts": int(time.time()), "text": text[:500], "error": error[:200]}
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
            cur.execute("SELECT val FROM ghost_state WHERE key='telegram_dead_letter'")
            row = cur.fetchone()
            queue = []
            if row and row[0]:
                try:
                    queue = _j.loads(row[0])
                except Exception:
                    queue = []
            if not isinstance(queue, list):
                queue = []
            queue.append(entry)
            queue = queue[-50:]
            cur.execute(
                "INSERT INTO ghost_state(key,val) VALUES('telegram_dead_letter',%s) "
                "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val", (_j.dumps(queue),))
    except Exception:
        pass  # best-effort; never raise into the caller


def get_dead_letter_queue() -> list:
    """Read dead-letter queue for /api/admin/telegram/dead-letter."""
    import json as _j
    from core.db import db_conn
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT val FROM ghost_state WHERE key='telegram_dead_letter'")
            row = cur.fetchone()
            if row and row[0]:
                return _j.loads(row[0])
    except Exception:
        pass
    return []


def replay_dead_letter(index: int) -> dict:
    """Replay one dead-letter entry by index (0 = oldest)."""
    queue = get_dead_letter_queue()
    if not queue or index < 0 or index >= len(queue):
        return {"ok": False, "error": "invalid index"}
    entry = queue[index]
    ok = _send(entry.get("text", ""))
    if ok:
        # Remove from queue on success
        import json as _j
        from core.db import db_conn
        queue.pop(index)
        try:
            with db_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO ghost_state(key,val) VALUES('telegram_dead_letter',%s) "
                    "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val", (_j.dumps(queue),))
        except Exception:
            pass
    return {"ok": ok, "entry": entry, "remaining": len(queue)}

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
            try:
                from core.risk_discipline import position_sizing_plan
                sz = position_sizing_plan(entry, stop, confidence=p.get("confidence"))
                if sz.get("ok"):
                    parts.append(
                        "   Size (1% risk): " + str(sz["suggested_shares"]) + " sh (~$"
                        + str(int(sz["suggested_notional_usd"])) + ") · max loss $"
                        + str(round(sz["max_loss_usd"], 0))
                    )
            except Exception:
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

def send_pick_withdrawn(symbol, reason, entry, exit_price, pnl_pct):
    """Alert when Ghost withdraws an open pick mid-trade."""
    sign = "+" if float(pnl_pct or 0) >= 0 else ""
    parts = [
        "<b>Ghost withdrew " + str(symbol) + " pick</b>",
        "Reason: " + str(reason).replace("_", " "),
        "Was: " + _fmt(float(entry or 0)) + " → now " + _fmt(float(exit_price or 0)),
        "Mark: " + sign + str(round(float(pnl_pct or 0), 2)) + "% (not a WIN/LOSS — signal withdrawn)",
        "Ghost keeps scanning; a fresh pick may appear if gates clear again.",
    ]
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

def send_daily_card(data):
    """Overhauled WOLF daily card (prediction + price levels + news influence +
    rates + track record). `data` is assembled by the caller; see
    core.telegram_cards.format_daily_card."""
    from core.telegram_cards import format_daily_card
    return _send(format_daily_card(data))


def send_silence_card(data):
    """Daily SILENCE card when no pick clears the high-conviction threshold."""
    from core.telegram_cards import format_silence_card
    return _send(format_silence_card(data))


def send_weekly_card(data):
    """Overhauled weekly summary (followed-picks P&L, all-time, retrain
    countdown, top/weakest pick, news-driven count)."""
    from core.telegram_cards import format_weekly_summary
    return _send(format_weekly_summary(data))


def send_health_alert(issue):
    return _send(NL.join(["<b>GHOST HEALTH ALERT</b>", issue]))


def send_risk_discipline_alert(title, body):
    return _send(NL.join(["<b>GHOST RISK — " + str(title) + "</b>", str(body)]))

def send_test():
    return _send("Ghost Protocol v2 -- Telegram connected OK")


def send_telegram_message(text) -> bool:
    """Public alert sender used by the live runtime alert paths.

    ``core/squeeze_monitor.py`` and ``core/wolf_monitor.py`` import
    ``send_telegram_message`` (historically from a never-created
    ``core.telegram_hunter`` module). This is the canonical implementation;
    ``core/telegram_hunter.py`` is a thin compatibility shim that re-exports it.
    Returns True on success (or when alerts are disabled), False on failure.
    """
    return _send(str(text))
