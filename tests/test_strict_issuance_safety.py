"""Fail-closed final issuance safety checks."""
from __future__ import annotations

import pytest

from core import risk_discipline as risk


class _Cursor:
    def __init__(self, pause_rows=(), trade_rows=(), fail=False):
        self.pause_rows = list(pause_rows)
        self.trade_rows = list(trade_rows)
        self.fail = fail
        self.executed = []
        self._rows = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self.fail:
            raise RuntimeError("database unavailable")
        if "SELECT key,val FROM ghost_state" in sql:
            self._rows = self.pause_rows
        elif "SELECT outcome, pnl_pct FROM predictions" in sql:
            self._rows = self.trade_rows
        elif sql.startswith("DELETE FROM ghost_state"):
            self._rows = []

    def fetchall(self):
        return list(self._rows)


def _settings():
    return {
        "account_size_usd": 10000.0,
        "daily_loss_limit_usd": 100.0,
        "daily_max_losses": 2,
        "open_buffer_min": 0,
    }


def test_strict_issuance_blocks_latched_pause(monkeypatch):
    cur = _Cursor(pause_rows=[
        ("engine_paused", "1"),
        ("engine_pause_reason", "manual stop"),
        ("engine_pause_latched", "1"),
    ])
    monkeypatch.setattr(risk, "risk_settings", _settings)
    monkeypatch.setattr(risk, "in_open_buffer_window", lambda: (False, ""))

    state = risk.strict_issuance_block(cur)

    assert state["blocked"] is True
    assert state["unavailable"] is False
    assert state["engine_pause"]["latched"] is True
    assert any("manual stop" in reason for reason in state["reasons"])
    trade_sql = next(sql for sql, _ in cur.executed if "SELECT outcome, pnl_pct" in sql)
    assert "asset_type='stock'" in trade_sql
    assert "symbol='WOLF'" not in trade_sql


def test_strict_issuance_db_uncertainty_blocks(monkeypatch):
    monkeypatch.setattr(risk, "risk_settings", _settings)
    state = risk.strict_issuance_block(_Cursor(fail=True))
    assert state["blocked"] is True
    assert state["unavailable"] is True
    assert state["reasons"] == ["safety state unavailable"]


@pytest.mark.parametrize("key", ["engine_paused", "engine_pause_latched"])
def test_strict_issuance_malformed_pause_flags_fail_closed(monkeypatch, key):
    cur = _Cursor(pause_rows=[
        ("engine_paused", "1"),
        ("engine_pause_auto_resume_at", "1"),
        ("engine_pause_latched", "0"),
        (key, "true"),
    ])
    monkeypatch.setattr(risk, "risk_settings", _settings)
    monkeypatch.setattr(risk, "in_open_buffer_window", lambda: (False, ""))

    state = risk.strict_issuance_block(cur)

    assert state["blocked"] is True
    assert state["unavailable"] is True
    assert key in state["error"]
    assert not any(sql.startswith("DELETE FROM ghost_state") for sql, _ in cur.executed)


def test_strict_issuance_clears_only_expired_unlatched_pause(monkeypatch):
    cur = _Cursor(pause_rows=[
        ("engine_paused", "1"),
        ("engine_pause_reason", "cooldown"),
        ("engine_pause_auto_resume_at", "1"),
        ("engine_pause_latched", "0"),
    ])
    monkeypatch.setattr(risk, "risk_settings", _settings)
    monkeypatch.setattr(risk, "in_open_buffer_window", lambda: (False, ""))

    state = risk.strict_issuance_block(cur)

    assert state["blocked"] is False
    assert any(sql.startswith("DELETE FROM ghost_state") for sql, _ in cur.executed)
