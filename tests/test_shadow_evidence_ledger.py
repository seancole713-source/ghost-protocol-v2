"""Tests for shadow-only evidence-score persistence.

The two things that matter structurally: this module never touches a
prediction/gate table, and a score is written once and never overwritten.
"""
from __future__ import annotations

from core import shadow_evidence_ledger as sel


class _Cursor:
    def __init__(self, *, fetchall_rows=None, fetchone_rows=None):
        self.fetchall_rows = list(fetchall_rows or [])
        self.fetchone_rows = list(fetchone_rows or [])
        self.executed = []
        self.params = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        self.params.append(params)

    def fetchall(self):
        return self.fetchall_rows

    def fetchone(self):
        if self.fetchone_rows:
            return self.fetchone_rows.pop(0)
        return None


def test_ensure_tables_creates_shadow_score_table():
    cur = _Cursor()
    sel.ensure_shadow_evidence_tables(cur)
    sql = " ".join(cur.executed)
    assert "CREATE TABLE IF NOT EXISTS ghost_shadow_evidence_scores" in sql
    assert "UNIQUE (evidence_id, scoring_version)" in sql


def test_module_never_imports_or_queries_the_fire_path():
    """Structural guarantee, not just a docstring claim: strip comments and
    docstrings, then check the remaining code for imports of or SQL against
    the live prediction/gate/wallet path. Prose in this module's own
    docstring (which explains what it does NOT touch) must not trip the
    check -- only real code may."""
    import ast
    import inspect

    source = inspect.getsource(sel)
    tree = ast.parse(source)

    # No import of the fire-path modules, anywhere in the file.
    forbidden_modules = {"core.prediction", "core.signal_engine", "core.paper_wallet"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module not in forbidden_modules, f"imports {node.module}"
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in forbidden_modules, f"imports {alias.name}"

    # No string literal *inside code* (not a docstring/comment) named after
    # a live fire-path table. Comments are stripped by ast; module/function
    # docstrings are the only remaining false-positive risk, so drop Expr
    # statements that are bare string constants (docstrings) before scanning.
    forbidden_tables = ("ghost_kill", "wallet_trades", "gate_status")
    for node in ast.walk(tree):
        is_docstring = (
            isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant)
            and isinstance(node.value.value, str)
        )
        if is_docstring:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            lowered = node.value.lower()
            for term in forbidden_tables:
                assert term not in lowered, f"non-docstring string literal references {term!r}: {node.value!r}"
    # "predictions" (the bare live table, not ghost_agent_tasks/ghost_shadow_*)
    # is checked separately since it's a substring of this file's own
    # docstring prose ("live predictions") in ways ast.Constant scanning
    # above already excludes -- confirm no SQL literal names it directly.
    sql_like_strings = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "SELECT" in node.value.upper()
    ]
    for sql in sql_like_strings:
        assert "FROM predictions" not in sql and "predictions " not in sql.lower()


def test_score_and_store_is_idempotent_on_evidence_and_version():
    """First call inserts; a second call with an existing row must return
    the stored score, not attempt a duplicate write."""
    existing_row = (
        7, 0.42, {"source_authority": 0.5}, {"note": "detail"}, {"source_authority": 0.28},
        0, 1_800_000_000,
    )
    cur = _Cursor(fetchone_rows=[existing_row])

    class _FakeConn:
        def cursor(self):
            return cur
        def commit(self):
            pass

    class _FakeDbConn:
        def __enter__(self):
            return _FakeConn()
        def __exit__(self, *a):
            return False

    import core.db as core_db
    saved = core_db.db_conn
    core_db.db_conn = _FakeDbConn
    try:
        result = sel.score_and_store_evidence(
            evidence_id="evd_test", task_id="agt_test", symbol="TEST",
            claims={"verdict": "supports"}, source_refs=[{"kind": "news_article", "locator": "https://a.com"}],
        )
    finally:
        core_db.db_conn = saved

    assert result["idempotent"] is True
    assert result["composite_score"] == 0.42
    # No INSERT should have been attempted once an existing row was found.
    assert not any("INSERT INTO ghost_shadow_evidence_scores" in sql for sql in cur.executed)


def test_score_pending_evidence_skips_one_failure_without_stopping_the_pass():
    rows = [
        ("evd_good", "agt_1", {"verdict": "supports"}, [{"kind": "news_article", "locator": "https://a.com"}], "AAA"),
        ("evd_bad", "agt_2", None, None, "BBB"),  # will still parse fine (empty dict/list), not a real failure case
    ]
    cur = _Cursor(fetchall_rows=rows)

    class _FakeConn:
        def cursor(self):
            return cur
        def commit(self):
            pass

    class _FakeDbConn:
        def __enter__(self):
            return _FakeConn()
        def __exit__(self, *a):
            return False

    import core.db as core_db
    saved = core_db.db_conn

    calls = []

    def fake_score_and_store(**kwargs):
        calls.append(kwargs["evidence_id"])
        if kwargs["evidence_id"] == "evd_bad":
            raise RuntimeError("boom")
        return {"ok": True}

    saved_score_fn = sel.score_and_store_evidence
    core_db.db_conn = _FakeDbConn
    sel.score_and_store_evidence = fake_score_and_store
    try:
        result = sel.score_pending_evidence(limit=10)
    finally:
        core_db.db_conn = saved
        sel.score_and_store_evidence = saved_score_fn

    assert result["ok"] is True
    assert result["scored"] == 1
    assert result["failed"] == 1
    assert result["scanned"] == 2
    assert calls == ["evd_good", "evd_bad"]


def test_recent_scores_filters_by_symbol_and_task():
    cur = _Cursor(fetchall_rows=[])

    class _FakeConn:
        def cursor(self):
            return cur
        def commit(self):
            pass

    class _FakeDbConn:
        def __enter__(self):
            return _FakeConn()
        def __exit__(self, *a):
            return False

    import core.db as core_db
    saved = core_db.db_conn
    core_db.db_conn = _FakeDbConn
    try:
        sel.recent_scores(symbol="brnx", task_id="agt_x", limit=5)
    finally:
        core_db.db_conn = saved

    executed_sql = cur.executed[-1]
    assert "symbol = %s" in executed_sql
    assert "task_id = %s" in executed_sql
    assert cur.params[-1] == ("BRNX", "agt_x", 5)
