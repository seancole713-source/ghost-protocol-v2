"""Super Ghost read / per-cycle paths run no DDL; the startup migration owns it.

Production: CREATE INDEX IF NOT EXISTS on every read / cycle takes a SHARE
lock per statement (and an ACCESS EXCLUSIVE on ALTER ... IF NOT EXISTS), the
same pattern behind the hourly shadow-resolver deadlocks. Public GET endpoints
(history / accuracy / if-followed / summaries) ran it on every hit.
"""
from __future__ import annotations

import ast
import glob
import inspect
import os

import pytest

import core.db as dbmod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _UndefinedTable(Exception):
    pgcode = "42P01"


class _RecordingCursor:
    def __init__(self, log, missing=False):
        self.log = log
        self.missing = missing
        self._rows = []

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split())
        self.log.append(norm)
        self._rows = []
        if self.missing and norm.upper().startswith(("SELECT", "INSERT", "UPDATE")):
            raise _UndefinedTable('relation "super_ghost_x" does not exist')

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return None


class _Ctx:
    def __init__(self, log, missing):
        self.log = log
        self.missing = missing

    def __enter__(self):
        log, missing = self.log, self.missing
        return type("C", (), {"cursor": lambda _s: _RecordingCursor(log, missing)})()

    def __exit__(self, *a):
        return False


def _install(monkeypatch, *, missing=False):
    log = []
    monkeypatch.setattr(dbmod, "db_conn", lambda: _Ctx(log, missing))
    return log


def _ddl(log):
    return [s for s in log if s.upper().startswith(("CREATE", "ALTER", "DROP"))]


def _read_calls():
    from core import (
        super_ghost_data_brain as db_brain,
        super_ghost_feature_store as fs,
        super_ghost_lab as lab,
        super_ghost_learning as learning,
        super_ghost_ledger as ledger,
        super_ghost_memory as memory,
        super_ghost_precision as precision,
        super_ghost_promotion as promotion,
        super_ghost_range_calibration as rng,
        super_ghost_regime_calibration as regime,
        super_ghost_shadow as shadow,
    )
    return {
        "ledger.get_history": lambda: ledger.get_history(limit=5),
        "ledger.get_accuracy": lambda: ledger.get_accuracy(horizon=5),
        "ledger.get_if_followed": lambda: ledger.get_if_followed(horizon=5),
        "shadow.shadow_summary": lambda: shadow.shadow_summary(),
        "shadow.shadow_model_profiles": lambda: shadow.shadow_model_profiles(),
        "data_brain.latest_data_brain_snapshots": lambda: db_brain.latest_data_brain_snapshots(),
        "feature_store.latest_snapshots": lambda: fs.latest_snapshots(),
        "feature_store.leakage_audit": lambda: fs.leakage_audit(),
        "lab.latest_lab_summary": lambda: lab.latest_lab_summary(),
        "learning.learning_summary": lambda: learning.learning_summary(),
        "memory.list_models": lambda: memory.list_models(),
        "memory.recent_features": lambda: memory.recent_features(),
        "memory.feature_profile": lambda: memory.feature_profile(),
        "precision.precision_summary": lambda: precision.precision_summary(),
        "promotion.latest_promotion_reviews": lambda: promotion.latest_promotion_reviews(),
        "range.range_calibration_summary": lambda: rng.range_calibration_summary(),
        "regime.regime_calibration_summary": lambda: regime.regime_calibration_summary(),
    }


def _profile_calls():
    from core import (
        super_ghost_learning as learning,
        super_ghost_range_calibration as rng,
        super_ghost_regime_calibration as regime,
    )
    return {
        "learning.get_learning_profile": lambda: learning.get_learning_profile("WOLF", "UP"),
        "learning.get_pooled_learning_profile": lambda: learning.get_pooled_learning_profile("UP"),
        "range.get_range_calibration_profile": lambda: rng.get_range_calibration_profile("WOLF", "UP"),
        "regime.get_regime_calibration_profile": lambda: regime.get_regime_calibration_profile("WOLF", "UP"),
    }


def _cycle_calls(monkeypatch):
    from core import (
        super_ghost_data_brain as db_brain,
        super_ghost_lab as lab,
        super_ghost_learning as learning,
        super_ghost_ledger as ledger,
        super_ghost_memory as memory,
        super_ghost_precision as precision,
        super_ghost_range_calibration as rng,
        super_ghost_regime_calibration as regime,
        super_ghost_shadow as shadow,
        super_ghost_feature_store as fs,
    )
    monkeypatch.setattr(db_brain, "build_data_brain", lambda sym, use_cache=False: {
        "symbol": sym, "as_of_ts": 1, "coverage": {}, "derived": {}, "sources": {},
    })
    report = {
        "ok": True, "symbol": "WOLF", "engine": "e", "ts": 100,
        "prediction": {"direction": "UP", "confidence": 0.7, "accuracy_grade": "B"},
        "market_regime": {}, "coverage": {"available": 18},
        "risk_plan": {"entry": 100.0, "stop_loss": 95.0, "target_price": 110.0},
        "checklist": [], "top_drivers": {}, "ai_brief": {},
    }

    def _cur():
        return dbmod.db_conn().__enter__().cursor()

    return {
        "ledger.log_prediction": lambda: ledger.log_prediction(report),
        "shadow.store_shadow_predictions": lambda: shadow.store_shadow_predictions(_cur(), 1, report),
        "shadow.resolve_shadow_predictions": lambda: shadow.resolve_shadow_predictions(),
        "feature_store.persist_feature_snapshot": lambda: fs.persist_feature_snapshot(_cur(), report, ledger_id=1),
        "memory.log_prediction_memory": lambda: memory.log_prediction_memory(_cur(), 1, report),
        "memory.score_features_from_ledger": lambda: memory.score_features_from_ledger(),
        "data_brain.persist_data_brain": lambda: db_brain.persist_data_brain("WOLF"),
        "lab.run_lab": lambda: lab.run_lab(persist=True),
        "learning.learn_from_ledger": lambda: learning.learn_from_ledger(),
        "precision.score_precision_from_ledger": lambda: precision.score_precision_from_ledger(),
        "range.rebuild_range_calibration": lambda: rng.rebuild_range_calibration(),
        "regime.rebuild_regime_calibration": lambda: regime.rebuild_regime_calibration(),
    }


@pytest.mark.parametrize("name", sorted(_read_calls()))
def test_read_paths_run_no_ddl(monkeypatch, name):
    log = _install(monkeypatch)
    _read_calls()[name]()
    assert log, f"{name} issued no SQL (fake not reached)"
    assert _ddl(log) == [], f"{name} ran DDL on a read: {_ddl(log)}"


@pytest.mark.parametrize("name", sorted(_profile_calls()))
def test_profile_lookups_run_no_ddl(monkeypatch, name):
    log = _install(monkeypatch)
    _profile_calls()[name]()
    assert _ddl(log) == [], f"{name} ran DDL: {_ddl(log)}"


def test_per_cycle_paths_run_no_ddl(monkeypatch):
    log = _install(monkeypatch)
    offenders = {}
    for name, call in _cycle_calls(monkeypatch).items():
        before = len(log)
        call()
        ddl = _ddl(log[before:])
        if ddl:
            offenders[name] = ddl
    assert offenders == {}


@pytest.mark.parametrize("name", sorted(_read_calls()))
def test_reads_tolerate_missing_table_without_creating_it(monkeypatch, name):
    log = _install(monkeypatch, missing=True)
    out = _read_calls()[name]()
    assert _ddl(log) == []
    assert out["ok"] is True
    assert out["reason"] == dbmod.SCHEMA_MISSING_REASON
    assert "error" not in out
    for key in ("rows", "profiles", "snapshots", "leaks", "models", "features", "reviews"):
        if key in out:
            assert out[key] == []


def test_profile_lookups_degrade_on_missing_table(monkeypatch):
    for name, call in _profile_calls().items():
        log = _install(monkeypatch, missing=True)
        out = call()
        assert _ddl(log) == [], name
        assert out.get("available") is False, name


def test_missing_schema_error_classifier():
    assert dbmod.is_missing_schema_error(_UndefinedTable("x"))
    col = type("E", (Exception,), {"pgcode": "42703"})("column y does not exist")
    assert dbmod.is_missing_schema_error(col)
    assert dbmod.is_missing_schema_error(Exception('relation "t" does not exist'))
    assert not dbmod.is_missing_schema_error(Exception("deadlock detected"))
    assert dbmod.missing_schema_result(Exception("timeout"), {"rows": []}) is None


def _ensure_names_called_outside_ensure_functions(path):
    tree = ast.parse(open(path).read())
    bad = []
    for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
        if fn.name.startswith("ensure_"):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id.startswith("ensure_"):
                bad.append(f"{fn.name} -> {node.func.id}")
    return bad


def test_no_super_ghost_module_calls_ensure_outside_migration():
    offenders = {}
    for path in sorted(glob.glob(os.path.join(ROOT, "core", "super_ghost*.py"))):
        bad = _ensure_names_called_outside_ensure_functions(path)
        if bad:
            offenders[os.path.basename(path)] = bad
    assert offenders == {}


def test_startup_migration_runs_every_super_ghost_ensure():
    """Every schema function the read/cycle paths used to call is run at boot."""
    src = inspect.getsource(dbmod._migrate_schema)
    defined = set()
    for path in glob.glob(os.path.join(ROOT, "core", "super_ghost*.py")):
        tree = ast.parse(open(path).read())
        defined |= {n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("ensure_")}
    assert defined, "no ensure_* functions found"
    missing = sorted(name for name in defined if f"{name}(cur)" not in src)
    assert missing == []


def test_news_available_runs_no_ddl_and_reads_missing_table_as_unavailable(monkeypatch):
    """Shadow news models call news_available() per prediction; it used to run
    CREATE / ALTER TABLE / a dedupe DELETE / CREATE INDEX every call."""
    from core.news_events import news_available
    log = _install(monkeypatch)
    news_available()
    assert log == ["SELECT MAX(ingested_at) FROM ghost_news_raw_articles"]
    log = _install(monkeypatch, missing=True)
    assert news_available() is False
    assert _ddl(log) == []


def test_startup_migration_creates_news_event_tables():
    src = inspect.getsource(dbmod._migrate_schema)
    assert "from core.news_events import ensure_news_tables" in src
