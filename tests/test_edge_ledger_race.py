"""The frozen ledger under two runners at once (leader handoff, lease expiry, overlapping redeploy).

Both runners read "nothing there yet" before either writes. The second write must never
overwrite the first: first writer wins, and a different final outcome is refused.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from edge.contracts import GAP_AND_GO_V1, TERMINAL, ContractError, FrozenSpecError, issue
from edge.ledger import Ledger, MemoryStore
from edge.resolver import Resolution
from edge.store_pg import PostgresStore

ET = ZoneInfo("America/New_York")
DAY = date(2026, 9, 23)


def ts(hh, mm):
    return int(datetime(2026, 9, 23, hh, mm, tzinfo=ET).timestamp())


def fc(ref=9.40):
    return issue(GAP_AND_GO_V1, symbol="ABCD", session_date=DAY, entry_ref=ref, issued_at=ts(9, 10))


class StaleReadStore(MemoryStore):
    """A runner whose reads of `stale` tables were taken before the other runner committed."""

    def __init__(self, shared: MemoryStore, stale=()):
        super().__init__()
        self._t = shared._t          # same underlying rows as the other runner
        self.stale = set(stale)

    def get(self, table, key):
        if table in self.stale:
            return None
        return super().get(table, key)


def _two_runners(*stale):
    shared = MemoryStore()
    a = Ledger(shared)
    a.register(GAP_AND_GO_V1, now=ts(8, 0))
    b = Ledger(StaleReadStore(shared, stale))
    return shared, a, b


def test_a_late_runner_cannot_overwrite_a_final_outcome():
    shared, a, b = _two_runners("outcomes")
    f = fc()
    a.record(f, now=ts(9, 11))
    a.settle(f.forecast_id, Resolution("WIN", pnl_usd=49.35, exit_price=9.96), now=ts(16, 0))
    with pytest.raises(ContractError):
        b.settle(f.forecast_id, Resolution("LOSS", pnl_usd=-29.4, exit_price=9.21), now=ts(16, 1))
    assert shared.get("outcomes", f"{f.forecast_id}|simulated")["outcome"] == "WIN"


def test_the_same_final_outcome_from_a_second_runner_is_a_no_op():
    shared, a, b = _two_runners("outcomes")
    f = fc()
    a.record(f, now=ts(9, 11))
    first = a.settle(f.forecast_id, Resolution("WIN", pnl_usd=49.35, exit_price=9.96), now=ts(16, 0))
    again = b.settle(f.forecast_id, Resolution("WIN", pnl_usd=49.35, exit_price=9.96), now=ts(16, 5))
    assert again["settled_at"] == first["settled_at"] == ts(16, 0)


def test_unresolved_is_still_replaced_by_a_later_runner():
    shared, a, b = _two_runners("outcomes")
    f = fc()
    a.record(f, now=ts(9, 11))
    a.settle(f.forecast_id, Resolution("UNRESOLVED"), now=ts(16, 0))
    b.settle(f.forecast_id, Resolution("LOSS", pnl_usd=-29.4, exit_price=9.21), now=ts(17, 0))
    assert shared.get("outcomes", f"{f.forecast_id}|simulated")["outcome"] == "LOSS"


def test_a_late_runner_cannot_overwrite_a_recorded_forecast():
    shared, a, b = _two_runners("forecasts")
    f = fc()
    a.record(f, now=ts(9, 11))
    with pytest.raises(ContractError):
        b.record(fc(ref=9.60), now=ts(9, 12))
    assert shared.get("forecasts", f.forecast_id)["entry_ref"] == f.entry_ref
    assert shared.get("forecasts", f.forecast_id)["recorded_at"] == ts(9, 11)


def test_a_late_runner_cannot_overwrite_a_registered_spec():
    shared, a, b = _two_runners("experiments")
    with pytest.raises(FrozenSpecError):
        b.register(replace(GAP_AND_GO_V1, target_mult=1.04), now=ts(8, 1))
    assert shared.get("experiments", "gap_and_go@v1")["spec_hash"] == GAP_AND_GO_V1.spec_hash()


def test_the_first_exclusion_reason_stands():
    shared, a, b = _two_runners("exclusions")
    f = fc()
    a.record(f, now=ts(9, 11))
    a.exclude(f.forecast_id, reason="halted through the window", now=ts(12, 0))
    out = b.exclude(f.forecast_id, reason="second runner", now=ts(12, 1))
    assert out["reason"] == shared.get("exclusions", f.forecast_id)["reason"] == "halted through the window"


# -------------------------------------------------- the Postgres statements --

class _PgCursor:
    """Honors ON CONFLICT DO NOTHING and the conflict-update's WHERE on outcome, like Postgres."""

    def __init__(self, db):
        self.db, self._r = db, []

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("INSERT"):
            tbl, key, row, known, *rest = params
            cur = self.db.get((tbl, key))
            if cur is None:
                self.db[(tbl, key)] = (json.loads(row), known)
                self._r = [(key,)]
            elif "DO NOTHING" in s:
                self._r = []
            elif "ANY(%s::text[])" in s and (cur[0].get("outcome") or "") in rest[0]:
                self._r = []
            else:
                self.db[(tbl, key)] = (json.loads(row), known)
                self._r = [(key,)]
        elif s.startswith("SELECT row FROM edge_rows WHERE tbl=%s AND key=%s"):
            v = self.db.get(tuple(params))
            self._r = [(v[0],)] if v else []

    def fetchone(self):
        return self._r[0] if self._r else None


class _PgConn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return _PgCursor(self.db)

    def commit(self):
        pass


def test_postgres_writes_are_first_writer_wins():
    db = {}

    @contextmanager
    def connect():
        yield _PgConn(db)

    pg = PostgresStore(connect)
    assert pg.put_new("forecasts", "k", {"v": 1}) == {"v": 1}
    assert pg.put_new("forecasts", "k", {"v": 2}) == {"v": 1}
    assert db[("forecasts", "k")][0] == {"v": 1}

    assert pg.put_unless_final("outcomes", "o", {"outcome": "UNRESOLVED"}, final=TERMINAL)["outcome"] == "UNRESOLVED"
    assert pg.put_unless_final("outcomes", "o", {"outcome": "WIN"}, final=TERMINAL)["outcome"] == "WIN"
    assert pg.put_unless_final("outcomes", "o", {"outcome": "LOSS"}, final=TERMINAL)["outcome"] == "WIN"
    assert db[("outcomes", "o")][0] == {"outcome": "WIN"}

    # and through the ledger, with the second runner's reads stale
    lg = Ledger(pg)
    lg.register(GAP_AND_GO_V1, now=ts(8, 0))
    f = fc()
    lg.record(f, now=ts(9, 11))
    lg.settle(f.forecast_id, Resolution("WIN", pnl_usd=49.35, exit_price=9.96), now=ts(16, 0))
    late = PostgresStore(connect)
    late.get = lambda table, key: None if table == "outcomes" else PostgresStore.get(late, table, key)
    with pytest.raises(ContractError):
        Ledger(late).settle(f.forecast_id, Resolution("LOSS", pnl_usd=-29.4, exit_price=9.21), now=ts(16, 1))
    assert db[("outcomes", f"{f.forecast_id}|simulated")][0]["outcome"] == "WIN"
