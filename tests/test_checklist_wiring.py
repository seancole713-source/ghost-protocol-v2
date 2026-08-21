"""Checklist snapshots are resolved later, but never reconstructed later."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_scheduler_registers_resolver_only():
    source = (ROOT / "wolf_app.py").read_text(encoding="utf-8")
    assert 'scheduler.register("checklist_resolver"' in source
    assert 'scheduler.register("checklist_snapshot"' not in source


def test_database_startup_migrates_checklist_ledger():
    source = (ROOT / "core" / "db.py").read_text(encoding="utf-8")
    assert "from core.checklist_ledger import ensure_checklist_tables" in source
    assert "ensure_checklist_tables(cur)" in source
