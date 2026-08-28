from urllib.parse import urlsplit

import pytest

from scripts.ci_test_database import (
    _CI_CLUSTER_MARKER,
    _verify_ci_cluster,
    database_url_for,
    validate_database_name,
)


def test_validate_database_name_accepts_ci_prefix():
    assert validate_database_name("ghost_ci_123_2") == "ghost_ci_123_2"


@pytest.mark.parametrize(
    "name",
    [
        "production",
        "ghost_ci_bad-name",
        "ghost_ci_;drop_database",
        "ghost_ci_",
        "ghost_ci_" + ("a" * 55),
    ],
)
def test_validate_database_name_rejects_unsafe_names(name):
    with pytest.raises(ValueError):
        validate_database_name(name)


def test_database_url_for_replaces_only_database_path():
    admin = "postgresql://ci-user:secret@db.example:5432/railway?sslmode=require"
    derived = database_url_for(admin, "ghost_ci_42_1")
    parsed = urlsplit(derived)

    assert parsed.path == "/ghost_ci_42_1"
    assert parsed.query == "sslmode=require"
    assert parsed.username == "ci-user"
    assert parsed.password == "secret"
    assert parsed.hostname == "db.example"


@pytest.mark.parametrize("url", ["", "https://example.com/db", "postgresql:/db"])
def test_database_url_for_rejects_non_postgres_urls(url):
    with pytest.raises(ValueError):
        database_url_for(url, "ghost_ci_42_1")


class _GuardCursor:
    def __init__(self, row):
        self.row = row
        self.params = None

    def execute(self, _query, params):
        self.params = params

    def fetchone(self):
        return self.row


def test_ci_cluster_guard_accepts_exact_marker():
    cursor = _GuardCursor((_CI_CLUSTER_MARKER,))

    _verify_ci_cluster(cursor)

    assert cursor.params == (_CI_CLUSTER_MARKER,)


@pytest.mark.parametrize("row", [None, ("production",)])
def test_ci_cluster_guard_fails_closed(row):
    with pytest.raises(RuntimeError, match="dedicated CI cluster"):
        _verify_ci_cluster(_GuardCursor(row))
