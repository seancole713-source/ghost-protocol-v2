"""Boot must not write the predictions ledger before leader election.

_expire_open_picks_without_v3_model() voids active picks whose symbol has no
v3 model. It used to run in the lifespan before any leader check, so a booting
follower (rolling deploy) wrote the ledger too. It now runs only inside
_start_leader_runtime, and it is idempotent: only still-open rows are voided.
"""
from __future__ import annotations

import inspect
import re

import wolf_app


class _Cur:
    def __init__(self, state):
        self.state = state
        self.rowcount = 0
        self._rows = []
        self.updates = []

    def execute(self, sql, params=None):
        if sql.startswith("SELECT key FROM ghost_v3_model"):
            self._rows = [("meta_WOLF_up",), ("meta_AMC",)]
        elif sql.startswith("SELECT id, symbol FROM predictions"):
            # Snapshot taken before a concurrent resolver closed row 2.
            self._rows = [(1, "WOLF"), (2, "GME"), (3, "TSLA")]
        elif sql.startswith("UPDATE predictions"):
            assert "outcome IS NULL" in sql, "expiry must only touch open rows"
            assert "outcome='ADMIN_VOID'" in sql, "U55: administrative closure, not EXPIRED"
            pid = params[1]
            self.updates.append(pid)
            if self.state.get(pid) is None:
                self.state[pid] = "ADMIN_VOID"
                self.rowcount = 1
            else:
                self.rowcount = 0
        else:  # pragma: no cover - unexpected SQL
            raise AssertionError(sql)

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self._cur


def test_orphan_expiry_is_idempotent_and_never_overwrites_resolved(monkeypatch):
    state = {1: None, 2: "WIN", 3: None}  # row 2 resolved concurrently
    cur = _Cur(state)
    monkeypatch.setattr(wolf_app, "db_conn", lambda: _Conn(cur))

    assert wolf_app._expire_open_picks_without_v3_model() == 1  # only TSLA
    assert state == {1: None, 2: "WIN", 3: "ADMIN_VOID"}
    # Second run (restart / leader handoff): nothing new is voided.
    assert wolf_app._expire_open_picks_without_v3_model() == 0
    assert state == {1: None, 2: "WIN", 3: "ADMIN_VOID"}


def test_orphan_expiry_runs_only_in_leader_runtime():
    src = inspect.getsource(wolf_app.lifespan)
    call = "_expire_open_picks_without_v3_model()"
    assert src.count(call) == 1
    leader_start = src.index("async def _start_leader_runtime()")
    election = src.index("_is_leader = try_acquire_leader()")
    pos = src.index(call)
    assert pos > leader_start > election
    # ...and inside the leader runtime body, not after it.
    body = src[leader_start:]
    next_def = re.search(r"\n    (?:async )?def |\n    _takeover_task = None", body)
    assert next_def is not None and body.index(call) < next_def.start()
