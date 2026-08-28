"""Create and remove an isolated PostgreSQL database for one CI run.

The configured secret is an administrative URL for a dedicated CI-only
PostgreSQL service. Tests receive a derived URL for a fresh database so state
from an earlier workflow can never affect the current run.
"""
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg2
from psycopg2 import sql


_CI_DATABASE_RE = re.compile(r"^ghost_ci_[a-z0-9_]{1,54}$")
_CI_CLUSTER_MARKER = "ghost-ci-dedicated-cluster-v1"


def validate_database_name(name: str) -> str:
    """Return a safe CI database name or fail before issuing any SQL."""
    normalized = str(name or "").strip().lower()
    if len(normalized) > 63 or not _CI_DATABASE_RE.fullmatch(normalized):
        raise ValueError(
            "CI database name must match ghost_ci_[a-z0-9_]+ and be <= 63 chars"
        )
    return normalized


def database_url_for(admin_url: str, database_name: str) -> str:
    """Replace only the database path while preserving credentials/options."""
    name = validate_database_name(database_name)
    parsed = urlsplit(admin_url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.netloc:
        raise ValueError("TEST_DATABASE_ADMIN_URL must be a PostgreSQL URL")
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{name}", parsed.query, ""))


def _admin_url() -> str:
    value = os.environ.get("TEST_DATABASE_ADMIN_URL", "").strip()
    if not value:
        raise RuntimeError("TEST_DATABASE_ADMIN_URL is required")
    return value


def _write_github_env(values: dict[str, str]) -> None:
    env_path = os.environ.get("GITHUB_ENV", "").strip()
    if not env_path:
        raise RuntimeError("GITHUB_ENV is required for create")
    with Path(env_path).open("a", encoding="utf-8") as output:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise ValueError(f"newline is not allowed in {key}")
            output.write(f"{key}={value}\n")


def _verify_ci_cluster(cursor) -> None:
    """Refuse to create databases unless the cluster was explicitly marked CI."""
    try:
        cursor.execute(
            "SELECT marker FROM ghost_ci_cluster_guard WHERE marker=%s",
            (_CI_CLUSTER_MARKER,),
        )
        row = cursor.fetchone()
    except psycopg2.Error as exc:
        raise RuntimeError("database is not marked as a dedicated CI cluster") from exc
    if not row or row[0] != _CI_CLUSTER_MARKER:
        raise RuntimeError("database is not marked as a dedicated CI cluster")


def create_database(database_name: str) -> str:
    name = validate_database_name(database_name)
    admin_url = _admin_url()
    connection = psycopg2.connect(admin_url)
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            _verify_ci_cluster(cursor)
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    finally:
        connection.close()
    test_url = database_url_for(admin_url, name)
    _write_github_env(
        {
            "CI_TEST_DATABASE_NAME": name,
            "TEST_DATABASE_URL": test_url,
        }
    )
    return name


def drop_database(database_name: str) -> str:
    name = validate_database_name(database_name)
    connection = psycopg2.connect(_admin_url())
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            _verify_ci_cluster(cursor)
            cursor.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
                """,
                (name,),
            )
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name))
            )
    finally:
        connection.close()
    return name


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manage an isolated PostgreSQL database for a CI run"
    )
    parser.add_argument("action", choices=("create", "drop"))
    parser.add_argument("--name", required=True)
    args = parser.parse_args()

    if args.action == "create":
        name = create_database(args.name)
        print(f"Created isolated CI database: {name}")
    else:
        name = drop_database(args.name)
        print(f"Removed isolated CI database: {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
