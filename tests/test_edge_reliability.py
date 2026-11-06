from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from edge import operations, research_budget as B, research_openai as O
from edge.ledger import MemoryStore


def test_atomic_reservations_do_not_double_admit(monkeypatch):
    monkeypatch.setenv("EDGE_RESEARCH_TASK_RESERVE_USD", "1.5")
    store = MemoryStore()
    with ThreadPoolExecutor(max_workers=8) as pool:
        got = list(pool.map(lambda _: B.reserve(store, "day", 4), range(20)))
    assert sum(token is not None for token, _ in got) == 2
    assert sum(store.get(B.TABLE, "day")["reservations"].values()) == 3
    for token, _ in got:
        if token:
            B.release(store, "day", token)
    assert store.get(B.TABLE, "day")["reservations"] == {}


def test_atomic_charges_do_not_lose_updates():
    store = MemoryStore()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: B.charge(store, "day", 0.1), range(100)))
    assert store.get(B.TABLE, "day")["spent_usd"] == 10


def test_auth_failure_is_not_silent(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-not-for-output")
    monkeypatch.setenv("EDGE_OPENAI_MODEL", "gpt-4.1")
    diag = {}
    http = SimpleNamespace(post=lambda *a, **k: SimpleNamespace(status_code=401))
    assert O.review(http, symbol="ABC", claims=[], diagnostic=diag) == (None, 0)
    assert diag == {"status": "authentication_failed", "http_status": 401}
    assert "secret" not in str(diag)


def test_operational_auth_cache_rechecks_changed_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "a")
    monkeypatch.setenv("EDGE_RESEARCH_ENABLED", "1")
    calls = []

    def get(*a, **kw):
        calls.append(1)
        return SimpleNamespace(status_code=401)

    store = MemoryStore()
    http = SimpleNamespace(get=get)
    out = operations.check(http, store, now=1793367600)
    assert (
        out["status"] == "DEGRADED"
        and out["issues"][0]["code"] == "reviewer_auth_failed"
    )
    assert "credential_hash" not in str(out)
    operations.check(http, store, now=1793367610)
    assert len(calls) == 1
    monkeypatch.setenv("OPENAI_API_KEY", "b")
    operations.check(http, store, now=1793367620)
    assert len(calls) == 2


def test_future_quote_cannot_be_healthy():
    from edge.health import SourceHealth, STALE

    assert SourceHealth("quotes", 1000, 60).status(900) == STALE


def test_operations_surfaces_coverage_cost_and_correlated_fallback_without_writing(
    monkeypatch,
):
    from copy import deepcopy

    monkeypatch.setenv("EDGE_RESEARCH_ENABLED", "0")
    store = MemoryStore()
    day = "2026-10-30"
    store.put("edge_cards", day, {"candidates": 100, "priced": 14})
    store.put(B.TABLE, day, {"spent_usd": 5, "admission_cap_usd": 4})
    store.put(
        "edge_research",
        "r",
        {"day": day, "independent_review": {"status": "authentication_failed"}},
    )
    before = deepcopy(store._t)
    out = operations.report(store, now=1793367600)
    assert {r["code"] for r in out["issues"]} == {
        "low_candidate_price_coverage",
        "research_budget_overrun",
        "independent_review_fallback",
    }
    assert out["candidate_price_coverage"] == 0.14
    assert out["coverage_target_is_release_gate"] is False
    assert store._t == before


def test_admission_stops_before_soft_cap(monkeypatch):
    from edge.research_worker import research_symbol

    monkeypatch.setenv("EDGE_RESEARCH_DAILY_USD", "4")
    store = MemoryStore()
    B.charge(store, "day", 3.9)
    out = research_symbol(None, store, symbol="ABC", day="day", now=1793367600)
    assert out["status"] == "budget_exhausted"
    assert store.get(B.TABLE, "day")["spent_usd"] == 3.9


def test_unexpected_paid_task_failure_keeps_its_budget_reservation(monkeypatch):
    import pytest
    from edge import research_worker as worker

    def fail(*args, **kwargs):
        raise RuntimeError("charge persistence failed")

    monkeypatch.setattr(worker, "_research_symbol", fail)
    monkeypatch.setenv("EDGE_RESEARCH_TASK_RESERVE_USD", "1.5")
    monkeypatch.setenv("EDGE_RESEARCH_DAILY_USD", "1.5")
    store = MemoryStore()
    with pytest.raises(RuntimeError, match="charge persistence failed"):
        worker.research_symbol(None, store, symbol="ABC", day="day", now=1793367600)
    assert sum(store.get(B.TABLE, "day")["reservations"].values()) == 1.5
    assert (
        worker.research_symbol(None, store, symbol="XYZ", day="day", now=1793367700)[
            "status"
        ]
        == "budget_exhausted"
    )
