from fastapi import APIRouter
from core.db import db_conn

router = APIRouter()

@router.get("/api/schema")
def get_schema():
    """Show actual DB table columns - used for v1->v2 migration debugging."""
    tables = {}
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position
        """)
        for table, col, dtype in cur.fetchall():
            if table not in tables:
                tables[table] = []
            tables[table].append({"col": col, "type": dtype})
    return {"ok": True, "tables": tables}