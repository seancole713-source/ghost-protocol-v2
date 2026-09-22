"""Phone messages -- few, plain, and never louder than the evidence.

Four kinds, each at most once per trading day (a stored marker):
  card       09:05-09:28 ET  the shadow card's headline
  duty_1030  10:25-10:30 ET  the operator's Gap-and-Go duty: cancel unfilled entries
  duty_1530  15:25-15:30 ET  the operator's duty: sell anything still open
  graded     16:20-20:00 ET  what the shadow's forecasts did

The two duty reminders exist because they are the steps in the operator's own
manual trading that cost real money when forgotten -- bracket children are DAY
orders and an unfilled entry or an open position does not manage itself.

Delivery: Telegram Bot API with Ghost's TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.
The token is part of the URL, so no URL is ever logged.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

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


def card_text(card: Dict[str, Any]) -> str:
    fc = card.get("forecasts") or []
    base = card.get("baseline_forecasts") or []
    lines = [f"edge shadow {card.get('day')}: " + (", ".join(fc) if fc else "no setups")
             + f"  (baseline: {', '.join(base) if base else 'none'})",
             card.get("coverage_note") or "",
             "Shadow + paper only. Not a trade recommendation."]
    return "\n".join(x for x in lines if x)


DUTY_1030 = ("Gap-and-Go v1: cancel any UNFILLED entry by 10:30 ET. "
             "(Skip if you placed nothing today.)")
DUTY_1530 = ("Gap-and-Go v1: SELL anything still open by 3:30 ET -- bracket orders expire at 4:00 "
             "and leave the position unprotected overnight. (Skip if you're flat.)")


def graded_text(day: str, settled: Dict[str, Any]) -> Optional[str]:
    if not settled:
        return None
    parts = []
    for k, v in sorted(settled.items()):
        pnl = v.get("pnl_usd")
        parts.append(f"{k.split(':')[-1]} [{k.split('@')[0]}] {v.get('simulated')}"
                     + (f" {pnl:+.2f}" if isinstance(pnl, (int, float)) else ""))
    return f"edge shadow graded {day}:\n" + "\n".join(parts) + "\n(simulated, $1,000 size, 10bps costs)"


def misses_text(review: Dict[str, Any]) -> Optional[str]:
    """The previous session's +5% movers: what was catchable, what was caught, why not."""
    if not review or not review.get("movers"):
        return None
    labels = {k: v for k, v in (review.get("labels") or {}).items() if v}
    top = sorted((r for r in review.get("rows") or [] if r.get("opportunity") == "EXECUTABLE"),
                 key=lambda r: -(r.get("move_pct") or 0))[:5]
    lines = [f"Movers review {review.get('day')}: {review.get('movers')} stocks hit +5%, "
             f"{review.get('executable')} were tradeable after the open, caught {review.get('caught')}."]
    if labels:
        lines.append("Missed because: " + ", ".join(f"{k.lower().replace('_', ' ')} {v}" for k, v in sorted(labels.items())))
    if top:
        lines.append("Biggest: " + ", ".join(f"{r['symbol']} +{r['move_pct']:.0f}% ({(r.get('label') or '').lower().replace('_', ' ')})"
                                             for r in top))
    if review.get("gap_only"):
        lines.append(f"{review['gap_only']} more gained only in the gap -- nothing to buy after the open.")
    return "\n".join(lines)
