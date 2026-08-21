"""Checklist persistence is optional but must share the issuance transaction."""
from __future__ import annotations

import pytest

from core import prediction


class _Cursor:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))


def test_prepare_snapshot_uses_exact_issue_time_and_feature_units(monkeypatch):
    import core.checklist_evidence as evidence
    import core.catalyst_checklist as checklist

    captured = {}

    def fake_collect(symbol, *, asof_ts, market_ctx):
        captured.update(symbol=symbol, asof_ts=asof_ts, market_ctx=market_ctx)
        return {"relative_volume": 3.0}

    monkeypatch.setattr(evidence, "collect_evidence", fake_collect)
    monkeypatch.setattr(
        checklist,
        "evaluate_checklist",
        lambda symbol, direction, frozen: {
            "checklist_version": "v1",
            "hold_bars": 3,
            "score_pct": 20.0,
            "blocked": False,
        },
    )
    pick = {
        "symbol": "WOLF",
        "asset_type": "stock",
        "direction": "DOWN",
        "predicted_at": 1_766_000_123,
        "entry_price": 3.5,
        "features": {"feature_asof_ts": 1_766_000_100},
        "scores": {
            "features": {
                "volume_ratio": 2.4,
                "mom_4h": -0.012,
                "macro_spy_20d_return": 0.034,
            }
        },
    }

    snapshot = prediction._prepare_checklist_snapshot(pick)

    assert snapshot["issued_at"] == pick["predicted_at"]
    assert captured["asof_ts"] == pick["predicted_at"]
    assert captured["market_ctx"]["feature_asof_ts"] == 1_766_000_100
    assert captured["market_ctx"]["trend_slope_pct"] == pytest.approx(-1.2)
    assert captured["market_ctx"]["market_move_pct"] == pytest.approx(3.4)


def test_snapshot_insert_uses_savepoint_and_exact_prediction_id(monkeypatch):
    import core.checklist_ledger as ledger

    cur = _Cursor()
    captured = {}
    monkeypatch.setattr(
        ledger,
        "store_snapshot_with_cursor",
        lambda cursor, **kwargs: captured.update(cursor=cursor, **kwargs) or 55,
    )
    snapshot = {
        "issued_at": 1_766_000_123,
        "evidence": {"relative_volume": 3.0},
        "report": {"checklist_version": "v1", "hold_bars": 3, "score_pct": 20.0},
    }
    pick = {
        "symbol": "WOLF",
        "direction": "UP",
        "entry_price": 3.5,
        "target_price": 4.0,
        "stop_price": 3.2,
        "expires_at": 1_766_300_000,
    }

    assert prediction._persist_checklist_snapshot_isolated(
        cur, pick=pick, prediction_id=42, snapshot=snapshot,
    ) is True
    assert [sql for sql, _ in cur.executed] == [
        "SAVEPOINT checklist_snapshot",
        "RELEASE SAVEPOINT checklist_snapshot",
    ]
    assert captured["cursor"] is cur
    assert captured["prediction_id"] == 42
    assert captured["issued_at"] == snapshot["issued_at"]


def test_snapshot_failure_rolls_back_only_savepoint(monkeypatch):
    import core.checklist_ledger as ledger

    cur = _Cursor()

    def fail(*args, **kwargs):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(ledger, "store_snapshot_with_cursor", fail)
    ok = prediction._persist_checklist_snapshot_isolated(
        cur,
        pick={"symbol": "WOLF", "direction": "UP"},
        prediction_id=42,
        snapshot={"issued_at": 123, "evidence": {}, "report": {}},
    )

    assert ok is False
    assert [sql for sql, _ in cur.executed] == [
        "SAVEPOINT checklist_snapshot",
        "ROLLBACK TO SAVEPOINT checklist_snapshot",
        "RELEASE SAVEPOINT checklist_snapshot",
    ]
