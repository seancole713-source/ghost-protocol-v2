"""Postgres backing for edge.ledger.Store -- one table, append-safe.

    edge_rows(tbl TEXT, key TEXT, row JSONB, known_at BIGINT, PRIMARY KEY (tbl, key))

`known_at` is when THIS system wrote the row: every stored fact carries the
moment it became known, which is what makes a later replay point-in-time.
The ledger enforces immutability; this store only persists.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional

DDL = """
CREATE TABLE IF NOT EXISTS edge_rows (
    tbl TEXT NOT NULL,
    key TEXT NOT NULL,
    row JSONB NOT NULL,
    known_at BIGINT NOT NULL,
    PRIMARY KEY (tbl, key)
);
CREATE INDEX IF NOT EXISTS edge_rows_tbl_known ON edge_rows (tbl, known_at);
"""


class PostgresStore:
    def __init__(self, connect: Callable[[], Any]) -> None:
        """`connect` returns a context manager yielding a DB-API connection."""
        self._connect = connect

    def ensure(self) -> None:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(DDL)
            conn.commit()

    def get(self, table: str, key: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT row FROM edge_rows WHERE tbl=%s AND key=%s", (table, key))
            r = cur.fetchone()
        if not r:
            return None
        return r[0] if isinstance(r[0], dict) else json.loads(r[0])

    def put(self, table: str, key: str, row: Dict[str, Any]) -> None:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO edge_rows (tbl, key, row, known_at) VALUES (%s, %s, %s::jsonb, %s) "
                "ON CONFLICT (tbl, key) DO UPDATE SET row = EXCLUDED.row, known_at = EXCLUDED.known_at",
                (table, key, json.dumps(row, default=str), int(time.time())),
            )
            conn.commit()

    def scan(self, table: str, **where: Any) -> List[Dict[str, Any]]:
        clauses, params = ["tbl=%s"], [table]
        for k, v in where.items():
            clauses.append("row->>%s = %s")
            params += [k, str(v)]
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT row FROM edge_rows WHERE " + " AND ".join(clauses) + " ORDER BY known_at",
                        tuple(params))
            rows = cur.fetchall()
        return [r[0] if isinstance(r[0], dict) else json.loads(r[0]) for r in rows]

    def claim(self, name: str, *, owner: str, ttl_s: int, now: Optional[int] = None) -> bool:
        """Atomically take (or renew) a lease. Two Ghost containers overlap for a minute or two
        during every redeploy; only the lease holder runs an edge tick, so orders and phone
        messages cannot go out twice."""
        now = int(now if now is not None else time.time())
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO edge_rows (tbl, key, row, known_at) VALUES ('edge_lease', %s, %s::jsonb, %s) "
                "ON CONFLICT (tbl, key) DO UPDATE SET row = EXCLUDED.row, known_at = EXCLUDED.known_at "
                "WHERE edge_rows.known_at < %s OR edge_rows.row->>'owner' = %s RETURNING key",
                (name, json.dumps({"owner": owner}), now, now - int(ttl_s), owner),
            )
            got = cur.fetchone()
            conn.commit()
        return got is not None

    def release(self, name: str, *, owner: str) -> None:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE edge_rows SET known_at = 0 WHERE tbl = 'edge_lease' AND key = %s "
                        "AND row->>'owner' = %s", (name, owner))
            conn.commit()

    def prune(self, table: str, *, older_than: int) -> int:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM edge_rows WHERE tbl = %s AND known_at < %s", (table, int(older_than)))
            n = cur.rowcount
            conn.commit()
        return int(n or 0)

