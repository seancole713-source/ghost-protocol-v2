"""F06: a Contract 70 registration is a frozen experiment, written once per version.

Re-registering used to upsert the same ghost_state key, replacing the universe
and resetting ``registered_at_ts`` (and so the forward window). These tests
use a stateful fake cursor that emulates ghost_state + the append-only history
table, including ON CONFLICT / compare-and-swap rowcounts.
"""
import json

import pytest

import core.contract_70_registry as reg
import core.db as db


class FakeDB:
    def __init__(self):
        self.state = {}
        self.history = []
        self.locks = 0


class FakeCursor:
    def __init__(self, store: FakeDB, after_read=None):
        self.store = store
        self.rowcount = -1
        self._one = None
        self._all = []
        self.after_read = after_read  # interleave another writer after a read
        self.sql = []

    def execute(self, sql, params=None):
        q = " ".join(sql.split())
        self.sql.append(q)
        self.rowcount = -1
        self._one, self._all = None, []
        st = self.store
        if q.startswith("CREATE TABLE"):
            return
        if "pg_advisory_xact_lock" in q:
            st.locks += 1
            return
        if q.startswith("SELECT val FROM ghost_state"):
            val = st.state.get(params[0])
            self._one = (val,) if val is not None else None
            if self.after_read is not None:
                hook, self.after_read = self.after_read, None
                hook()
            return
        if q.startswith("INSERT INTO ghost_state"):
            assert "DO NOTHING" in q and "DO UPDATE" not in q
            if params[0] in st.state:
                self.rowcount = 0
            else:
                st.state[params[0]] = params[1]
                self.rowcount = 1
            return
        if q.startswith("UPDATE ghost_state"):
            new, key, old = params
            if st.state.get(key) == old:
                st.state[key] = new
                self.rowcount = 1
            else:
                self.rowcount = 0
            return
        if q.startswith("SELECT 1 FROM ghost_contract_70_registry_history"):
            hit = any(h["version_id"] == params[0] and h["event"] in ("registered", "superseded")
                      for h in st.history)
            self._one = (1,) if hit else None
            return
        if q.startswith("INSERT INTO ghost_contract_70_registry_history"):
            event, version_id, record, ts = params
            st.history.append({"id": len(st.history) + 1, "event": event,
                               "version_id": version_id, "record": record, "created_at_ts": ts})
            self.rowcount = 1
            return
        if q.startswith("SELECT id, event, version_id, record, created_at_ts"):
            rows = sorted(st.history, key=lambda h: -h["id"])[: params[0]]
            self._all = [(h["id"], h["event"], h["version_id"], h["record"], h["created_at_ts"])
                         for h in rows]
            return
        if "ghost_state" in q and q.startswith("SELECT"):
            val = st.state.get(params[0])
            self._one = (val,) if val is not None else None
            return
        raise AssertionError(f"unexpected SQL: {q}")

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


@pytest.fixture
def store(monkeypatch):
    s = FakeDB()
    monkeypatch.setattr(db, "ensure_ghost_state", lambda c=None: None)
    clock = {"t": 1000}
    monkeypatch.setattr(reg, "_server_now", lambda: clock["t"])
    s.clock = clock
    return s


def _current(store):
    return json.loads(store.state[reg._REGISTRY_KEY])


def test_first_registration_is_server_timestamped_and_logged(store):
    out = reg.register_universe(["good"], min_n=8, min_wilson_low=0.7, cur=FakeCursor(store))
    assert out["registration_status"] == "registered"
    assert out["registered_at_ts"] == 1000
    assert out["version_id"] == "v1"
    assert "registration_status" not in _current(store)
    assert [h["event"] for h in store.history] == ["registered"]
    assert store.locks == 1
    with pytest.raises(TypeError):
        reg.register_universe(["GOOD"], min_n=8, min_wilson_low=0.7, now_ts=1, cur=FakeCursor(store))


def test_identical_retry_returns_existing_record(store):
    first = reg.register_universe(["GOOD", "BILL"], min_n=8, min_wilson_low=0.7, cur=FakeCursor(store))
    store.clock["t"] = 9000
    again = reg.register_universe(["bill", "good"], min_n=8, min_wilson_low=0.70, cur=FakeCursor(store))
    assert again["registration_status"] == "already_registered"
    assert again["registered_at_ts"] == first["registered_at_ts"] == 1000
    assert _current(store)["registered_at_ts"] == 1000
    assert [h["event"] for h in store.history] == ["registered", "idempotent_retry"]


def test_identical_slices_with_refreshed_evidence_are_idempotent(store):
    spec = {"dims": ["symbol"], "key": {"symbol": "BILL"}, "n": 30, "wins": 27, "wilson_low": 0.74}
    reg.register_slices([spec], min_n=8, min_wilson_low=0.7, cur=FakeCursor(store))
    store.clock["t"] = 5000
    later = dict(spec, n=40, wins=35, wilson_low=0.73)
    out = reg.register_slices([later], min_n=8, min_wilson_low=0.7, cur=FakeCursor(store))
    assert out["registration_status"] == "already_registered"
    assert out["registered_at_ts"] == 1000
    assert out["slices"][0]["selection_evidence"]["n"] == 30  # original provenance kept


@pytest.mark.parametrize("change", [
    {"symbols": ["GOOD", "OTHER"]},
    {"min_wilson_low": 0.75},
    {"min_n": 12},
])
def test_changed_universe_or_threshold_is_refused(store, change):
    reg.register_universe(["GOOD"], min_n=8, min_wilson_low=0.7, cur=FakeCursor(store))
    before = store.state[reg._REGISTRY_KEY]
    kwargs = {"min_n": 8, "min_wilson_low": 0.7}
    kwargs.update({k: v for k, v in change.items() if k != "symbols"})
    with pytest.raises(reg.Contract70RegistrationRefused) as exc:
        reg.register_universe(change.get("symbols", ["GOOD"]), cur=FakeCursor(store), **kwargs)
    assert exc.value.details["existing_version_id"] == "v1"
    assert exc.value.details["existing_registered_at_ts"] == 1000
    assert store.state[reg._REGISTRY_KEY] == before  # untouched
    assert store.history[-1]["event"] == "refused_changed_definition"


def test_changed_slices_refused(store):
    reg.register_slices([{"dims": ["symbol"], "key": {"symbol": "BILL"}}],
                        min_n=8, min_wilson_low=0.7, cur=FakeCursor(store))
    with pytest.raises(reg.Contract70RegistrationRefused):
        reg.register_slices([{"dims": ["symbol"], "key": {"symbol": "YMM"}}],
                            min_n=8, min_wilson_low=0.7, cur=FakeCursor(store))
    assert _current(store)["slices"][0]["key"] == {"symbol": "BILL"}


def test_new_version_preserves_history(store):
    reg.register_universe(["GOOD"], min_n=8, min_wilson_low=0.7, cur=FakeCursor(store))
    store.clock["t"] = 7000
    out = reg.register_universe(["GOOD", "BILL"], min_n=8, min_wilson_low=0.7,
                                version_id="v2", cur=FakeCursor(store))
    assert out["registration_status"] == "registered_new_version"
    assert out["registered_at_ts"] == 7000
    assert out["supersedes"] == {"version_id": "v1", "registered_at_ts": 1000}
    events = [(h["event"], h["version_id"]) for h in store.history]
    assert events == [("registered", "v1"), ("superseded", "v1"), ("registered", "v2")]
    superseded = json.loads(store.history[1]["record"])
    assert superseded["symbols"] == ["GOOD"] and superseded["registered_at_ts"] == 1000
    assert superseded["superseded_by"] == "v2"
    hist = reg.registry_history(cur=FakeCursor(store))
    assert [h["event"] for h in hist] == ["registered", "superseded", "registered"]

    # A used version id can never be reused, even to go back to v1's universe.
    for vid in ("v2", "v1"):
        with pytest.raises(reg.Contract70RegistrationRefused):
            reg.register_universe(["GOOD"], min_n=8, min_wilson_low=0.7,
                                  version_id=vid, cur=FakeCursor(store))
    assert _current(store)["version_id"] == "v2"


def test_legacy_unversioned_record_can_only_be_superseded_explicitly(store):
    store.state[reg._REGISTRY_KEY] = json.dumps({
        "registered_at_ts": 500, "symbols": ["GOOD"], "min_n": 8,
        "min_wilson_low": 0.7, "prob_floor": 0.7, "target": 0.7,
    })
    same = reg.register_universe(["GOOD"], min_n=8, min_wilson_low=0.7, cur=FakeCursor(store))
    assert same["registration_status"] == "already_registered"
    assert same["registered_at_ts"] == 500
    with pytest.raises(reg.Contract70RegistrationRefused) as exc:
        reg.register_universe(["BILL"], min_n=8, min_wilson_low=0.7, cur=FakeCursor(store))
    assert exc.value.details["existing_version_id"] == "legacy_unversioned"
    out = reg.register_universe(["BILL"], min_n=8, min_wilson_low=0.7,
                                version_id="v2", cur=FakeCursor(store))
    assert out["supersedes"]["registered_at_ts"] == 500


def test_bad_version_id_rejected(store):
    with pytest.raises(ValueError):
        reg.register_universe(["GOOD"], min_n=8, min_wilson_low=0.7,
                              version_id="v2; DROP", cur=FakeCursor(store))


def test_repeat_registration_after_losses_cannot_reset_window(store):
    reg.register_universe(["GOOD", "BAD"], min_n=8, min_wilson_low=0.7, cur=FakeCursor(store))
    rows = [
        {"symbol": "BAD", "up_prob": 0.8, "eval_ts": 1100 + i, "trade_date": f"2026-09-{i + 1:02d}",
         "outcome": "LOSS"} for i in range(5)
    ] + [
        {"symbol": "GOOD", "up_prob": 0.8, "eval_ts": 1200 + i, "trade_date": f"2026-09-{i + 1:02d}",
         "outcome": "WIN"} for i in range(5)
    ]
    store.clock["t"] = 2000  # after the losses resolved
    same = reg.register_universe(["GOOD", "BAD"], min_n=8, min_wilson_low=0.7, cur=FakeCursor(store))
    assert same["registered_at_ts"] == 1000
    with pytest.raises(reg.Contract70RegistrationRefused):
        reg.register_universe(["GOOD"], min_n=8, min_wilson_low=0.7, cur=FakeCursor(store))
    live = reg.load_registry(cur=FakeCursor(store))
    out = reg.evaluate_forward(rows, registered_symbols=live["symbols"],
                               registered_at_ts=live["registered_at_ts"])
    assert out["n"] == 10 and out["wins"] == 5  # the losses still count
    assert [h["event"] for h in store.history] == [
        "registered", "idempotent_retry", "refused_changed_definition"]


def test_concurrent_first_registrations_one_wins_other_refused(store):
    """Both read 'no record'; the second writer's INSERT loses and re-compares."""
    def _other_writer():
        reg.register_universe(["BILL"], min_n=8, min_wilson_low=0.7, cur=FakeCursor(store))

    with pytest.raises(reg.Contract70RegistrationRefused):
        reg.register_universe(["GOOD"], min_n=8, min_wilson_low=0.7,
                              cur=FakeCursor(store, after_read=_other_writer))
    assert _current(store)["symbols"] == ["BILL"]
    assert [h["event"] for h in store.history] == ["registered", "refused_changed_definition"]


def test_concurrent_identical_registrations_converge_on_one_record(store):
    def _other_writer():
        store.clock["t"] = 1001
        reg.register_universe(["GOOD"], min_n=8, min_wilson_low=0.7, cur=FakeCursor(store))

    out = reg.register_universe(["GOOD"], min_n=8, min_wilson_low=0.7,
                                cur=FakeCursor(store, after_read=_other_writer))
    assert out["registration_status"] == "already_registered"
    assert out["registered_at_ts"] == _current(store)["registered_at_ts"] == 1001
    assert [h["event"] for h in store.history].count("registered") == 1


def test_concurrent_supersede_is_compare_and_swap(store):
    reg.register_universe(["GOOD"], min_n=8, min_wilson_low=0.7, cur=FakeCursor(store))

    def _other_writer():
        reg.register_universe(["BILL"], min_n=8, min_wilson_low=0.7,
                              version_id="v2", cur=FakeCursor(store))

    # Our v3 read v1, but v2 lands before our UPDATE: the CAS misses, we
    # re-read v2 and supersede it (never silently overwrite an unseen record).
    out = reg.register_universe(["YMM"], min_n=8, min_wilson_low=0.7, version_id="v3",
                                cur=FakeCursor(store, after_read=_other_writer))
    assert out["supersedes"]["version_id"] == "v2"
    events = [(h["event"], h["version_id"]) for h in store.history]
    assert events == [("registered", "v1"), ("superseded", "v1"), ("registered", "v2"),
                      ("superseded", "v2"), ("registered", "v3")]


def test_register_route_returns_409_on_refusal(monkeypatch):
    from fastapi.testclient import TestClient
    from wolf_app import APP

    monkeypatch.setattr("wolf_app._cron_ok", lambda secret, strict=False: secret == "ok" and strict is True)
    monkeypatch.setattr("wolf_app._admin_token_valid", lambda tok: False)
    monkeypatch.setattr("core.contract_70_slices.contract_70_slice_search", lambda **kw: {
        "ok": True, "qualified": [{"dims": ["symbol"], "key": {"symbol": "YMM"}}],
    })
    seen = []

    def _refuse(slices, **kwargs):
        seen.append(kwargs)
        raise reg.Contract70RegistrationRefused(
            "registration differs from the frozen experiment",
            {"existing_version_id": "v1", "existing_registered_at_ts": 1000},
        )

    monkeypatch.setattr("core.contract_70_registry.register_slices", _refuse)
    r = TestClient(APP).post("/api/watcher/contract-70/register", headers={"x-cron-secret": "ok"})
    assert r.status_code == 409
    assert r.json()["detail"]["existing_version_id"] == "v1"
    assert "version_id" not in seen[0]

    r = TestClient(APP).post("/api/watcher/contract-70/register?version_id=v2",
                             headers={"x-cron-secret": "ok"})
    assert r.status_code == 409
    assert seen[1]["version_id"] == "v2"


def test_register_route_reports_idempotent_retry(monkeypatch):
    from fastapi.testclient import TestClient
    from wolf_app import APP

    monkeypatch.setattr("wolf_app._cron_ok", lambda secret, strict=False: secret == "ok" and strict is True)
    monkeypatch.setattr("wolf_app._admin_token_valid", lambda tok: False)
    monkeypatch.setattr("core.contract_70_slices.contract_70_slice_search", lambda **kw: {
        "ok": True, "qualified": [{"dims": ["symbol"], "key": {"symbol": "YMM"}}],
    })
    monkeypatch.setattr("core.contract_70_registry.register_slices", lambda slices, **kw: {
        "mode": "slices", "registered_at_ts": 1000, "registration_status": "already_registered",
    })
    r = TestClient(APP).post("/api/watcher/contract-70/register", headers={"x-cron-secret": "ok"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "already_registered"
    assert body["registry"]["registered_at_ts"] == 1000
    assert "registration_status" not in body["registry"]
