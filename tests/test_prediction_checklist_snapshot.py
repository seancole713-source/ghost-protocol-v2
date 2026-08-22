"""Prediction issuance requires an immutable checklist in the same transaction."""
from __future__ import annotations

import pytest

from core import prediction


class _Cursor:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))


def test_prepare_snapshot_uses_exact_issue_time_feature_units_and_sources(monkeypatch):
    import core.checklist_evidence as evidence
    import core.catalyst_checklist as checklist

    captured = {}

    def fake_collect(symbol, *, asof_ts, market_ctx):
        captured.update(symbol=symbol, asof_ts=asof_ts, market_ctx=market_ctx)
        return {"relative_volume": 3.0}

    monkeypatch.setattr(evidence, "collect_evidence", fake_collect)
    monkeypatch.setattr(evidence, "sources_for", lambda frozen: {"relative_volume": "frozen-feed"})
    monkeypatch.setattr(
        checklist,
        "evaluate_checklist",
        lambda symbol, direction, frozen: {
            "checklist_version": "v1",
            "hold_bars": 3,
            "score_pct": 20.0,
            "blocked": False,
            "groups": [{"boxes": [{"signal": "relative_volume", "state": "pass"}]}],
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
            },
            "extended_session": {
                "symbol": "WOLF",
                "session": "premarket",
                "live_price": 3.55,
                "session_price": 3.6,
                "previous_close": 3.0,
                "gap_abs": 0.6,
                "gap_pct": 20.0,
                "price_as_of_ts": 1_766_000_110,
                "requested_at_ts": 1_766_000_120,
                "ts": 1_766_000_120,
            },
        },
    }

    snapshot = prediction._prepare_checklist_snapshot(pick)

    assert snapshot["issued_at"] == pick["predicted_at"]
    assert captured["asof_ts"] == pick["predicted_at"]
    assert captured["market_ctx"]["feature_asof_ts"] == 1_766_000_100
    assert captured["market_ctx"]["trend_slope_pct"] == pytest.approx(-1.2)
    assert captured["market_ctx"]["market_move_pct"] == pytest.approx(3.4)
    assert captured["market_ctx"]["price"] == pytest.approx(3.6)
    assert captured["market_ctx"]["prior_close"] == pytest.approx(3.0)
    assert captured["market_ctx"]["peak_move_pct"] == pytest.approx(20.0)
    assert captured["market_ctx"]["price_as_of_ts"] == 1_766_000_110
    assert snapshot["report"]["groups"][0]["boxes"][0]["source"] == "frozen-feed"


def test_snapshot_insert_is_direct_and_uses_exact_prediction_id(monkeypatch):
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

    assert prediction._persist_checklist_snapshot(
        cur, pick=pick, prediction_id=42, snapshot=snapshot,
    ) == 55
    assert cur.executed == []
    assert captured["cursor"] is cur
    assert captured["prediction_id"] == 42
    assert captured["issued_at"] == snapshot["issued_at"]


def test_snapshot_failure_propagates_to_candidate_transaction(monkeypatch):
    import core.checklist_ledger as ledger

    def fail(*args, **kwargs):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(ledger, "store_snapshot_with_cursor", fail)
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        prediction._persist_checklist_snapshot(
            _Cursor(),
            pick={"symbol": "WOLF", "direction": "UP"},
            prediction_id=42,
            snapshot={"issued_at": 123, "evidence": {}, "report": {}},
        )
