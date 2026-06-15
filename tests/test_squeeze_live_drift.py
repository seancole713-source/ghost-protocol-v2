"""Tests for squeeze live drift (core.squeeze_live_drift)."""

from core.squeeze_live_drift import (
    build_live_drift_board,
    compute_live_drift,
    enrich_pick_rows,
    first_alert_buy_map,
)


def test_first_alert_buy_map_uses_earliest_alert():
    alerts = [
        {"symbol": "HOOD", "buy": 99.75, "alerted_at": 1000},
        {"symbol": "HOOD", "buy": 98.50, "alerted_at": 2000},
        {"symbol": "AMC", "buy": 2.40, "alerted_at": 1500},
    ]
    m = first_alert_buy_map(alerts)
    assert m["HOOD"] == 99.75
    assert m["AMC"] == 2.40


def test_compute_live_drift_below_alert():
    d = compute_live_drift(99.75, 98.26)
    assert d is not None
    assert d["gap_pct"] == round((98.26 - 99.75) / 99.75 * 100, 2)
    assert d["drift_status"] == "below"
    assert "below alert" in d["gap_label"]


def test_compute_live_drift_fading():
    d = compute_live_drift(2.40, 2.31)
    assert d["drift_status"] == "fading"


def test_enrich_pick_rows_adds_live_drift():
    alerts = [{"symbol": "HOOD", "buy": 99.75, "alerted_at": 1}]
    picks = [{"symbol": "HOOD", "buy": 98.26, "price": 98.26, "kind": "squeeze_forming"}]
    out = enrich_pick_rows(picks, alerts, [])
    assert out[0]["live_price"] == 98.26
    assert out[0]["alert_buy"] == 99.75
    assert out[0]["gap_pct"] < 0


def test_build_live_drift_board_one_row_per_symbol():
    alerts = [
        {"symbol": "HOOD", "buy": 99.75, "alerted_at": 1, "kind": "squeeze_forming"},
        {"symbol": "AMC", "buy": 2.40, "alerted_at": 2, "kind": "squeeze_active"},
    ]
    picks = [
        {"symbol": "HOOD", "price": 98.26},
        {"symbol": "AMC", "price": 2.31},
    ]
    board = build_live_drift_board(alerts, picks, [])
    assert len(board) == 2
    by_sym = {r["symbol"]: r for r in board}
    assert by_sym["HOOD"]["gap_pct"] < 0
    assert by_sym["AMC"]["drift_status"] == "fading"
