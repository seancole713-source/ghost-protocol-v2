"""U66: the news-defense "withdraw" switch must never void a pick's result after entry.

It annotates the pick (scores.news_defense_flag + reason) and the resolver still counts it.
"""
from __future__ import annotations

import json
from contextlib import contextmanager

import core.db as db
import core.news_events as NE
from core import news_defense as ND


class _Cur:
    def __init__(self, picks):
        self.picks, self.sql = picks, []
        self._r = []

    def execute(self, sql, params=()):
        self.sql.append((" ".join(sql.split()), params))
        if "FROM predictions" in sql and sql.lstrip().upper().startswith("SELECT"):
            self._r = self.picks

    def fetchall(self):
        return self._r


def _run(monkeypatch, mode):
    now_pick = [(7, "SPCE", "UP", 1000)]
    cur = _Cur(now_pick)

    class _Conn:
        def cursor(self):
            return cur

        def commit(self):
            pass

    @contextmanager
    def fake_conn():
        yield _Conn()

    monkeypatch.setenv("NEWS_DEFENSE_ENABLED", "1")
    monkeypatch.setenv("NEWS_DEFENSE_MODE", mode)
    monkeypatch.setattr(db, "db_conn", fake_conn)
    monkeypatch.setattr(db, "ensure_ghost_state", lambda _c: None)
    monkeypatch.setattr(ND.time, "time", lambda: 3000)
    monkeypatch.setattr(NE, "recent_events_for_symbol", lambda *a, **k: [
        {"event_type": "dilution_or_offering", "direction_hint": "bearish", "materiality": 0.9, "asof_ts": 2000}])
    out = ND.run_defense_check()
    return out, cur.sql


def test_withdraw_mode_annotates_and_never_touches_the_outcome(monkeypatch):
    out, sql = _run(monkeypatch, "withdraw")
    assert out["ok"] and len(out["actions"]) == 1
    updates = [(s, p) for s, p in sql if s.upper().startswith("UPDATE PREDICTIONS")]
    assert updates, "the threat is recorded on the pick"
    for s, p in updates:
        assert "outcome" not in s.split("WHERE")[0].lower()          # nothing SETs outcome
        assert "WITHDRAWN" not in s and "exit_price" not in s and "pnl_pct" not in s
    s, p = updates[0]
    note = json.loads(p[0])
    assert note["news_defense_flag"] is True and "dilution_or_offering" in note["news_defense_reason"]
    assert p[1] == 7


def test_warn_mode_writes_nothing_to_the_pick(monkeypatch):
    out, sql = _run(monkeypatch, "warn")
    assert out["ok"] and len(out["actions"]) == 1
    assert not [s for s, _ in sql if s.upper().startswith("UPDATE PREDICTIONS")]


def test_unset_env_defaults_off(monkeypatch):
    monkeypatch.delenv("NEWS_DEFENSE_ENABLED", raising=False)
    monkeypatch.delenv("NEWS_DEFENSE_MODE", raising=False)
    assert ND.defense_enabled() is False and ND.defense_mode() == "warn"
    assert ND.run_defense_check()["skipped"]
