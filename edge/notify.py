"""Phone messages -- few, plain, and never louder than the evidence.

Each kind at most once per trading day (a stored marker). Times ET (CT = ET - 1h):
  card       09:05-09:28 ET  the shadow card as a trade: levels, size, risk, clock in CT
  duty_1030  10:20-10:30 ET  the operator's Gap-and-Go duty: cancel unfilled entries
  duty_1530  15:20-15:30 ET  the operator's duty: sell anything still open
  graded     16:20-20:00 ET  what the shadow's forecasts did
  problem_*  any time        PROBLEM alerts: no card by 09:28 ET, a failed step
                             (09:00-16:00 ET), grading not done by 17:00 ET

The two duty reminders exist because they are the steps in the operator's own
manual trading that cost real money when forgotten -- bracket children are DAY
orders and an unfilled entry or an open position does not manage itself. They go
out every regular trading day, whatever Ghost's card holds: the operator's own
card is built elsewhere and can hold a name Ghost's shadow card does not.

Delivery: Telegram Bot API with Ghost's TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.
The token is part of the URL, so no URL is ever logged.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, time as dtime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

TELEGRAM = "https://api.telegram.org"


def _creds():
    tok = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    return (tok, chat) if tok and chat else (None, None)


def send(http, text: str) -> Dict[str, Any]:
    tok, chat = _creds()
    if not tok:
        return {"status": "no_telegram_config"}
    try:
        r = http.post(f"{TELEGRAM}/bot{tok}/sendMessage", timeout=10,
                      json={"chat_id": chat, "text": text, "disable_web_page_preview": True})
        return {"status": "sent" if 200 <= r.status_code < 300 else "error", "http_status": r.status_code}
    except Exception as exc:  # noqa: BLE001 - never raise from a notification
        return {"status": "error", "error": type(exc).__name__}


def once(http, store, *, day: str, kind: str, text: str) -> Dict[str, Any]:
    key = f"{day}|{kind}"
    if store.get("edge_notify", key):
        return {"status": "already_sent"}
    res = send(http, text)
    if res["status"] == "sent":
        store.put("edge_notify", key, {"day": day, "kind": kind, "text": text})
    return res


CT = ZoneInfo("America/Chicago")
CARD_MAX_CHARS = 1500           # Telegram's limit is 4096; a phone card should fit a screen or two
CATALYST_CHARS = 80


def _ct(ts: Optional[int]) -> Optional[str]:
    """An epoch as the operator reads it: '9:30am CT'."""
    if not ts:
        return None
    t = datetime.fromtimestamp(int(ts), tz=CT)
    return f"{t.hour % 12 or 12}:{t.minute:02d}{'am' if t.hour < 12 else 'pm'} CT"


def _clock_ct(day: Optional[str], hhmm_et: str) -> Optional[str]:
    """A spec's ET clock time ('10:30') on `day`, in CT."""
    try:
        from edge.contracts import ET
        hh, mm = (int(x) for x in hhmm_et.split(":"))
        return _ct(int(datetime.combine(date.fromisoformat(str(day)), dtime(hh, mm), tzinfo=ET).timestamp()))
    except Exception:  # noqa: BLE001 - a malformed card still gets a card
        return None


def _money(x: Any) -> str:
    return f"${float(x):,.2f}" if isinstance(x, (int, float)) else "?"


def _num(*xs: Any) -> bool:
    return all(isinstance(x, (int, float)) for x in xs)


def _trades(card: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Each Gap-and-Go forecast with its frozen levels.

    A card carries `trades` (copied from the recorded forecasts when it is issued).
    A card without them has only the symbol list and its rows; the levels are then
    rebuilt from the row's reference price with the same frozen spec, which is
    deterministic -- the same numbers the ledger holds."""
    if card.get("trades"):
        return list(card["trades"])
    rows = {r.get("symbol"): r for r in card.get("rows") or []}
    out = []
    for sym in card.get("forecasts") or []:
        r = rows.get(sym) or {}
        t = {"symbol": sym, "ref_price": r.get("ref_price"), "prev_close": r.get("prev_close"),
             "catalyst": r.get("catalyst")}
        if r.get("ref_price"):
            try:
                from edge.contracts import issue
                from edge.pipeline import SPEC
                f = issue(SPEC, symbol=sym, session_date=date.fromisoformat(card["day"]),
                          entry_ref=r["ref_price"], issued_at=int(card.get("issued_at") or 0))
                t.update({k: getattr(f, k) for k in ("entry_trigger", "entry_limit", "target", "stop",
                                                     "shares", "entry_expiry", "time_exit")})
            except Exception:  # noqa: BLE001 - shown without levels rather than not at all
                pass
        out.append(t)
    return out


def _data_warning(card: Dict[str, Any]) -> Optional[str]:
    """A card built on broken data must not read like a quiet day."""
    parts = []
    banner = str(card.get("health_banner") or "")
    if "paused" in banner.lower() or "incomplete" in banner.lower():
        parts.append(banner)
    errs = card.get("source_errors") or {}
    if errs:
        parts.append("source failed: " + ", ".join(sorted(errs)))
    if card.get("movers_stale"):
        parts.append("movers list stale")
    return ("DATA WARNING: " + "; ".join(parts))[:300] if parts else None


def _trade_lines(i: int, t: Dict[str, Any]) -> List[str]:
    ref, prev = t.get("ref_price"), t.get("prev_close")
    head = f"{i}) {t.get('symbol')}"
    if _num(ref):
        head += f"  ref {_money(ref)}"
        if _num(prev) and prev > 0:
            head += f", gap {(ref / prev - 1) * 100:+.1f}% vs {_money(prev)} close"
    lines = [head]
    trig, lim, tgt, stop, sh = (t.get(k) for k in ("entry_trigger", "entry_limit", "target", "stop", "shares"))
    if _num(trig, lim, tgt, stop, sh):
        lines.append(f"   BUY STOP {_money(trig)}, limit {_money(lim)} | target {_money(tgt)} | stop {_money(stop)}")
        # Worst case under the rule: filled at the limit, stopped at the stop. A gap
        # through the stop can lose more; "~" says so rather than promise.
        lines.append(f"   {int(sh)} shares (~${sh * trig:,.0f}) | max loss ~${(lim - stop) * sh:,.0f} at stop")
    else:
        lines.append("   levels unavailable -- do not trade this line")
    cat = " ".join(str(t.get("catalyst") or "").split())
    if cat:
        lines.append("   Catalyst: " + (cat if len(cat) <= CATALYST_CHARS else cat[:CATALYST_CHARS - 3] + "..."))
    return lines


def card_text(card: Dict[str, Any]) -> str:
    """The morning card as a trade the operator can act on from a phone: levels, size,
    dollar risk and the clock, all in Central time."""
    day = card.get("day")
    trades = _trades(card)
    lines: List[str] = []
    warn = _data_warning(card)
    if warn:
        lines.append(warn)
    lines.append(f"Ghost Gap-and-Go card {day} (shadow + paper)")
    if trades:
        for i, t in enumerate(trades, 1):
            lines.extend(_trade_lines(i, t))
        expiry = _ct(trades[0].get("entry_expiry")) or _clock_ct(day, "10:30") or "9:30am CT"
        exit_ = _ct(trades[0].get("time_exit")) or _clock_ct(day, "15:30") or "2:30pm CT"
        lines.append(f"Entry expires {expiry} if not filled. Exit everything by {exit_}.")
    elif card.get("status") == "early_close":
        lines.append("No trade today -- early close: the rule's 2:30pm CT exit falls after the close.")
    else:
        lines.append("No trade today -- nothing passed the Gap-and-Go rule.")
    base = card.get("baseline_forecasts") or []
    lines.append("Baseline (no catalyst check): " + (", ".join(base) if base else "none"))
    ranked = card.get("top10_ranked")
    if ranked:
        lines.append("Top 10 (learning list, not trades): " + ", ".join(
            f"{x.get('rank')}.{x.get('symbol')}" + (f" {x['score']:.0f}" if _num(x.get("score")) else "")
            for x in ranked))
    elif card.get("top10"):
        lines.append("Top 10 (learning list, not trades): "
                     + ", ".join(f"{i}.{s}" for i, s in enumerate(card["top10"], 1)))
    if card.get("coverage_note"):
        lines.append(str(card["coverage_note"]))
    lines.append("Paper only. Real money only by your hand, $1,000 per trade.")
    text = "\n".join(x for x in lines if x)
    return text if len(text) <= CARD_MAX_CHARS else text[:CARD_MAX_CHARS - 3] + "..."


# The operator reads Central time (the market opens 8:30am CT = 9:30am ET);
# every message leads with CT and keeps ET in brackets for the record. Sent on
# every regular trading day, so each says when to skip it.
DUTY_1030 = ("Gap-and-Go v1: cancel any UNFILLED entry by 9:30am CT (10:30 ET). "
             "(Skip if you placed nothing today.)")
DUTY_1530 = ("Gap-and-Go v1: SELL anything still open by 2:30pm CT (3:30 ET) -- bracket orders expire at "
             "the 3:00pm CT close and leave the position unprotected overnight. "
             "(Skip if you placed nothing today or are already flat.)")

# ---------------------------------------------------------------- problems --
# A broken day must never look like a quiet one. Each goes out at most once per
# kind per day (the `once` marker), only through the existing notifier.
PROBLEM_NO_CARD = ("PROBLEM: no card by 9:28 ET/8:28 CT -- Ghost's shadow card was not written. "
                   "Treat today as UNKNOWN, not quiet.")

_SECRET = re.compile(r"(?i)((?:api[_-]?key|apikey|token|secret|key|password)=)[^&\s'\"]+")
_BOT = re.compile(r"/bot[^/\s]+/")


def redact(text: Any) -> str:
    """Error strings can carry a request URL; never forward a key that rides in one."""
    return _BOT.sub("/bot***/", _SECRET.sub(r"\1***", str(text)))


def problem_text(step: str, result: Dict[str, Any], now: int) -> str:
    err = result.get("error") or result.get("message") or (
        f"HTTP {result['http_status']}" if result.get("http_status") else "status error")
    return f"PROBLEM: {step} failed ({_ct(now)}): {redact(err)[:200]}"


def grading_problem_text(resolve: Optional[str], graded: Optional[str]) -> str:
    return ("PROBLEM: tonight's grading not done by 4:00pm CT (5:00 ET): "
            f"resolve={resolve or 'did not run'}, card grade={graded or 'did not run'}. "
            "Today's results are not recorded yet.")


def graded_text(day: str, settled: Dict[str, Any]) -> Optional[str]:
    if not settled:
        return None
    parts = []
    for k, v in sorted(settled.items()):
        pnl = v.get("pnl_usd")
        parts.append(f"{k.split(':')[-1]} [{k.split('@')[0]}] {v.get('simulated')}"
                     + (f" {pnl:+.2f}" if isinstance(pnl, (int, float)) else ""))
    return f"edge shadow graded {day}:\n" + "\n".join(parts) + "\n(simulated, $1,000 size, 10bps costs)"


_MISS_PHRASE = {
    "UNIVERSE_COVERAGE": "outside the universe",
    "DATA_INTERRUPTION": "data down",
    "CATALYST_MISSED": "news on file, never linked",
    "DETECTION_FAILURE": "never seen",
    "STRATEGY_REJECTION": "seen, rule rejected",
    "RISK_LIQUIDITY_EXCLUSION": "liquidity/risk excluded",
    "ALERT_EXECUTION_FAILURE": "forecast late or never filled",
}


def _miss_phrase(row: Dict[str, Any]) -> str:
    lab = row.get("label") or ""
    if lab == "CAUGHT":
        return f"caught by {row['caught_by']}" if row.get("caught_by") else "caught"
    if lab in ("STRATEGY_REJECTION", "RISK_LIQUIDITY_EXCLUSION") and row.get("seen_by"):
        who = "radar" if "radar" in row["seen_by"] else "card"
        rule = "rule rejected" if lab == "STRATEGY_REJECTION" else "liquidity/risk excluded"
        return f"{who} saw it, {rule}"
    return _MISS_PHRASE.get(lab, lab.lower().replace("_", " "))


def misses_text(review: Dict[str, Any]) -> Optional[str]:
    """The previous session's +5% movers: what was catchable, what was caught, why not."""
    if not review or not review.get("movers"):
        return None
    ex = [r for r in review.get("rows") or [] if r.get("opportunity") == "EXECUTABLE"]
    top = sorted(ex, key=lambda r: -(r.get("move_pct") or 0))[:5]
    lines = [f"Movers review {review.get('day')}: {review.get('movers')} stocks hit +5% at their peak, "
             f"{review.get('executable')} were tradeable after the open, caught {review.get('caught')}."]
    if review.get("labels_version") and review.get("executable"):
        cb, n = review.get("caught_by") or {}, review["executable"]
        lines.append(f"Caught by the card {cb.get('card', 0)}/{n}, by the radar {cb.get('radar', 0)}/{n} "
                     f"(the radar saw {review.get('radar_seen', 0)}, the card {review.get('card_seen', 0)}).")
    if any("seen_by" in r for r in ex):          # v2 review: the reasons are read from the rows
        counts: Dict[str, int] = {}
        for r in ex:
            if r.get("label") and r["label"] != "CAUGHT":
                counts[_miss_phrase(r)] = counts.get(_miss_phrase(r), 0) + 1
    else:
        counts = {_MISS_PHRASE.get(k, k.lower().replace("_", " ")): v
                  for k, v in (review.get("labels") or {}).items() if v}
    if counts:
        lines.append("Missed because: " + "; ".join(
            f"{k}: {v}" for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))))
    if top:
        lines.append("Biggest (peak / close): " + ", ".join(
            f"{r['symbol']} +{r['move_pct']:.0f}%"
            + (f" / {r['close_pct']:+.0f}%" if isinstance(r.get("close_pct"), (int, float)) else "")
            + f" ({_miss_phrase(r)})" for r in top))
    if review.get("gap_only"):
        lines.append(f"{review['gap_only']} more gained only in the gap -- nothing to buy after the open.")
    return "\n".join(lines)


def probe(http):
    """Can the bot reach the operator's chat? getChat checks token AND chat without sending.
    Never logs the URL: the token lives in it."""
    from edge.providers import base as B
    tok, chat = _creds()
    if not tok:
        return B.Probe("notify.telegram", "telegram", B.NO_KEY, note="TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
    try:
        r = http.get(f"{TELEGRAM}/bot{tok}/getChat", params={"chat_id": chat}, timeout=10)
    except Exception as exc:  # noqa: BLE001
        return B.Probe("notify.telegram", "telegram", B.ERROR, note=type(exc).__name__)
    if r.status_code >= 400:
        desc = ""
        try:
            desc = str((r.json() or {}).get("description") or "")[:120]
        except Exception:  # noqa: BLE001
            pass
        return B.Probe("notify.telegram", "telegram", B.classify(r.status_code), http_status=r.status_code,
                       note=f"bot cannot reach the chat: {desc}")
    return B.Probe("notify.telegram", "telegram", B.OK, http_status=r.status_code, rows=1,
                   note="bot can reach the operator's chat (nothing sent)")
