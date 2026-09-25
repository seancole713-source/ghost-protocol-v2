"""tests/test_explosion_benchmark.py — preregistered explosion-event detection."""
from __future__ import annotations

import core.explosion_benchmark as eb


def _bars(closes):
    return [{"ts": 1000 + i * 86400, "close": c} for i, c in enumerate(closes)]


def test_detect_20pct_1d_event():
    events = eb.detect_explosion_events(_bars([10.0, 12.5]), symbol="ARCT")
    tiers = {e["tier"] for e in events}
    assert "+20%_1d" in tiers


def test_detect_100pct_20d_event():
    # +100% over 20 days.
    closes = [10.0] + [10.0 + i * 0.6 for i in range(1, 21)]  # ends at 22.0
    events = eb.detect_explosion_events(_bars(closes), symbol="ARCT")
    tiers = {e["tier"] for e in events}
    assert "+100%_20d" in tiers


def test_no_event_on_flat_series():
    events = eb.detect_explosion_events(_bars([10.0, 10.1, 10.2]), symbol="ARCT")
    assert events == []


def test_event_carries_start_and_peak():
    events = eb.detect_explosion_events(_bars([10.0, 12.5]), symbol="ARCT")
    e = next(e for e in events if e["tier"] == "+20%_1d")
    assert e["start_price"] == 10.0
    assert e["peak_price"] == 12.5
    assert e["move_pct"] == 25.0


def test_benchmark_summary_empty_db(monkeypatch):
    class _Cur:
        def execute(self, sql, params=None):
            self.sql = sql

        def fetchall(self):
            return []

    out = eb.benchmark_summary(cur=_Cur())
    assert out["ok"] is True
    assert out["total_events"] == 0
    assert out["overall_recall_pct"] == 0.0


class _SqliteCur:
    """psycopg-style (%s) cursor over sqlite3, enough for the event upsert."""

    def __init__(self, conn):
        self._cur = conn.cursor()

    def execute(self, sql, params=()):
        self._cur.execute(sql.replace("%s", "?"), tuple(params or ()))

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


def _sqlite_benchmark_db():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE ghost_explosion_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL, tier TEXT NOT NULL, window_days INT NOT NULL,
            move_pct FLOAT NOT NULL, start_price FLOAT NOT NULL, peak_price FLOAT NOT NULL,
            start_ts BIGINT NOT NULL, peak_ts BIGINT NOT NULL,
            first_observed_ts BIGINT, first_observed_price FLOAT,
            promoted_ts BIGINT, promoted_price FLOAT, max_move_captured_pct FLOAT,
            alerted_before_10pct BOOLEAN, alerted_before_20pct BOOLEAN,
            crossed_10pct_ts BIGINT, crossed_20pct_ts BIGINT, first_alert_ts BIGINT,
            benchmark_version TEXT NOT NULL, created_at BIGINT NOT NULL,
            UNIQUE (symbol, tier, start_ts)
        );
        CREATE TABLE ghost_explosion_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
            observed_at BIGINT NOT NULL, price FLOAT, kind TEXT NOT NULL,
            confidence_pct FLOAT, created_at BIGINT NOT NULL
        );
        """
    )
    return conn


def test_first_observed_ts_survives_observation_purge():
    """F39: re-detecting an event after its observations aged out must keep
    first_observed_ts / price / promotion / max captured, not NULL them."""
    conn = _sqlite_benchmark_db()
    cur = _SqliteCur(conn)
    start = 1_780_000_000
    event = {
        "symbol": "ARCT", "tier": "+20%_1d", "window_days": 1, "move_pct": 25.0,
        "start_price": 10.0, "peak_price": 12.5, "start_ts": start, "peak_ts": start + 3600,
        "crossed_10pct_ts": start + 600, "crossed_20pct_ts": start + 1800,
    }
    cur.execute(
        "INSERT INTO ghost_explosion_observations (symbol, observed_at, price, kind, created_at) "
        "VALUES (%s,%s,%s,%s,%s)", ("ARCT", start + 300, 10.4, "watch", start + 300),
    )
    cur.execute(
        "INSERT INTO ghost_explosion_observations (symbol, observed_at, price, kind, created_at) "
        "VALUES (%s,%s,%s,%s,%s)", ("ARCT", start + 900, 11.0, "candidate", start + 900),
    )
    event_id = eb.record_event_with_observations(event, cur=cur)
    cur.execute(
        "SELECT first_observed_ts, first_observed_price, promoted_ts, max_move_captured_pct "
        "FROM ghost_explosion_events WHERE id=%s", (event_id,),
    )
    assert cur.fetchone() == (start + 300, 10.4, start + 900, 20.19)

    # The 120-day purge removes the non-alert observations ...
    cur.execute("DELETE FROM ghost_explosion_observations WHERE kind <> 'alert'")
    # ... and a later benchmark run re-detects the same event (peak extended).
    again = dict(event, peak_price=13.0, move_pct=30.0)
    assert eb.record_event_with_observations(again, cur=cur) == event_id
    cur.execute(
        "SELECT first_observed_ts, first_observed_price, promoted_ts, promoted_price, "
        "max_move_captured_pct, peak_price FROM ghost_explosion_events WHERE id=%s", (event_id,),
    )
    first_ts, first_px, promoted_ts, promoted_px, captured, peak = cur.fetchone()
    assert (first_ts, first_px, promoted_ts, promoted_px) == (start + 300, 10.4, start + 900, 11.0)
    assert peak == 13.0
    assert captured == round((13.0 - 10.4) / 10.4 * 100.0, 2)


def test_surviving_later_alert_does_not_delay_first_observation():
    """F39: alert rows are never purged, so after the purge the 'first'
    observation a re-run finds is the later alert. The earliest recorded
    first_observed_ts must be kept, not replaced with that later time."""
    conn = _sqlite_benchmark_db()
    cur = _SqliteCur(conn)
    start = 1_780_000_000
    event = {
        "symbol": "ARCT", "tier": "+20%_1d", "window_days": 1, "move_pct": 25.0,
        "start_price": 10.0, "peak_price": 12.5, "start_ts": start, "peak_ts": start + 3600,
        "crossed_10pct_ts": start + 600, "crossed_20pct_ts": start + 1800,
    }
    for ts, px, kind in ((300, 10.4, "watch"), (400, 10.9, "alert")):
        cur.execute(
            "INSERT INTO ghost_explosion_observations (symbol, observed_at, price, kind, created_at) "
            "VALUES (%s,%s,%s,%s,%s)", ("ARCT", start + ts, px, kind, start + ts),
        )
    event_id = eb.record_event_with_observations(event, cur=cur)
    cur.execute("DELETE FROM ghost_explosion_observations WHERE kind <> 'alert'")
    eb.record_event_with_observations(event, cur=cur)
    cur.execute(
        "SELECT first_observed_ts, first_observed_price, first_alert_ts, alerted_before_10pct "
        "FROM ghost_explosion_events WHERE id=%s", (event_id,),
    )
    first_ts, first_px, alert_ts, before_10 = cur.fetchone()
    assert (first_ts, first_px, alert_ts) == (start + 300, 10.4, start + 400)
    assert bool(before_10) is True


def test_fresh_observation_still_updates_event():
    conn = _sqlite_benchmark_db()
    cur = _SqliteCur(conn)
    start = 1_780_000_000
    event = {
        "symbol": "ARCT", "tier": "+20%_1d", "window_days": 1, "move_pct": 25.0,
        "start_price": 10.0, "peak_price": 12.5, "start_ts": start, "peak_ts": start + 3600,
        "crossed_10pct_ts": None, "crossed_20pct_ts": None,
    }
    event_id = eb.record_event_with_observations(event, cur=cur)
    cur.execute("SELECT first_observed_ts FROM ghost_explosion_events WHERE id=%s", (event_id,))
    assert cur.fetchone() == (None,)
    cur.execute(
        "INSERT INTO ghost_explosion_observations (symbol, observed_at, price, kind, created_at) "
        "VALUES (%s,%s,%s,%s,%s)", ("ARCT", start + 120, 10.2, "watch", start + 120),
    )
    eb.record_event_with_observations(event, cur=cur)
    cur.execute(
        "SELECT first_observed_ts, first_observed_price FROM ghost_explosion_events WHERE id=%s",
        (event_id,),
    )
    assert cur.fetchone() == (start + 120, 10.2)
