"""Exercise admission races against a disposable PostgreSQL instance, never production."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
from uuid import uuid4

import psycopg2
import pytest

from edge import research_budget as budget
from edge.store_pg import PostgresStore

pytestmark = pytest.mark.integration


@pytest.fixture
def store():
    if (
        not os.getenv("TEST_DATABASE_URL")
        or os.getenv("GHOST_INTEGRATION_TESTS") != "1"
    ):
        pytest.skip("requires explicit disposable integration database")

    @contextmanager
    def connect():
        conn = psycopg2.connect(os.environ["TEST_DATABASE_URL"])
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    result = PostgresStore(connect)
    result.ensure()
    return result


def test_concurrent_budget_admission_and_charges(store, monkeypatch):
    monkeypatch.setenv("EDGE_RESEARCH_TASK_RESERVE_USD", "1.5")
    day = "integration-" + uuid4().hex
    with ThreadPoolExecutor(max_workers=8) as pool:
        admissions = list(pool.map(lambda _: budget.reserve(store, day, 4), range(20)))
    assert sum(token is not None for token, _ in admissions) == 2
    assert sum(store.get(budget.TABLE, day)["reservations"].values()) == 3
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: budget.charge(store, day, 0.1), range(40)))
    assert store.get(budget.TABLE, day)["spent_usd"] == 4
    for token, _ in admissions:
        if token:
            budget.release(store, day, token)
    assert store.get(budget.TABLE, day)["reservations"] == {}
    assert budget.reserve(store, day, 4) == (None, 0)


def test_failed_mutation_rolls_back_without_losing_existing_budget(store):
    day = "integration-" + uuid4().hex
    budget.charge(store, day, 1)

    def fail(row):
        row["spent_usd"] = 9000
        raise ValueError("injected failure")

    with pytest.raises(ValueError, match="injected failure"):
        store.mutate(budget.TABLE, day, fail)
    assert store.get(budget.TABLE, day)["spent_usd"] == 1
    budget.charge(store, day, 1)
    assert store.get(budget.TABLE, day)["spent_usd"] == 2
