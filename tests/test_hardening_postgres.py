"""Real-PostgreSQL regressions for provenance and transaction isolation."""
from __future__ import annotations

import time
import uuid

import pytest


@pytest.mark.integration
def test_news_provenance_expression_index_allows_distinct_evidence(integration_db):
    from core.db import db_conn
    from core.news_events import ensure_news_tables

    suffix = uuid.uuid4().hex[:10].upper()
    symbol = f"PG{suffix}"[:20]
    event_type = f"test_{suffix.lower()}"
    now = int(time.time())
    keys = [f"{suffix}-direct", f"{suffix}-mrna", f"{suffix}-bntx"]
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_news_tables(cur)
        cur.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname='idx_news_event_provenance_once'"
        )
        row = cur.fetchone()
        assert row is not None
        assert "derived" in row[0].lower()
        try:
            for dedupe_key, derived, origin in (
                (keys[0], False, None),
                (keys[1], True, "MRNA"),
                (keys[2], True, "BNTX"),
            ):
                cur.execute(
                    """INSERT INTO ghost_news_events
                       (symbol, event_type, asof_ts, extracted_at, dedupe_key,
                        derived, origin_symbol)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (symbol, event_type, now, now, dedupe_key, derived, origin),
                )
            cur.execute(
                "SELECT COUNT(*) FROM ghost_news_events WHERE dedupe_key = ANY(%s)",
                (keys,),
            )
            assert cur.fetchone()[0] == 3
        finally:
            cur.execute(
                "DELETE FROM ghost_news_events WHERE dedupe_key = ANY(%s)", (keys,)
            )


@pytest.mark.integration
def test_timeline_database_failure_does_not_poison_next_surface(integration_db):
    from core.symbol_timeline import _read_db_surface

    def broken(symbol, cur):
        cur.execute("SELECT * FROM ghost_deliberately_missing_timeline_table")
        return []

    def healthy(symbol, cur):
        cur.execute("SELECT %s::text", (symbol,))
        return [{"symbol": cur.fetchone()[0]}]

    failed_rows, failed_state = _read_db_surface("WOLF", "broken", broken)
    healthy_rows, healthy_state = _read_db_surface("WOLF", "healthy", healthy)

    assert failed_rows == []
    assert failed_state == {
        "status": "unavailable", "count": 0, "error": "database_query_failed"
    }
    assert healthy_rows == [{"symbol": "WOLF"}]
    assert healthy_state == {"status": "available", "count": 1, "error": None}
