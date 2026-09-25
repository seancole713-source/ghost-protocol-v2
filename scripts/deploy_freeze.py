"""Market-hours deploy freeze (F29), run from Railway's pre-deploy step.

A deploy restarts the scheduler (morning card, paper orders, exits) and the
intraday monitors. Landing one mid-session drops or duplicates that work, so
the pre-deploy step refuses deploys on NYSE trading days between 08:00 and
16:30 America/New_York. A failed pre-deploy leaves the previous deployment
serving on Railway.

Override for an emergency fix: set DEPLOY_FREEZE_OVERRIDE=1 on the service and
redeploy (remove it afterwards).

The check must never block a deploy for any OTHER reason: any unexpected error
(missing tz data, calendar import failure, bad clock) allows the deploy and
prints a warning. Only a positive "inside the frozen window" answer blocks.

Offline: imports only the pure edge.calendar table (no app, DB or network).
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Callable, Optional

FREEZE_START = time(8, 0)
FREEZE_END = time(16, 30)
TZ_NAME = "America/New_York"
OVERRIDE_ENV = "DEPLOY_FREEZE_OVERRIDE"

ALLOW = 0
BLOCK = 1


def _override_set(env) -> bool:
    return str(env.get(OVERRIDE_ENV, "")).strip().lower() in ("1", "true", "yes", "on")


def _new_york_tz():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(TZ_NAME)
    except Exception:
        import pytz  # bundled tz database; requirements.txt pins it

        return pytz.timezone(TZ_NAME)


def _is_trading_day(day: date) -> bool:
    """NYSE session per the repo calendar (weekends + holidays + EDGE_HOLIDAYS)."""
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    from edge.calendar import session

    return bool(session(day)["trading"])


def in_freeze_window(
    now_utc: datetime,
    *,
    is_trading_day: Callable[[date], bool] = _is_trading_day,
    tz=None,
) -> bool:
    """True when ``now_utc`` is 08:00 <= t < 16:30 New York on a trading day."""
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    local = now_utc.astimezone(tz or _new_york_tz())
    if not (FREEZE_START <= local.time().replace(tzinfo=None) < FREEZE_END):
        return False
    return is_trading_day(local.date())


def check(
    now_utc: Optional[datetime] = None,
    env=None,
    *,
    is_trading_day: Callable[[date], bool] = _is_trading_day,
    out=None,
) -> int:
    """Return BLOCK (1) inside the freeze window without override, else ALLOW (0)."""
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    try:
        now = now_utc or datetime.now(timezone.utc)
        if not in_freeze_window(now, is_trading_day=is_trading_day):
            print("[DEPLOY_FREEZE] outside market-hours freeze window; deploy allowed", file=out)
            return ALLOW
        local = now.astimezone(_new_york_tz()).strftime("%Y-%m-%d %H:%M %Z")
        if _override_set(env):
            print(
                f"[DEPLOY_FREEZE] {local} is inside the market-hours freeze "
                f"(trading day 08:00-16:30 {TZ_NAME}) but {OVERRIDE_ENV}=1 is set; "
                "deploy allowed. Remove the override after this deploy.",
                file=out,
            )
            return ALLOW
        print(
            f"[DEPLOY_FREEZE] BLOCKED: {local} is inside the market-hours deploy freeze "
            f"(NYSE trading day, 08:00-16:30 {TZ_NAME}). A deploy now would restart the "
            "scheduler mid-session (card, paper orders, exits). The previous deployment "
            f"keeps running. Deploy after 16:30 ET, or set {OVERRIDE_ENV}=1 for an "
            "emergency fix.",
            file=out,
        )
        return BLOCK
    except Exception as exc:  # the freeze may never break a deploy by itself
        print(
            f"[DEPLOY_FREEZE] WARNING: freeze check errored ({type(exc).__name__}: "
            f"{str(exc)[:160]}); allowing deploy",
            file=out,
        )
        return ALLOW


def main() -> int:
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
