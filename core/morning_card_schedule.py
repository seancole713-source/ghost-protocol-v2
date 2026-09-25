"""Fixed wall-clock schedule for the core morning card (audit F31, 2026-09-25).

The core morning_card job used to be registered with ``interval_s=86400``, so
it fired 24 h after whenever the leader booted (07:39Z boot -> 07:39Z card),
and its boot "self-heal" ran only when the booting process won the leader
lock -- on a rolling deploy the new replica is a follower at boot, so the
recovery was skipped. With 15 deploys in four days the card effectively never
fired from the scheduler.

Now the scheduler ticks every few minutes and this module decides, from the
ET wall clock and the persisted ``ghost_state`` send record, whether the card
is due. The same check runs on the first tick after ANY process becomes
leader (boot or takeover), which is the self-heal; it is idempotent because
the send is keyed on ``last_morning_card_date`` (written only after a
successful send) and retries are spaced by ``last_morning_card_attempt_ts``.

Window (the edge jobs' convention: America/New_York wall-clock windows):
  * start: ``MORNING_CARD_ET`` ("HH:MM" ET, default 09:00 ET = 08:00 CT, the
    card's documented "8 AM CT"). If only the legacy ``TELEGRAM_DAILY_HOUR``
    (a CT hour) is set, that hour in America/Chicago is used.
  * length: ``MORNING_CARD_WINDOW_MIN`` (default 240, the old self-heal's
    "+4h" window).
  * retry spacing: ``MORNING_CARD_RETRY_MIN`` (default 30) between attempts
    that did not record a send (e.g. dead-lettered Telegram).
"""
from __future__ import annotations

import os
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")

_DEFAULT_START_ET = (9, 0)
_DEFAULT_WINDOW_MIN = 240
_DEFAULT_RETRY_MIN = 30


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, "").strip() or default)
    except ValueError:
        value = default
    return max(low, min(high, value))


def _start_for(day_et: date) -> datetime:
    """Window start for an ET calendar day, as an aware ET datetime."""
    raw = os.getenv("MORNING_CARD_ET", "").strip()
    if raw:
        try:
            hh, mm = (int(part) for part in raw.split(":", 1))
            if 0 <= hh < 24 and 0 <= mm < 60:
                return datetime.combine(day_et, dtime(hh, mm), tzinfo=ET)
        except ValueError:
            pass
    legacy = os.getenv("TELEGRAM_DAILY_HOUR", "").strip()
    if legacy:
        try:
            hour_ct = int(legacy)
            if 0 <= hour_ct < 24:
                return datetime.combine(day_et, dtime(hour_ct, 0), tzinfo=CT).astimezone(ET)
        except ValueError:
            pass
    return datetime.combine(day_et, dtime(*_DEFAULT_START_ET), tzinfo=ET)


def window(now_ts: float) -> Dict[str, Any]:
    """Today's morning-card window around ``now_ts`` (epoch seconds)."""
    now_et = datetime.fromtimestamp(now_ts, tz=ET)
    start = _start_for(now_et.date())
    length = _int_env("MORNING_CARD_WINDOW_MIN", _DEFAULT_WINDOW_MIN, 30, 720)
    end = start + timedelta(minutes=length)
    return {
        "start_ts": int(start.timestamp()),
        "end_ts": int(end.timestamp()),
        "start_et": start.strftime("%H:%M ET"),
        # The send record (last_morning_card_date) is a CT calendar date.
        "card_date": datetime.fromtimestamp(now_ts, tz=CT).strftime("%Y-%m-%d"),
    }


def due(
    now_ts: float,
    *,
    last_sent_date: Optional[str],
    last_attempt_ts: Optional[float],
) -> Dict[str, Any]:
    """Whether the card should be attempted now, with the reason."""
    win = window(now_ts)
    if now_ts < win["start_ts"]:
        return {"due": False, "reason": "before_window", **win}
    if now_ts >= win["end_ts"]:
        return {"due": False, "reason": "after_window", **win}
    if last_sent_date == win["card_date"]:
        return {"due": False, "reason": "already_sent", **win}
    retry_s = 60 * _int_env("MORNING_CARD_RETRY_MIN", _DEFAULT_RETRY_MIN, 5, 240)
    if last_attempt_ts and win["start_ts"] <= float(last_attempt_ts) and now_ts - float(last_attempt_ts) < retry_s:
        return {"due": False, "reason": "retry_backoff", **win}
    return {"due": True, "reason": "due", **win}


def missed_today(now_ts: float, *, last_sent_date: Optional[str]) -> bool:
    """True once today's window has closed without a recorded send."""
    win = window(now_ts)
    return now_ts >= win["end_ts"] and last_sent_date != win["card_date"]
