"""F31 (audit 2026-09-25): the core morning card runs at a fixed ET wall-clock
time, not 24 h after boot, and its self-heal runs whenever a process becomes
leader (idempotent via the persisted send record)."""
from __future__ import annotations

import inspect
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from core import morning_card_schedule as mcs

CT = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")


def _ts(hh, mm, tz=CT, day=(2026, 9, 25)):
    return datetime(*day, hh, mm, tzinfo=tz).timestamp()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("MORNING_CARD_ET", "TELEGRAM_DAILY_HOUR", "MORNING_CARD_WINDOW_MIN",
                 "MORNING_CARD_RETRY_MIN"):
        monkeypatch.delenv(name, raising=False)


def test_default_window_is_0900_et_for_four_hours():
    win = mcs.window(_ts(7, 0))
    assert win["start_ts"] == _ts(9, 0, ET) == _ts(8, 0, CT)
    assert win["end_ts"] == _ts(13, 0, ET)
    assert win["card_date"] == "2026-09-25"


def test_window_is_wall_clock_across_dst(monkeypatch):
    # Same 09:00 ET start in EST (January) and EDT (July).
    for day in ((2026, 1, 15), (2026, 7, 15)):
        win = mcs.window(_ts(6, 0, ET, day))
        start = datetime.fromtimestamp(win["start_ts"], ET)
        assert (start.hour, start.minute) == (9, 0)


def test_legacy_ct_hour_and_explicit_et_override(monkeypatch):
    monkeypatch.setenv("TELEGRAM_DAILY_HOUR", "7")
    assert mcs.window(_ts(5, 0))["start_ts"] == _ts(7, 0, CT)
    monkeypatch.setenv("MORNING_CARD_ET", "09:15")
    assert mcs.window(_ts(5, 0))["start_ts"] == _ts(9, 15, ET)


def test_due_rules():
    assert mcs.due(_ts(7, 59), last_sent_date=None, last_attempt_ts=None)["reason"] == "before_window"
    assert mcs.due(_ts(8, 0), last_sent_date=None, last_attempt_ts=None)["due"] is True
    assert mcs.due(_ts(8, 5), last_sent_date="2026-09-25", last_attempt_ts=None)["reason"] == "already_sent"
    assert mcs.due(_ts(8, 5), last_sent_date="2026-09-24", last_attempt_ts=_ts(8, 0))["reason"] == "retry_backoff"
    assert mcs.due(_ts(8, 31), last_sent_date="2026-09-24", last_attempt_ts=_ts(8, 0))["due"] is True
    # An attempt from YESTERDAY's window never blocks today.
    assert mcs.due(_ts(8, 0), last_sent_date="2026-09-23",
                   last_attempt_ts=_ts(11, 50, day=(2026, 9, 24)))["due"] is True
    assert mcs.due(_ts(12, 0), last_sent_date=None, last_attempt_ts=None)["reason"] == "after_window"
    assert mcs.missed_today(_ts(12, 0), last_sent_date="2026-09-24") is True
    assert mcs.missed_today(_ts(12, 0), last_sent_date="2026-09-25") is False
    assert mcs.missed_today(_ts(9, 0), last_sent_date="2026-09-24") is False


class _GhostState:
    """In-memory ghost_state shared by every simulated process (the DB)."""

    def __init__(self):
        self.rows = {}

    def conn(self):
        state = self

        class _Cur:
            def execute(self, sql, params=None):
                flat = " ".join(sql.split())
                if flat.startswith("SELECT key, val FROM ghost_state"):
                    self._out = [(k, v) for k, v in state.rows.items() if k.startswith("last_morning_card")]
                elif flat.startswith("INSERT INTO ghost_state"):
                    key = flat.split("VALUES('", 1)[1].split("'", 1)[0]
                    state.rows[key] = params[0]
                else:
                    raise AssertionError(flat)

            def fetchall(self):
                return self._out

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def cursor(self):
                return _Cur()

        return _Conn()


def _simulate(monkeypatch, *, boots, send_ok=True, start=(6, 0), end=(12, 0)):
    """Tick every 5 min from `start` to `end` CT; each boot is a new leader
    process whose first tick fires 30 s after it starts."""
    import wolf_app

    db = _GhostState()
    clock = {"now": 0.0}
    sends = []
    attempts = []

    def fake_job():
        attempts.append(clock["now"])
        if send_ok:
            sends.append(clock["now"])
            db.rows["last_morning_card_ts"] = str(int(clock["now"]))
            db.rows["last_morning_card_date"] = datetime.fromtimestamp(clock["now"], CT).strftime("%Y-%m-%d")
        return []

    monkeypatch.setattr(wolf_app, "db_conn", db.conn)
    monkeypatch.setattr(wolf_app, "_morning_card_job", fake_job)
    stop = _ts(*end)
    boot_ts = sorted(_ts(*b) for b in boots)
    ticks = []
    for i, boot in enumerate(boot_ts):
        # Each new leader process restarts the tick schedule (initial_delay 30 s)
        # and runs until the next redeploy replaces it.
        until = boot_ts[i + 1] if i + 1 < len(boot_ts) else stop + 1
        tick = boot + 30
        while tick < until and tick <= stop:
            ticks.append(tick)
            tick += 300
    for tick in ticks:
        clock["now"] = tick
        wolf_app._morning_card_tick(now_ts=tick)
    return sends, attempts, db


def test_boot_0600_redeploy_0730_sends_exactly_one_card_between_0800_and_0900_ct(monkeypatch):
    """Audit acceptance for F31."""
    sends, _attempts, db = _simulate(monkeypatch, boots=[(6, 0), (7, 30)])
    assert len(sends) == 1
    assert _ts(8, 0) <= sends[0] < _ts(9, 0)
    assert db.rows["last_morning_card_date"] == "2026-09-25"


def test_leader_starting_inside_window_self_heals_once(monkeypatch):
    # Rolling deploy: the new leader takes over at 10:12 CT, no card sent yet.
    sends, _a, _db = _simulate(monkeypatch, boots=[(10, 12)], start=(10, 12))
    assert len(sends) == 1
    assert sends[0] == _ts(10, 12) + 30


def test_repeated_leader_starts_after_send_do_not_resend(monkeypatch):
    sends, _a, _db = _simulate(monkeypatch, boots=[(6, 0), (8, 20), (9, 45), (11, 0)])
    assert len(sends) == 1


def test_failed_send_retries_spaced_and_bounded(monkeypatch):
    sends, attempts, _db = _simulate(monkeypatch, boots=[(6, 0)], send_ok=False)
    assert sends == []
    assert attempts, "the card must be attempted in the window"
    gaps = [b - a for a, b in zip(attempts, attempts[1:])]
    assert all(g >= 30 * 60 for g in gaps)
    assert all(_ts(8, 0) <= a < _ts(12, 0) for a in attempts)
    assert len(attempts) <= 8


def test_leader_runtime_registers_wall_clock_tick_not_boot_interval():
    import wolf_app

    src = inspect.getsource(wolf_app.lifespan)
    assert 'scheduler.register("morning_card", _morning_card_job, interval_s=86400' not in src
    assert '"morning_card", _morning_card_tick, interval_s=300' in src
    leader_start = src.index("async def _start_leader_runtime()")
    assert src.index('"morning_card", _morning_card_tick') > leader_start
    # The boot-only (leader-at-boot) recovery path is gone.
    assert "Startup recovery" not in src


def test_health_flags_missed_card_from_persisted_send_record(monkeypatch):
    import core.prices
    import core.scheduler
    import wolf_app

    monkeypatch.setattr(core.prices, "check_feeds", lambda: {})
    monkeypatch.setattr(core.scheduler, "status", lambda: [])
    monkeypatch.setattr(
        wolf_app, "_morning_card_state",
        lambda: {"last_sent_date": "2020-01-01", "last_sent_ts": 1_577_880_000.0,
                 "last_attempt_ts": None},
    )
    monkeypatch.setattr(mcs, "missed_today", lambda now, last_sent_date: True)
    out = wolf_app.health()
    assert out["last_morning_card_min"] > 1440
    assert any("Morning card not sent today" in issue for issue in out["issues"])
